#!/usr/bin/env python3
"""Pipeline de la newsletter watchtower : ai-weekly, ai-monthly, ai-yearly.

Étapes d'un passage :
  0. relevé des tendances X (Twitter) liées à l'IA, vérifiées par script, qui servent de pistes ;
  1. recherche indépendante par Codex et par Claude (même prompt, sortie JSON) ;
  2. vérification des liens par script (page joignable, date dans la période) ;
  3. synthèse en markdown à partir des seules entrées vérifiées ;
  4. contrôle que chaque lien du markdown vient des entrées, puis commit et push.

Usage :
  watchtower.py due [--today AAAA-MM-JJ] [--dry-run] [--no-push]
  watchtower.py weekly|monthly|yearly [--date AAAA-MM-JJ] [--no-push] [--force]
                                      [--providers codex,claude]

--force régénère une édition existante ; il exige --no-push (une édition publiée ne se réécrit pas).

Bibliothèque standard uniquement (Python >= 3.11).
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import fcntl
import html
import http.client
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
PROMPTS = ROOT / "prompts"
SCHEMAS = ROOT / "schemas"
RUNS = ROOT / "runs"
TZ = ZoneInfo("Europe/Paris")
REPO_URL = "https://github.com/Guillaume-Lombardo/watchtower"

# Premières éditions : rien n'est rattrapé avant ces dates.
FIRST_WEEKLY = dt.date(2026, 11, 23)
FIRST_MONTHLY = dt.date(2026, 12, 1)
FIRST_YEARLY = 2026
NEWSLETTER_START_FR = "23 novembre 2026"

CODEX_MODEL = "gpt-6.2-sol"
CODEX_EFFORT = "high"
CLAUDE_MODEL = "claude-opus-5-5"
CLAUDE_EFFORT = "high"
PROVIDERS = ("codex", "claude")
RESEARCH_TIMEOUT = 60 * 60
# Le relevé des tendances n'est qu'une aide : court et sans second essai, pour préserver la recherche.
TRENDS_TIMEOUT = 15 * 60
SYNTHESIS_TIMEOUT = 30 * 60
# Budget total d'un passage `due` : la tâche planifiée l'attend au plus 2 h.
RUN_BUDGET = 100 * 60
GIT_TIMEOUT = 5 * 60
# Chemins qu'un passage a le droit de pousser sur main.
PUBLISHABLE = ("weekly/", "monthly/", "yearly/", "raw/")

CATEGORIES = ["modeles", "hardware", "logiciel", "societes", "recherche", "regulation", "usages"]
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
        "septembre", "octobre", "novembre", "décembre"]
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0 link-checker/1.0"
# Codes HTTP qui signalent un site qui bloque les robots plutôt qu'une page absente.
BLOCKED_CODES = {401, 402, 403, 405, 406, 408, 425, 429, 451, 999}
# Liens X (Twitter) : l'identifiant d'un post ou d'une page tendance encode sa date (« snowflake »,
# millisecondes depuis l'époque X). Les pages tendances (/i/trending/<id>) sont des résumés générés par X :
# elles attestent qu'un sujet a fait parler, sans être une source primaire.
X_HOSTS = {"x.com", "twitter.com", "mobile.twitter.com", "mobile.x.com"}
X_STATUS = re.compile(r"^/(?:[A-Za-z0-9_]{1,15}/status(?:es)?|i/web/status|i/trending)/(\d{6,20})(?:/|$)")
X_TRENDING = re.compile(r"^/i/trending/\d+")
# Filtre des pages tendances X : on ne garde que celles dont le titre ou le résumé (rendus par X côté serveur)
# parle d'IA. Sigles en majuscules seulement (« ai » est un mot français) ; noms propres insensibles à la casse,
# suivis ou non d'un numéro de version (Qwen4, Gemini 4 reste couvert par « gemini »).
AI_ACRONYMS = re.compile(r"\b(?:AI|IA|AGI|LLMs?|TPUs?|NPUs?)\b")
# Termes sans ambiguïté : un seul suffit.
AI_TERMS = re.compile(
    r"\b(?:artificial intelligence|intelligence artificielle|machine learning|deep learning|neural net\w*|"
    r"large language models?|language models?|chatbots?|generative ai|ia g[ée]n[ée]rative|"
    r"open[- ]weights?|open[- ]source models?|superintelligence|ai act|"
    r"openai|chatgpt|gpt-?\d[\w.]*|anthropic|deepmind|google ai|meta ai|xai|"
    r"qwen|deepseek|zhipu|hugging ?face|stability ai|midjourney|elevenlabs|nvidia|cerebras|"
    r"thinking machines)(?:-?\d[\w.]*)?\b", re.I)
# Noms de produits qui ont aussi un autre sens (prénom, vent, signe, aviation…) : il en faut deux distincts,
# sauf s'ils portent une version ou un suffixe de produit (« Grok 4 », « Codex CLI »). Limite connue : un prénom
# suivi d'un nombre passe (« Claude 2 fois… ») ; les recherches ne proposent de toute façon que des tendances IA.
AI_WEAK_TERMS = re.compile(
    r"\b(claude|gemini|grok|groq|llama|mistral|sora|codex|copilot|cursor|windsurf|perplexity|runway|kimi|"
    r"moonshot|minimax|glm|ernie|blackwell|rubin|cuda)"
    r"(\s?-?\d[\w.]*|\s(?:cli|api|code|app|pro|ultra|opus|sonnet|haiku|fable))?\b", re.I)
X_EPOCH_MS = 1288834974657
BUZZ_LEVELS = ["inconnu", "faible", "moyen", "fort"]
TRACKING_PARAMS = re.compile(r"^(utm_|fbclid$|gclid$|mc_|ref$|ref_src$|cmpid$|ocid$|guccounter$)")


class PipelineError(RuntimeError):
    pass


_deadline: float | None = None


def step_timeout(schema: Path | None, search: bool) -> int:
    if schema is not None and schema.name == "trends.schema.json":
        return TRENDS_TIMEOUT
    return RESEARCH_TIMEOUT if search else SYNTHESIS_TIMEOUT


def call_timeout(limit: int) -> float:
    """Délai d'un appel, borné par le budget restant du passage."""
    if _deadline is None:
        return limit
    left = _deadline - time.monotonic()
    if left < 120:
        raise PipelineError("budget de temps du passage épuisé")
    return min(limit, left)


# --------------------------------------------------------------------------- dates


def today_paris() -> dt.date:
    return dt.datetime.now(TZ).date()


def now_paris() -> str:
    return dt.datetime.now(TZ).strftime("%d/%m %H:%M:%S")


def fr_date(d: dt.date, year: bool = True) -> str:
    day = "1er" if d.day == 1 else str(d.day)
    return f"{day} {MOIS[d.month - 1]}" + (f" {d.year}" if year else "")


def fr_range(start: dt.date, end: dt.date) -> str:
    if start.year == end.year and start.month == end.month:
        return f"du {'1er' if start.day == 1 else start.day} au {fr_date(end)}"
    if start.year == end.year:
        return f"du {fr_date(start, year=False)} au {fr_date(end)}"
    return f"du {fr_date(start)} au {fr_date(end)}"


def monday_of(d: dt.date) -> dt.date:
    return d - dt.timedelta(days=d.weekday())


class Edition:
    """Une édition à produire : type, date de publication, période couverte."""

    def __init__(self, kind: str, published: dt.date):
        self.kind = kind
        self.published = published
        if kind == "weekly":
            if published.weekday() != 0:
                raise PipelineError(f"un hebdo se publie un lundi, pas le {published}")
            self.start = published - dt.timedelta(days=7)
            self.end = published - dt.timedelta(days=1)
        elif kind == "monthly":
            if published.day != 1:
                raise PipelineError(f"un mensuel se publie le 1er, pas le {published}")
            self.end = published - dt.timedelta(days=1)
            self.start = self.end.replace(day=1)
        elif kind == "yearly":
            if (published.month, published.day) != (12, 20):
                raise PipelineError(f"la rétrospective se publie le 20/12, pas le {published}")
            self.start = dt.date(published.year, 1, 1)
            self.end = dt.date(published.year, 12, 19)
        else:
            raise PipelineError(f"type inconnu : {kind}")

    @property
    def name(self) -> str:
        return {"weekly": "ai-weekly", "monthly": "ai-monthly", "yearly": "ai-yearly"}[self.kind]

    @property
    def path(self) -> Path:
        return ROOT / self.kind / f"{self.published.isoformat()}_{self.name}.md"

    @property
    def raw_dir(self) -> Path:
        return ROOT / "raw" / self.kind / self.published.isoformat()

    def __repr__(self) -> str:
        return f"{self.name} {self.published} ({self.start} → {self.end})"


def default_edition(kind: str, today: dt.date) -> Edition:
    if kind == "weekly":
        return Edition(kind, monday_of(today))
    if kind == "monthly":
        return Edition(kind, today.replace(day=1))
    year = today.year if today >= dt.date(today.year, 12, 20) else today.year - 1
    return Edition(kind, dt.date(year, 12, 20))


def due_editions(today: dt.date, done=None) -> list[Edition]:
    """Éditions à produire aujourd'hui, dans l'ordre (l'hebdo d'abord : le mensuel s'en sert).

    Un passage manqué est rattrapé : l'hebdo pendant toute sa semaine, le mensuel jusqu'au 7,
    la rétrospective jusqu'au 31/12. `done(ed)` dit si l'édition est faite (par défaut : le
    fichier existe ; en publication : il est sur origin/main).
    """
    done = done or (lambda ed: ed.path.exists())
    due = []
    weekly = Edition("weekly", monday_of(today))
    if weekly.published >= FIRST_WEEKLY and not done(weekly):
        due.append(weekly)
    monthly = Edition("monthly", today.replace(day=1))
    if monthly.published >= FIRST_MONTHLY and today.day <= 7 and not done(monthly):
        due.append(monthly)
    if today.month == 12 and today.day >= 20 and today.year >= FIRST_YEARLY:
        yearly = Edition("yearly", dt.date(today.year, 12, 20))
        if not done(yearly):
            due.append(yearly)
    return due


# --------------------------------------------------------------------------- outils


def log(msg: str) -> None:
    print(f"[{now_paris()}] {msg}", flush=True)


def render(template_name: str, **values: str) -> str:
    text = (PROMPTS / template_name).read_text(encoding="utf-8")

    def sub(m: re.Match) -> str:
        key = m.group(1)
        if key == "style" and key not in values:  # règles de style communes aux synthèses
            values[key] = (PROMPTS / "style.md").read_text(encoding="utf-8").strip()
        if key not in values:
            raise PipelineError(f"{template_name} : variable {{{{{key}}}}} non fournie")
        return str(values[key])

    return re.sub(r"\{\{\s*([a-z_]+)\s*\}\}", sub, text)


def parse_json_text(text: str):
    text = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.S)
    if fence:
        text = fence.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise


def strip_md_fence(text: str) -> str:
    text = text.strip()
    m = re.match(r"^```(?:markdown|md)?\s*\n(.*)\n```$", text, re.S)
    return m.group(1).strip() if m else text


# Les URL (à recopier à l'identique) sont laissées telles quelles.
URL_SPAN = re.compile(r"https?://[^\s<>\]]+")
DASH_SPACED = re.compile(r"[ \u00a0\u202f]*[\u2013\u2014][ \u00a0\u202f]*")
BULLET_DASH = re.compile(r"^(\s*- \*\*[^*\n]+\*\*)[ \u00a0\u202f]*[\u2013\u2014][ \u00a0\u202f]*", re.M)


def normalize_typography(md: str) -> str:
    """Apostrophe droite, tiret simple à la place des tirets cadratin et demi-cadratin."""
    def fix(chunk: str) -> str:
        chunk = chunk.replace("\u2019", "'").replace("\u2018", "'")
        return DASH_SPACED.sub(lambda m: " - " if m.group(0) != m.group(0).strip() else "-", chunk)

    md = BULLET_DASH.sub(r"\1 : ", md)  # ancien format des puces : « **Titre** — … »
    out, pos = [], 0
    for m in URL_SPAN.finditer(md):
        out += [fix(md[pos:m.start()]), m.group(0)]
        pos = m.end()
    out.append(fix(md[pos:]))
    return "".join(out)


# Emoji : plans supplémentaires, sélecteur emoji, touches, et les caractères du BMP à propriété Unicode
# Emoji_Presentation (emoji-data.txt). Les symboles textuels (✓, ★, →, ×) restent permis.
EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\uFE0F\u20E3"
    "\u231A\u231B\u23E9-\u23EC\u23F0\u23F3\u25FD\u25FE\u2614\u2615\u2648-\u2653\u267F\u2693\u26A1"
    "\u26AA\u26AB\u26BD\u26BE\u26C4\u26C5\u26CE\u26D4\u26EA\u26F2\u26F3\u26F5\u26FA\u26FD\u2705"
    "\u270A\u270B\u2728\u274C\u274E\u2753-\u2755\u2757\u2795-\u2797\u27B0\u27BF\u2B1B\u2B1C\u2B50\u2B55]")


def child_env() -> dict[str, str]:
    env = dict(os.environ)
    # Le passage peut être lancé depuis une session Claude Code : ne pas la faire hériter.
    for key in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT"):
        env.pop(key, None)
    return env


def git(*args: str, check: bool = True) -> str:
    try:
        proc = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=GIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise PipelineError(f"git {' '.join(args)} : délai dépassé") from None
    if check and proc.returncode != 0:
        raise PipelineError(f"git {' '.join(args)} : {proc.stderr.strip()}")
    return proc.stdout.strip()


def provider_levels() -> dict[str, str]:
    try:
        out = subprocess.run(["agents-usage", "--json"], capture_output=True, text=True, timeout=120).stdout
        return {p["provider"]: p.get("level", "unknown") for p in json.loads(out)["providers"]}
    except Exception:  # agents-usage absent ou illisible : on ne bloque pas
        return {}


# --------------------------------------------------------------------------- appels aux modèles


def run_codex(prompt: str, workdir: Path, label: str, schema: Path | None, search: bool) -> str:
    out = workdir / f"{label}.codex.out"
    cmd = ["codex", "exec", "-m", CODEX_MODEL,
           "-c", f'model_reasoning_effort="{CODEX_EFFORT}"',
           "-c", f'web_search="{"live" if search else "disabled"}"',
           "-s", "read-only", "--skip-git-repo-check", "--ephemeral",
           "-C", str(workdir), "-o", str(out)]
    if schema:
        cmd += ["--output-schema", str(schema)]
    cmd.append("-")
    with open(workdir / f"{label}.codex.log", "w") as logf:
        proc = subprocess.run(cmd, input=prompt, text=True, stdout=logf, stderr=subprocess.STDOUT, cwd=workdir,
                              env=child_env(), timeout=call_timeout(step_timeout(schema, search)))
    if proc.returncode != 0 or not out.exists() or not out.read_text().strip():
        raise PipelineError(f"codex ({label}) : code {proc.returncode}, voir {workdir.name}/{label}.codex.log")
    return out.read_text(encoding="utf-8")


def run_claude(prompt: str, workdir: Path, label: str, schema: Path | None, search: bool) -> str:
    cmd = ["claude", "-p", "--model", CLAUDE_MODEL, "--effort", CLAUDE_EFFORT,
           "--output-format", "json", "--no-session-persistence", "--permission-mode", "dontAsk"]
    if schema:
        cmd += ["--json-schema", schema.read_text(encoding="utf-8")]
    if search:
        cmd += ["--tools", "WebSearch,WebFetch", "--allowedTools", "WebSearch,WebFetch"]
    else:
        cmd += ["--tools", ""]
    proc = subprocess.run(cmd, input=prompt, text=True, capture_output=True, cwd=workdir, env=child_env(),
                          timeout=call_timeout(step_timeout(schema, search)))
    (workdir / f"{label}.claude.log").write_text(proc.stderr + "\n" + proc.stdout[-4000:])
    if proc.returncode != 0:
        raise PipelineError(f"claude ({label}) : code {proc.returncode}, voir {workdir.name}/{label}.claude.log")
    envelope = json.loads(proc.stdout)
    if envelope.get("is_error"):
        raise PipelineError(f"claude ({label}) : {envelope.get('subtype')} {str(envelope.get('result'))[:300]}")
    if schema and envelope.get("structured_output") is not None:
        return json.dumps(envelope["structured_output"], ensure_ascii=False)
    return envelope.get("result", "")


RUNNERS = {"codex": run_codex, "claude": run_claude}
MODEL_NAMES = {"codex": CODEX_MODEL, "claude": CLAUDE_MODEL}


def research_all(prompt: str, schema: Path, workdir: Path, label: str, providers: list[str]) -> dict[str, dict]:
    """Lance la même recherche chez chaque fournisseur en parallèle ; un essai de plus en cas d'échec."""
    (workdir / f"{label}.prompt.md").write_text(prompt, encoding="utf-8")
    key = {"impact.schema.json": "topics", "trends.schema.json": "trends"}.get(schema.name, "items")

    attempts = (1,) if key == "trends" else (1, 2)

    def one(provider: str) -> dict:
        last = None
        for attempt in attempts:
            try:
                log(f"recherche {label} : {provider} (essai {attempt})")
                data = parse_json_text(RUNNERS[provider](prompt, workdir, label, schema, True))
                if not isinstance(data, dict) or not isinstance(data.get(key), list):
                    raise PipelineError(f"réponse sans liste « {key} »")
                log(f"recherche {label} : {provider} terminée")
                return data
            except (PipelineError, json.JSONDecodeError, subprocess.TimeoutExpired, KeyError, OSError) as exc:
                last = exc
                log(f"recherche {label} : {provider} en échec : {exc}")
        raise PipelineError(f"{provider} : {last}")

    results: dict[str, dict] = {}
    with cf.ThreadPoolExecutor(max_workers=len(providers)) as pool:
        futures = {pool.submit(one, p): p for p in providers}
        for fut in cf.as_completed(futures):
            provider = futures[fut]
            try:
                results[provider] = fut.result()
            except PipelineError as exc:
                log(f"recherche {label} : {provider} abandonné ({exc})")
    if not results:
        raise PipelineError(f"recherche {label} : aucun fournisseur n'a répondu")
    return results


def synthesize(prompt: str, workdir: Path, label: str, providers: list[str], allowed: set[str]) -> tuple[str, str]:
    """Rédige le markdown ; Claude d'abord, Codex en secours. Refuse tout lien hors des entrées."""
    order = [p for p in ("claude", "codex") if p in providers] or list(PROVIDERS)
    (workdir / f"{label}.prompt.md").write_text(prompt, encoding="utf-8")
    current = prompt
    errors = []
    for provider in order:
        for attempt in (1, 2):
            try:
                log(f"synthèse {label} : {provider} (essai {attempt})")
                md = normalize_typography(
                    strip_md_fence(RUNNERS[provider](current, workdir, f"{label}-{attempt}", None, False)))
            except (PipelineError, subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
                errors.append(f"{provider} : {exc}")
                log(f"synthèse {label} : {provider} en échec : {exc}")
                break
            problems = check_markdown(md, allowed)
            if not problems:
                return md, provider
            errors.append(f"{provider} : {problems[0]}")
            log(f"synthèse {label} : {len(problems)} problème(s) : {problems[:3]}")
            current = prompt + (
                "\n\n---\nTon brouillon précédent a été refusé par le contrôle automatique :\n- "
                + "\n- ".join(problems[:20])
                + "\nRecommence en corrigeant ces points, avec uniquement les URL fournies dans l'entrée,"
                " recopiées à l'identique."
            )
    raise PipelineError(f"synthèse {label} impossible : {'; '.join(errors)}")


# --------------------------------------------------------------------------- liens


def normalize_url(url: str) -> str:
    try:
        p = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return url.strip()
    host = (p.netloc.rsplit("@", 1)[-1] or "").lower()
    if host.startswith("www."):
        host = host[4:]
    query = urllib.parse.urlencode(
        [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True) if not TRACKING_PARAMS.match(k)])
    path = p.path.rstrip("/") or "/"
    if host in X_HOSTS:  # ?s=20, ?t=… : paramètres de partage, même post
        host, query = "x.com", ""
    return urllib.parse.urlunsplit(("https", host, path, query, ""))


# Métadonnées de publication d'abord ; les balises génériques (souvent la date de mise à jour) ensuite.
PUBLISHED_META = re.compile(
    r"""<meta[^>]+(?:property|name|itemprop)\s*=\s*["'](?:article:published_time|og:published_time|"""
    r"""datePublished|pubdate|publish[-_]date|parsely-pub-date|DC\.date\.issued|citation_publication_date)"""
    r"""["'][^>]*>""", re.I)
OTHER_DATE_META = re.compile(
    r"""<meta[^>]+(?:property|name|itemprop)\s*=\s*["'](?:date|sailthru\.date|dc\.date|dcterms\.date|"""
    r"""citation_date)["'][^>]*>""", re.I)
CONTENT_ATTR = re.compile(r"""content\s*=\s*["']([^"']+)["']""", re.I)
JSONLD_DATE = re.compile(r'"datePublished"\s*:\s*"([^"]+)"')
ISO_DAY = re.compile(r"(\d{4})[-/](\d{2})[-/](\d{2})")


def parse_day(value: str) -> dt.date | None:
    value = (value or "").strip()
    try:  # horodatage avec fuseau : jour à Paris
        stamp = dt.datetime.fromisoformat(value)
        if stamp.tzinfo:
            return stamp.astimezone(TZ).date()
    except ValueError:
        pass
    m = ISO_DAY.search(value)
    try:
        if m:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", value)  # JJ/MM/AAAA
        if m:
            return dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None
    return None


def extract_page_date(body: str) -> dt.date | None:
    def from_meta(pattern: re.Pattern) -> dt.date | None:
        for tag in pattern.finditer(body):
            content = CONTENT_ATTR.search(tag.group(0))
            if content and (d := parse_day(html.unescape(content.group(1)))):
                return d
        return None

    m = JSONLD_DATE.search(body)
    return from_meta(PUBLISHED_META) or (parse_day(m.group(1)) if m else None) or from_meta(OTHER_DATE_META)


def x_post_date(url: str) -> dt.date | None:
    """Date (heure de Paris) d'un post ou d'une page tendance X, tirée de son identifiant ; sinon None."""
    try:
        path = urllib.parse.urlsplit(url).path
    except ValueError:
        return None
    if host_of(url) not in X_HOSTS or not (m := X_STATUS.match(path)):
        return None
    ms = (int(m.group(1)) >> 22) + X_EPOCH_MS
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).astimezone(TZ).date()


def meta_content(body: str, name: str) -> str:
    for tag in re.finditer(r"<meta[^>]+>", body):
        if re.search(rf"""(?:property|name)\s*=\s*["']{re.escape(name)}["']""", tag.group(0), re.I):
            if (m := re.search(r'content\s*=\s*"([^"]*)"', tag.group(0)) or
                    re.search(r"content\s*=\s*'([^']*)'", tag.group(0))):
                return html.unescape(m.group(1)).strip()
    return ""


def x_trend_text(body: str) -> dict:
    """Titre et résumé d'une page tendance X, tels que X les rend côté serveur."""
    return {"title": meta_content(body, "og:title"),
            "description": meta_content(body, "og:description") or meta_content(body, "description")}


def is_ai_related(text: str) -> bool:
    if AI_ACRONYMS.search(text) or AI_TERMS.search(text):
        return True
    weak = list(AI_WEAK_TERMS.finditer(text))
    return any(m.group(2) for m in weak) or len({m.group(1).lower() for m in weak}) >= 2


def check_url(url: str) -> dict:
    """ok : page lue ; unverifiable : le site bloque les robots ou ne répond pas ; dead : absente."""
    res = {"url": url, "status": None, "verdict": "dead", "reason": "", "page_date": None}
    if not re.match(r"^https?://[^/\s]+\.[^/\s]+", url or ""):
        res["reason"] = "URL invalide"
        return res
    if host_of(url) in X_HOSTS or host_of(url) == "t.co":
        # Seuls les posts et les pages tendances datées sont acceptés : un profil ou une recherche ne datent rien.
        day = x_post_date(url)
        if day is None:
            res["reason"] = "lien X qui n'est ni un post ni une page tendance (profil, recherche, lien raccourci)"
            return res
        res["page_date"] = day.isoformat()
        trending = is_x_trending(url)
    else:
        trending = False
    for attempt in (1, 2):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                "Accept-Language": "fr,en;q=0.8"})
            with urllib.request.urlopen(req, timeout=25) as resp:
                res["status"] = resp.status
                ctype = resp.headers.get("Content-Type", "")
                body = resp.read(600_000).decode("utf-8", errors="replace") if "html" in ctype else ""
            res["verdict"], res["reason"] = "ok", ""
            if trending:
                res["x_trend"] = x_trend_text(body)
            if not res["page_date"] and (d := extract_page_date(body)):
                res["page_date"] = d.isoformat()
            return res
        except urllib.error.HTTPError as exc:
            res["status"] = exc.code
            if exc.code in BLOCKED_CODES or exc.code >= 500:
                res["verdict"], res["reason"] = "unverifiable", f"HTTP {exc.code}"
            else:
                res["verdict"], res["reason"] = "dead", f"HTTP {exc.code}"
                return res
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, socket.gaierror) and exc.reason.errno == socket.EAI_NONAME:
                res["verdict"], res["reason"] = "dead", "domaine introuvable"
                return res
            res["verdict"], res["reason"] = "unverifiable", f"connexion : {exc.reason}"
        except (TimeoutError, socket.timeout, ssl.SSLError, ConnectionError, OSError,
                http.client.HTTPException) as exc:
            res["verdict"], res["reason"] = "unverifiable", f"connexion : {exc.__class__.__name__}"
        except ValueError as exc:
            res["verdict"], res["reason"] = "dead", f"URL invalide : {exc}"
            return res
        if attempt == 1:
            time.sleep(3)
    return res


def check_urls(urls: set[str]) -> dict[str, dict]:
    log(f"vérification de {len(urls)} liens")
    ordered = sorted(urls)
    with cf.ThreadPoolExecutor(max_workers=12) as pool:
        return dict(zip(ordered, pool.map(check_url, ordered)))


def host_of(url: str) -> str:
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def importance(value) -> int:
    try:
        return max(1, min(5, int(value)))
    except (TypeError, ValueError):
        return 3


def is_x_trending(url: str) -> bool:
    try:
        return host_of(url) in X_HOSTS and bool(X_TRENDING.match(urllib.parse.urlsplit(url).path))
    except ValueError:
        return False


def buzz(value) -> str:
    return value if value in BUZZ_LEVELS else "inconnu"


def in_window(day: dt.date | None, start: dt.date, end: dt.date, before: int, after: int) -> bool:
    return day is None or (start - dt.timedelta(days=before)) <= day <= (end + dt.timedelta(days=after))


def validate_items(results: dict[str, dict], start: dt.date, end: dt.date) -> tuple[list[dict], list[dict], dict]:
    """Vérifie et fusionne les sujets des recherches. Rend (retenus, écartés, statistiques)."""
    raw = [(provider, item) for provider, data in results.items() for item in data.get("items", [])
           if isinstance(item, dict)]
    urls = set()
    for _, item in raw:
        urls.add(str(item.get("url", "")).strip())
        urls.update(str(u).strip() for u in item.get("other_urls", []) or [])
    checks = check_urls({u for u in urls if u})

    def ai_trend(url: str) -> bool:
        """Une page tendance X ne compte que si le script a pu la lire et qu'elle parle d'IA."""
        c = checks.get(url) or {}
        trend = c.get("x_trend") or {}
        return c.get("verdict") == "ok" and is_ai_related(f"{trend.get('title', '')} {trend.get('description', '')}")

    off_topic = sorted(u for u in checks if is_x_trending(u) and checks[u]["verdict"] != "dead" and not ai_trend(u))

    def usable(url: str) -> bool:
        c = checks.get(url)
        if not c or c["verdict"] == "dead":
            return False
        if is_x_trending(url) and not ai_trend(url):
            return False
        return in_window(parse_day(c["page_date"] or ""), start, end, before=3, after=1)

    merged: dict[str, dict] = {}
    rejected = []
    for provider, item in raw:
        url = str(item.get("url", "")).strip()
        others = [str(u).strip() for u in item.get("other_urls", []) or [] if str(u).strip()]
        candidates = [u for u in [url, *others] if u and usable(u)]
        # Une page tendance X complète une source, elle ne la remplace pas.
        candidates.sort(key=is_x_trending)
        claimed = parse_day(str(item.get("date", "")))
        reason = None
        if candidates and is_x_trending(candidates[0]):
            reason = "seule source : une page tendance X"
        elif not candidates:
            c = checks.get(url, {})
            if c.get("verdict") == "dead":
                reason = f"lien mort ({c.get('reason')})"
            elif url in off_topic:
                reason = "page tendance X illisible ou sans rapport avec l'IA"
            elif c:
                reason = f"page datée du {c.get('page_date')}, hors période"
            else:
                reason = "aucun lien"
        elif not claimed:
            reason = f"date annoncée illisible ({item.get('date')!r})"
        elif not in_window(claimed, start, end, before=0, after=1):
            reason = f"date annoncée {claimed}, hors période"
        if reason:
            rejected.append({"found_by": provider, "title": item.get("title"), "url": url, "reason": reason})
            continue
        main, extra = candidates[0], candidates[1:]
        level, notes = buzz(item.get("x_buzz")), str(item.get("x_buzz_notes") or "").strip()
        # Le buzz s'appuyait sur des liens X tous écartés : il n'est plus étayé.
        if (any(host_of(u) in X_HOSTS | {"t.co"} for u in [url, *others])
                and not any(host_of(u) in X_HOSTS for u in candidates)):
            level, notes = "inconnu", ""
        key = normalize_url(main)
        entry = merged.get(key)
        if entry is None:
            category = item.get("category") if item.get("category") in CATEGORIES else "usages"
            merged[key] = {
                "title": item.get("title", "").strip(),
                "summary": item.get("summary", "").strip(),
                "category": category,
                "kind": item.get("kind", "fait"),
                "importance": importance(item.get("importance")),
                "date": claimed.isoformat() if claimed else None,
                # Le nom de l'éditeur ne vaut que pour `url` ; si un autre lien le remplace, son domaine.
                "source": item.get("source", "") if main == url else host_of(main),
                "url": main,
                "other_urls": extra,
                "found_by": [provider],
                "link_check": checks[main]["verdict"],
                "x_buzz": level,
                "x_buzz_notes": notes,
            }
        else:
            if provider not in entry["found_by"]:
                entry["found_by"].append(provider)
            if len(item.get("summary", "")) > len(entry["summary"]):
                entry["summary"] = item["summary"].strip()
            entry["importance"] = max(entry["importance"], importance(item.get("importance")))
            if BUZZ_LEVELS.index(level) > BUZZ_LEVELS.index(entry["x_buzz"]):
                entry["x_buzz"] = level
            if len(notes) > len(entry["x_buzz_notes"]):
                entry["x_buzz_notes"] = notes
            for u in extra:
                if normalize_url(u) not in {normalize_url(x) for x in [entry["url"], *entry["other_urls"]]}:
                    entry["other_urls"].append(u)
    kept = sorted(merged.values(), key=lambda e: (-len(e["found_by"]), -e["importance"], e["category"]))
    for entry in kept:
        # Titre et résumé des pages tendances, lus par le script : la synthèse s'appuie dessus pour décrire le buzz.
        entry["x_trends"] = [{"url": u, **checks[u]["x_trend"]} for u in [entry["url"], *entry["other_urls"]]
                             if is_x_trending(u) and checks.get(u, {}).get("x_trend")]
    for i, entry in enumerate(kept, 1):
        entry["id"] = f"S{i}"
    stats = {
        "per_provider": {p: len(d.get("items", [])) for p, d in results.items()},
        "kept": len(kept),
        "both": sum(1 for e in kept if len(e["found_by"]) > 1),
        "unverifiable": sum(1 for e in kept if e["link_check"] == "unverifiable"),
        "rejected": len(rejected),
        "x_buzz": sum(1 for e in kept if e["x_buzz"] == "fort"),
        "x_posts": sum(1 for e in kept if any(host_of(u) in X_HOSTS for u in [e["url"], *e["other_urls"]])),
        "x_trends": len({t["url"] for e in kept for t in e["x_trends"]}),
        "x_trends_off_topic": len(off_topic),
    }
    return kept, rejected, stats


def validate_impact(results: dict[str, dict], start: dt.date, end: dt.date,
                    archived: dict[str, str] | None = None) -> tuple[list[dict], list[dict], dict]:
    """Vérifie les preuves de la recherche d'impact ; un sujet sans preuve valide est écarté."""
    def dicts(values) -> list[dict]:
        return [v for v in values if isinstance(v, dict)] if isinstance(values, list) else []

    urls = {str(ev.get("url", "")).strip() for data in results.values()
            for t in dicts(data.get("topics")) for ev in dicts(t.get("evidence"))}
    checks = check_urls({u for u in urls if u})
    kept, rejected = [], []
    for provider, data in results.items():
        for topic in dicts(data.get("topics")):
            evidence = []
            for ev in dicts(topic.get("evidence")):
                url = str(ev.get("url", "")).strip()
                c = checks.get(url)
                claimed = parse_day(str(ev.get("date", "")))
                # Une page tendance X atteste le bruit d'une époque, jamais l'impact. Elle doit parler d'IA :
                # archivée par un passage précédent (déjà filtrée), ou relue maintenant.
                if is_x_trending(url):
                    trend = (c or {}).get("x_trend") or {}
                    if ev.get("role") != "perception":
                        continue
                    day = parse_day((archived or {}).get(normalize_url(url), ""))
                    if day:
                        if claimed and in_window(day, start, end, before=60, after=1):
                            evidence.append({**ev, "date": day.isoformat(), "link_check": "archived"})
                        continue
                    if not is_ai_related(f"{trend.get('title', '')} {trend.get('description', '')}"):
                        continue
                if (c and c["verdict"] != "dead" and claimed
                        and in_window(parse_day(c["page_date"] or ""), start, end, before=60, after=1)
                        and in_window(claimed, start, end, before=60, after=1)):
                    evidence.append({**ev, "link_check": c["verdict"]})
            # Un verdict d'impact sans preuve d'impact valide n'est pas étayé.
            if any(ev.get("role") == "impact" for ev in evidence):
                kept.append({**topic, "evidence": evidence, "found_by": provider})
            else:
                rejected.append({"found_by": provider, "topic": topic.get("topic"),
                                 "reason": "aucune preuve d'impact valide"})
    stats = {
        "per_provider": {p: len(d.get("topics", [])) for p, d in results.items()},
        "kept": len(kept),
        "both": 0,
        "unverifiable": sum(1 for t in kept for ev in t["evidence"] if ev["link_check"] == "unverifiable"),
        "rejected": len(rejected),
    }
    return kept, rejected, stats


URL_TOKEN = re.compile(r"""https?://[^\s<>"'\[\]]+""")


def markdown_urls(md: str) -> list[str]:
    """Toutes les URL du texte, quelle que soit leur forme (lien, lien avec titre, autolien, URL nue)."""
    urls = []
    for m in URL_TOKEN.finditer(md):
        token = m.group(0)
        if md[max(0, m.start() - 2):m.start()] == "](":
            # Destination d'un lien : elle s'arrête à la parenthèse qui ferme le lien, ponctuation comprise.
            depth = 0
            for i, ch in enumerate(token):
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    if depth == 0:
                        token = token[:i]
                        break
                    depth -= 1
        else:  # URL nue ou autolien : la ponctuation et la parenthèse finales appartiennent à la phrase
            token = token.rstrip(".,;:!?*_")
            while token.endswith(")") and token.count(")") > token.count("("):
                token = token[:-1].rstrip(".,;:!?*_")
        urls.append(token)
    return urls


def check_markdown(md: str, allowed: set[str]) -> list[str]:
    problems = []
    allowed_norm = {normalize_url(u) for u in allowed}
    urls = markdown_urls(md)
    for u in urls:
        if normalize_url(u) not in allowed_norm:
            problems.append(f"lien absent des entrées : {u}")
    if len(urls) < 5:
        problems.append(f"seulement {len(urls)} liens : chaque information doit citer sa source")
    if re.search(r"^# ", md, re.M):
        problems.append("titre de niveau 1 interdit (l'en-tête est ajouté par le script)")
    if emoji := sorted(set(EMOJI.findall(md))):
        problems.append(f"emoji interdit : {' '.join(emoji)}")
    return problems


# --------------------------------------------------------------------------- éditions publiées


def read_front_matter(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^<!--\n(.*?)\n-->\n(.*)$", text, re.S)
    if not m:
        m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if not m:
        return {}, text
    meta = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    return meta, m.group(2)


def editions_overlapping(kind: str, start: dt.date, end: dt.date) -> list[tuple[Path, dict, str]]:
    found = []
    for path in sorted((ROOT / kind).glob("*.md")):
        meta, body = read_front_matter(path)
        s, e = parse_day(meta.get("period_start", "")), parse_day(meta.get("period_end", ""))
        if s and e and s <= end and e >= start:
            found.append((path, meta, body))
    return found


def uncovered_ranges(start: dt.date, end: dt.date, covered: list[tuple[dt.date, dt.date]]) -> list[tuple[dt.date, dt.date]]:
    ranges, day, current = [], start, None
    while day <= end:
        if any(s <= day <= e for s, e in covered):
            if current:
                ranges.append(current)
                current = None
        else:
            current = (current[0], day) if current else (day, day)
        day += dt.timedelta(days=1)
    if current:
        ranges.append(current)
    return ranges


def strip_method_section(body: str) -> str:
    article = re.split(r"\n---\n\n## Méthode\n", body)[0]
    return re.sub(r"(?m)^\[\^generation-ia\]: [^\n]*\n\n", "", article).strip()


# --------------------------------------------------------------------------- écriture


def header(ed: Edition, title: str, subtitle: str) -> str:
    return (
        "<!--\n"
        f"type: {ed.kind}\n"
        f"newsletter: {ed.name}\n"
        f"published: {ed.published.isoformat()}\n"
        f"period_start: {ed.start.isoformat()}\n"
        f"period_end: {ed.end.isoformat()}\n"
        "language: fr\n"
        "generated_by_ai: true\n"
        "-->\n\n"
        f"# {title}\n\n"
        f"*{subtitle}*[^generation-ia]\n\n"
        "[^generation-ia]: **Texte généré automatiquement par IA**, sans relecture humaine systématique : deux assistants "
        "ont mené des recherches indépendantes, un script a vérifié les liens, un assistant a rédigé la "
        "synthèse. Chaque information renvoie à sa source : vérifiez-la avant de la reprendre. "
        f"Une erreur ? [Ouvrez une issue]({REPO_URL}/issues).\n\n"
    )


def method_section(ed: Edition, stats_by_step: dict[str, dict], writer: str, missing: list[str], extra: str = "") -> str:
    version = git("rev-parse", "--short", "HEAD", check=False) or "inconnue"
    raw_rel = f"../raw/{ed.kind}/{ed.published.isoformat()}/"
    lines = ["", "---", "", "## Méthode", ""]
    lines.append(f"- Période couverte : {fr_range(ed.start, ed.end)} (heure de Paris).")
    for step, st in stats_by_step.items():
        per = ", ".join(f"{p} ({MODEL_NAMES[p]}) : {n}" for p, n in st["per_provider"].items())
        lines.append(
            f"- {step} : {per} ; retenus après vérification : {st['kept']}"
            + (f" (dont {st['both']} trouvés par les deux)" if st.get("both") else "")
            + (f", {st['unverifiable']} liens non vérifiables automatiquement (site qui bloque les robots)"
               if st.get("unverifiable") else "")
            + f" ; écartés : {st['rejected']} (lien mort ou hors période).")
        if (tr := st.get("trends")) is not None:
            lines.append(
                f"- Relevé des tendances X, {step.lower()} : "
                + ("en échec, recherche faite sans pistes." if tr.get("error") else
                   f"{tr['kept']} tendance(s) IA vérifiée(s) ({tr['rejected']} écartée(s)), données comme pistes aux"
                   f" recherches ; {tr.get('covered', 0)} rattachée(s) à un sujet sourcé,"
                   f" {tr.get('uncovered', 0)} non rattachée(s)."))
        if st.get("x_buzz") or st.get("x_posts") or st.get("x_trends_off_topic"):
            lines.append(
                f"- Signal X (Twitter), {step.lower()} : {st.get('x_buzz', 0)} sujet(s) jugé(s) très discuté(s)"
                f" par les recherches (estimation, pas une mesure), {st.get('x_posts', 0)} citant un lien X ;"
                f" pages tendances X retenues : {st.get('x_trends', 0)}, écartées par le filtre IA :"
                f" {st.get('x_trends_off_topic', 0)}.")
    if missing:
        lines.append(f"- Fournisseur absent pour ce passage : {', '.join(missing)}.")
    if extra:
        lines.append(f"- {extra}")
    lines.append(f"- Rédaction : {writer} ({MODEL_NAMES[writer]}).")
    lines.append(f"- Données brutes et liens écartés : [{raw_rel.strip('./')}]({raw_rel}) ; "
                 f"prompts : [prompts/](../prompts/) (version `{version}`).")
    return "\n".join(lines) + "\n"


def write_atomic(path: Path, text: str) -> None:
    """Écrit puis renomme : un passage interrompu ne laisse pas de fichier tronqué à publier."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def save_json(path: Path, data) -> None:
    write_atomic(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def pick_providers(requested: list[str]) -> tuple[list[str], list[str]]:
    levels = provider_levels()
    usable = [p for p in requested if levels.get(p) != "red"]
    missing = [f"{p} (budget épuisé)" for p in requested if p not in usable]
    if not usable:
        raise PipelineError("tous les fournisseurs sont au niveau red dans agents-usage")
    return usable, missing


# --------------------------------------------------------------------------- éditions


def validate_trends(results: dict[str, dict], start: dt.date, end: dt.date) -> tuple[list[dict], list[dict], dict]:
    """Garde les pages tendances X existantes, datées de la période et qui parlent d'IA."""
    proposed: dict[str, dict] = {}
    for provider, data in results.items():
        for t in data.get("trends", []):
            if not isinstance(t, dict):
                continue
            url = str(t.get("url", "")).strip()
            entry = proposed.setdefault(normalize_url(url), {"url": url, "subject": "", "found_by": []})
            entry["subject"] = entry["subject"] or str(t.get("subject", "")).strip()
            if provider not in entry["found_by"]:
                entry["found_by"].append(provider)
    candidates = {k: e for k, e in proposed.items() if is_x_trending(e["url"])}
    checks = check_urls({e["url"] for e in candidates.values()})
    kept, rejected = [], []
    for key, e in proposed.items():
        c = checks.get(e["url"], {})
        trend = c.get("x_trend") or {}
        day = x_post_date(e["url"])
        reason = None
        if key not in candidates:
            reason = "pas une page tendance X"
        elif c.get("verdict") != "ok":
            reason = f"page illisible ({c.get('reason') or c.get('verdict')})"
        elif not in_window(day, start, end, before=0, after=0):
            reason = f"tendance du {day}, hors période"
        elif not is_ai_related(f"{trend.get('title', '')} {trend.get('description', '')}"):
            reason = "sans rapport avec l'IA"
        if reason:
            rejected.append({**e, "reason": reason})
            continue
        kept.append({"url": e["url"], "date": day.isoformat(), "title": trend.get("title", ""),
                     "description": trend.get("description", ""), "found_by": e["found_by"]})
    kept.sort(key=lambda t: (t["date"], t["url"]))
    for i, t in enumerate(kept, 1):
        t["id"] = f"T{i}"
    stats = {"per_provider": {p: len(d.get("trends", [])) for p, d in results.items()},
             "kept": len(kept), "rejected": len(rejected)}
    return kept, rejected, stats


def scan_trends(ed: Edition, workdir: Path, label: str, providers: list[str], start: dt.date, end: dt.date,
                purpose: str) -> tuple[list[dict], dict]:
    """Relevé des tendances X ; un échec n'arrête pas le passage, la recherche se fait alors sans pistes."""
    prompt = render("trends.md", today=fr_date(today_paris()), period=fr_range(start, end),
                    period_start=start.isoformat(), period_end=end.isoformat(), purpose=purpose)
    try:
        results = research_all(prompt, SCHEMAS / "trends.schema.json", workdir, label, providers)
    except PipelineError as exc:
        log(f"{label} : relevé des tendances abandonné ({exc})")
        return [], {"kept": 0, "rejected": 0, "error": str(exc)}
    for provider, data in results.items():
        save_json(ed.raw_dir / f"{label}.{provider}.json", data)
    kept, rejected, stats = validate_trends(results, start, end)
    save_json(ed.raw_dir / f"{label}.validated.json", {"kept": kept, "rejected": rejected, "stats": stats})
    log(f"{label} : {stats}")
    return kept, stats


def trends_for_prompt(trends: list[dict]) -> str:
    if not trends:
        return "(aucune tendance vérifiée pour cette période)"
    return json.dumps([{k: t[k] for k in ("id", "url", "date", "title", "description")} for t in trends],
                      ensure_ascii=False, indent=1)


def research_step(ed: Edition, workdir: Path, label: str, providers: list[str], start: dt.date, end: dt.date,
                  purpose: str, target: str) -> tuple[list[dict], dict, list[str], list[dict]]:
    """Relevé des tendances, recherche guidée par ces tendances, vérification, contrôle de couverture.

    Rend (sujets retenus, statistiques, fournisseurs en échec, tendances qu'aucun sujet ne couvre)."""
    trends, trend_stats = scan_trends(ed, workdir, f"{label}.trends", providers, start, end, purpose)
    prompt = render(
        "research.md", today=fr_date(today_paris()), period=fr_range(start, end),
        period_start=start.isoformat(), period_end=end.isoformat(), purpose=purpose, target=target,
        trends=trends_for_prompt(trends))
    results = research_all(prompt, SCHEMAS / "research.schema.json", workdir, label, providers)
    for provider, data in results.items():
        save_json(ed.raw_dir / f"{label}.{provider}.json", data)
    kept, rejected, stats = validate_items(results, start, end)
    cited = {normalize_url(u) for e in kept for u in [e["url"], *e["other_urls"]]}
    uncovered = [t for t in trends if normalize_url(t["url"]) not in cited]
    stats["trends"] = {**trend_stats, "covered": len(trends) - len(uncovered), "uncovered": len(uncovered)}
    save_json(ed.raw_dir / f"{label}.validated.json",
              {"kept": kept, "rejected": rejected, "trends_uncovered": uncovered, "stats": stats})
    log(f"{label} : {stats}")
    failed = [p for p in providers if p not in results]
    return kept, stats, [f"{p} (échec)" for p in failed], uncovered


def build_weekly(ed: Edition, workdir: Path, providers: list[str], missing: list[str]) -> list[Path]:
    items, stats, failed, uncovered = research_step(
        ed, workdir, "research", providers, ed.start, ed.end,
        purpose="l'édition hebdomadaire ai-weekly", target="25 à 45")
    if len(items) < 8:
        raise PipelineError(f"seulement {len(items)} sujets retenus : trop peu pour publier")
    synth_prompt = render(
        "synthesis-weekly.md", period=fr_range(ed.start, ed.end), published=fr_date(ed.published),
        items_json=json.dumps(items, ensure_ascii=False, indent=1),
        trends_json=trends_for_prompt(uncovered))
    allowed = {u for e in items for u in [e["url"], *e["other_urls"]]} | {t["url"] for t in uncovered}
    body, writer = synthesize(synth_prompt, workdir, "synthesis", providers, allowed)
    text = (header(ed, f"ai-weekly du {fr_date(ed.published)}", f"Semaine {fr_range(ed.start, ed.end)}")
            + body.strip() + "\n" + method_section(ed, {"Recherches": stats}, writer, missing + failed))
    write_atomic(ed.path, text)
    return [ed.path, ed.raw_dir]


def build_monthly(ed: Edition, workdir: Path, providers: list[str], missing: list[str]) -> list[Path]:
    weeklies = editions_overlapping("weekly", ed.start, ed.end)
    covered = [(parse_day(m["period_start"]), parse_day(m["period_end"])) for _, m, _ in weeklies]
    gaps = uncovered_ranges(ed.start, ed.end, covered)
    gap_items, stats_by_step, failed = [], {}, []
    for i, (gs, ge) in enumerate(gaps, 1):
        days = (ge - gs).days + 1
        items, stats, f, _ = research_step(
            ed, workdir, f"gap{i}", providers, gs, ge,
            purpose=f"l'édition mensuelle ai-monthly de {MOIS[ed.start.month - 1]} {ed.start.year} "
                    "(jours non couverts par les éditions hebdomadaires)",
            target=f"{max(5, min(45, days * 4))} environ")
        gap_items += items
        failed += f
        stats_by_step[f"Recherche complémentaire {fr_range(gs, ge)}"] = stats
    if not weeklies and not gap_items:
        raise PipelineError("aucune édition hebdomadaire ni recherche complémentaire pour ce mois")
    for i, item in enumerate(gap_items, 1):
        item["id"] = f"C{i}"
    weekly_text = "\n\n".join(
        f"=== Édition {p.name} ({m['period_start']} → {m['period_end']}) ===\n{strip_method_section(b)}"
        for p, m, b in weeklies) or "(aucune)"
    allowed = {u for _, _, b in weeklies for u in markdown_urls(strip_method_section(b))}
    allowed |= {u for e in gap_items for u in [e["url"], *e["other_urls"]]}
    month = f"{MOIS[ed.start.month - 1]} {ed.start.year}"
    synth_prompt = render(
        "synthesis-monthly.md", month=month, period_start=ed.start.isoformat(), period_end=ed.end.isoformat(),
        weeklies=weekly_text, gap_json=json.dumps(gap_items, ensure_ascii=False, indent=1) if gap_items else "[]")
    body, writer = synthesize(synth_prompt, workdir, "synthesis", providers, allowed)
    extra = (f"Éditions hebdomadaires utilisées : {', '.join(p.name for p, _, _ in weeklies)}."
             if weeklies else "Aucune édition hebdomadaire sur ce mois.")
    save_json(ed.raw_dir / "inputs.json", {
        "weeklies": [p.name for p, _, _ in weeklies],
        "gaps": [[s.isoformat(), e.isoformat()] for s, e in gaps]})
    text = (header(ed, f"ai-monthly de {month}", f"Synthèse du mois, publiée le {fr_date(ed.published)}")
            + body.strip() + "\n" + method_section(ed, stats_by_step, writer, missing + failed, extra))
    write_atomic(ed.path, text)
    return [ed.path, ed.raw_dir]


def archived_trends(start: dt.date, end: dt.date) -> list[dict]:
    """Tendances X vérifiées lors des passages précédents (raw/), datées de [start, end], sans doublon."""
    found: dict[str, dict] = {}
    for path in sorted((ROOT / "raw").glob("*/*/*trends.validated.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for t in data.get("kept", []):
            day = parse_day(str(t.get("date", "")))
            if isinstance(t, dict) and t.get("url") and day and start <= day <= end:
                found.setdefault(normalize_url(t["url"]), t)
    return sorted(found.values(), key=lambda t: (t["date"], t["url"]))


def build_yearly(ed: Edition, workdir: Path, providers: list[str], missing: list[str]) -> list[Path]:
    monthlies = editions_overlapping("monthly", ed.start, ed.end)
    monthly_periods = [(parse_day(m["period_start"]), parse_day(m["period_end"])) for _, m, _ in monthlies]
    # Un hebdo n'est repris que s'il couvre des jours de l'année qu'aucun mensuel ne couvre.
    weeklies = [w for w in editions_overlapping("weekly", ed.start, ed.end)
                if uncovered_ranges(max(parse_day(w[1]["period_start"]), ed.start),
                                    min(parse_day(w[1]["period_end"]), ed.end), monthly_periods)]
    archive = "\n\n".join(
        f"=== {p.parent.name}/{p.name} ({m['period_start']} → {m['period_end']}) ===\n{strip_method_section(b)}"
        for p, m, b in [*monthlies, *weeklies]) or "(aucune archive)"
    prev = Edition("yearly", dt.date(ed.published.year - 1, 12, 20))
    previous = strip_method_section(read_front_matter(prev.path)[1]) if prev.path.exists() else ""
    trends = archived_trends(ed.start, ed.end)
    trends_text = "\n".join(f"- {t['date']} · {t.get('title', '')} · {t['url']}" for t in trends) or "(aucune)"
    research_prompt = render(
        "research-yearly.md", year=str(ed.published.year), today=fr_date(today_paris()),
        start_note=NEWSLETTER_START_FR, archive=archive, trends=trends_text,
        previous=previous or "(pas de rétrospective l'année précédente)")
    results = research_all(research_prompt, SCHEMAS / "impact.schema.json", workdir, "impact", providers)
    for provider, data in results.items():
        save_json(ed.raw_dir / f"impact.{provider}.json", data)
    topics, rejected, stats = validate_impact(results, ed.start, ed.end,
                                              {normalize_url(t["url"]): t["date"] for t in trends})
    save_json(ed.raw_dir / "impact.validated.json", {"kept": topics, "rejected": rejected, "stats": stats})
    if len(topics) < 6:
        raise PipelineError(f"seulement {len(topics)} sujets d'impact retenus : trop peu pour publier")
    allowed = {u for _, _, b in [*monthlies, *weeklies] for u in markdown_urls(strip_method_section(b))}
    allowed |= {ev["url"] for t in topics for ev in t["evidence"]}
    allowed |= set(markdown_urls(previous))
    synth_prompt = render(
        "synthesis-yearly.md", year=str(ed.published.year), prev_year=str(ed.published.year - 1),
        next_year=str(ed.published.year + 1), archive=archive,
        impact_json=json.dumps(topics, ensure_ascii=False, indent=1),
        previous=previous or "(pas de rétrospective l'année précédente)")
    body, writer = synthesize(synth_prompt, workdir, "synthesis", providers, allowed)
    failed = [f"{p} (échec)" for p in providers if p not in results]
    extra = (f"Archives utilisées : {len(monthlies)} édition(s) mensuelle(s), {len(weeklies)} hebdomadaire(s)"
             + f", {len(trends)} tendance(s) X archivée(s)"
             + ("" if prev.path.exists() else " ; pas de rétrospective l'année précédente") + ".")
    text = (header(ed, f"ai-yearly, rétrospective {ed.published.year}",
                   f"L'année {ed.published.year} en IA, publiée le {fr_date(ed.published)}")
            + body.strip() + "\n" + method_section(ed, {"Recherche d'impact": stats}, writer, missing + failed, extra))
    write_atomic(ed.path, text)
    return [ed.path, ed.raw_dir]


BUILDERS = {"weekly": build_weekly, "monthly": build_monthly, "yearly": build_yearly}


def pull_rebase() -> None:
    try:
        git("pull", "--rebase", "--autostash", "origin", "main")
    except PipelineError:
        git("rebase", "--abort", check=False)
        raise


def on_remote(ed: Edition) -> bool:
    rel = ed.path.relative_to(ROOT).as_posix()
    return subprocess.run(["git", "cat-file", "-e", f"origin/main:{rel}"], cwd=ROOT,
                          capture_output=True).returncode == 0


def publish(ed: Edition, paths: list[Path]) -> None:
    """Commite les fichiers de l'édition et pousse sur main ; reprend une publication interrompue."""
    rel = [str(p.relative_to(ROOT)) for p in paths if p.exists()]
    git("add", "--", *rel)
    if git("diff", "--cached", "--name-only", "--", *rel):
        git("commit", "-m", f"docs({ed.kind}): {ed.name} {ed.published.isoformat()}", "--", *rel)
    pull_rebase()
    # Chaque commit sortant ne doit qu'ajouter des fichiers d'édition : rien d'autre ne part sur main.
    changes = git("log", "--name-status", "--format=", "origin/main..HEAD").splitlines()
    foreign = [c for c in changes if c.strip() and not (c.startswith("A\t") and c[2:].startswith(PUBLISHABLE))]
    if foreign:
        raise PipelineError(f"commits locaux hors ajout d'éditions ({'; '.join(foreign[:5])}) : publication refusée")
    if git("rev-list", "origin/main..HEAD"):
        git("push", "origin", "HEAD:main")
    log(f"publié : {REPO_URL}/blob/main/{ed.path.relative_to(ROOT)}")


def produce(ed: Edition, requested: list[str], push: bool, force: bool) -> Path:
    if ed.path.exists() and not force:
        if push:  # écrite par un passage dont la publication a échoué : on la publie sans la refaire
            log(f"{ed.path.relative_to(ROOT)} existe déjà : publication")
            publish(ed, [ed.path, ed.raw_dir])
        else:
            log(f"{ed.path.relative_to(ROOT)} existe déjà : rien à faire")
        return ed.path
    providers, missing = pick_providers(requested)
    stamp = dt.datetime.now(TZ).strftime("%Y%m%dT%H%M%S")
    workdir = RUNS / f"{ed.kind}-{ed.published.isoformat()}-{stamp}"
    workdir.mkdir(parents=True, exist_ok=True)
    log(f"début {ed} avec {', '.join(providers)} (travail : runs/{workdir.name})")
    paths = BUILDERS[ed.kind](ed, workdir, providers, missing)
    log(f"écrit : {ed.path.relative_to(ROOT)}")
    if not push:
        log("--no-push : fichiers écrits, ni commit ni push")
        return ed.path
    publish(ed, paths)
    return ed.path


# --------------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p_due = sub.add_parser("due", help="produit les éditions attendues aujourd'hui (tâche quotidienne)")
    p_due.add_argument("--today", type=dt.date.fromisoformat, default=None)
    p_due.add_argument("--dry-run", action="store_true")
    p_due.add_argument("--no-push", action="store_true")
    for kind in BUILDERS:
        p = sub.add_parser(kind, help=f"produit une édition {kind}")
        p.add_argument("--date", type=dt.date.fromisoformat, default=None, help="date de publication")
        p.add_argument("--no-push", action="store_true")
        p.add_argument("--force", action="store_true", help="régénère même si le fichier existe")
        p.add_argument("--providers", default=",".join(PROVIDERS))
    args = parser.parse_args(argv)
    if getattr(args, "force", False) and not args.no_push:
        parser.error("--force exige --no-push : une édition publiée ne se réécrit pas")

    RUNS.mkdir(exist_ok=True)
    lock = open(RUNS / ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Un autre passage est en cours : abandon.", file=sys.stderr)
        return 3

    try:
        if args.command == "due":
            global _deadline
            today = args.today or today_paris()
            push = not args.no_push and not args.dry_run
            if push:
                _deadline = time.monotonic() + RUN_BUDGET
                pull_rebase()
            editions = due_editions(today, on_remote if push else None)
            if not editions:
                print(f"RIEN À FAIRE ({today.isoformat()})")
                return 0
            log("à produire : " + " ; ".join(map(repr, editions)))
            if args.dry_run:
                return 0
            done, failures = [], []
            for ed in editions:  # un échec n'empêche pas les éditions suivantes
                try:
                    produce(ed, list(PROVIDERS), push=push, force=False)
                    done.append(str(ed.path.relative_to(ROOT)))
                except PipelineError as exc:
                    failures.append(f"{ed.name} {ed.published} : {exc}")
                    log(f"échec {ed} : {exc}")
            if done:
                print("PUBLIÉ : " + " ; ".join(done))
            if failures:
                print("ÉCHEC : " + " ; ".join(failures), file=sys.stderr)
                return 1
            return 0
        providers = [p.strip() for p in args.providers.split(",") if p.strip() in PROVIDERS]
        ed = Edition(args.command, args.date) if args.date else default_edition(args.command, today_paris())
        if not args.no_push:
            pull_rebase()
        produce(ed, providers, push=not args.no_push, force=args.force)
        print(f"PUBLIÉ : {ed.path.relative_to(ROOT)}")
        return 0
    except PipelineError as exc:
        print(f"ÉCHEC : {exc}", file=sys.stderr)
        return 1
    finally:
        lock.close()


if __name__ == "__main__":
    sys.exit(main())

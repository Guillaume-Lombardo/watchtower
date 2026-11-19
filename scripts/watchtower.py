#!/usr/bin/env python3
"""Pipeline de la newsletter watchtower : ai-weekly, ai-monthly, ai-yearly.

Étapes d'un passage :
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

CODEX_MODEL = "gpt-6.1-sol"
CODEX_EFFORT = "high"
CLAUDE_MODEL = "claude-opus-5-5"
CLAUDE_EFFORT = "high"
PROVIDERS = ("codex", "claude")
RESEARCH_TIMEOUT = 60 * 60
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
TRACKING_PARAMS = re.compile(r"^(utm_|fbclid$|gclid$|mc_|ref$|ref_src$|cmpid$|ocid$|guccounter$)")


class PipelineError(RuntimeError):
    pass


_deadline: float | None = None


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
                              env=child_env(), timeout=call_timeout(RESEARCH_TIMEOUT if search else SYNTHESIS_TIMEOUT))
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
                          timeout=call_timeout(RESEARCH_TIMEOUT if search else SYNTHESIS_TIMEOUT))
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
    key = "topics" if schema.name == "impact.schema.json" else "items"

    def one(provider: str) -> dict:
        last = None
        for attempt in (1, 2):
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
                md = strip_md_fence(RUNNERS[provider](current, workdir, f"{label}-{attempt}", None, False))
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
                + "\nRecommence en n'utilisant que les URL fournies dans l'entrée, recopiées à l'identique."
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


def check_url(url: str) -> dict:
    """ok : page lue ; unverifiable : le site bloque les robots ou ne répond pas ; dead : absente."""
    res = {"url": url, "status": None, "verdict": "dead", "reason": "", "page_date": None}
    if not re.match(r"^https?://[^/\s]+\.[^/\s]+", url or ""):
        res["reason"] = "URL invalide"
        return res
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
            if (d := extract_page_date(body)):
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
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def importance(value) -> int:
    try:
        return max(1, min(5, int(value)))
    except (TypeError, ValueError):
        return 3


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

    def usable(url: str) -> bool:
        c = checks.get(url)
        if not c or c["verdict"] == "dead":
            return False
        return in_window(parse_day(c["page_date"] or ""), start, end, before=3, after=1)

    merged: dict[str, dict] = {}
    rejected = []
    for provider, item in raw:
        url = str(item.get("url", "")).strip()
        others = [str(u).strip() for u in item.get("other_urls", []) or [] if str(u).strip()]
        candidates = [u for u in [url, *others] if u and usable(u)]
        claimed = parse_day(str(item.get("date", "")))
        reason = None
        if not candidates:
            c = checks.get(url, {})
            if c.get("verdict") == "dead":
                reason = f"lien mort ({c.get('reason')})"
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
            }
        else:
            if provider not in entry["found_by"]:
                entry["found_by"].append(provider)
            if len(item.get("summary", "")) > len(entry["summary"]):
                entry["summary"] = item["summary"].strip()
            entry["importance"] = max(entry["importance"], importance(item.get("importance")))
            for u in extra:
                if normalize_url(u) not in {normalize_url(x) for x in [entry["url"], *entry["other_urls"]]}:
                    entry["other_urls"].append(u)
    kept = sorted(merged.values(), key=lambda e: (-len(e["found_by"]), -e["importance"], e["category"]))
    for i, entry in enumerate(kept, 1):
        entry["id"] = f"S{i}"
    stats = {
        "per_provider": {p: len(d.get("items", [])) for p, d in results.items()},
        "kept": len(kept),
        "both": sum(1 for e in kept if len(e["found_by"]) > 1),
        "unverifiable": sum(1 for e in kept if e["link_check"] == "unverifiable"),
        "rejected": len(rejected),
    }
    return kept, rejected, stats


def validate_impact(results: dict[str, dict], start: dt.date, end: dt.date) -> tuple[list[dict], list[dict], dict]:
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
                c = checks.get(str(ev.get("url", "")).strip())
                claimed = parse_day(str(ev.get("date", "")))
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
    return re.split(r"\n---\n\n## Méthode\n", body)[0].strip()


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
    if missing:
        lines.append(f"- Fournisseur absent pour ce passage : {', '.join(missing)}.")
    if extra:
        lines.append(f"- {extra}")
    lines.append(f"- Rédaction : {writer} ({MODEL_NAMES[writer]}).")
    lines.append(f"- Données brutes et liens écartés : [{raw_rel.strip('./')}]({raw_rel}) ; "
                 f"prompts : [prompts/](../prompts/) (version `{version}`).")
    return ("\n".join(lines) + "\n\n"
        "[^generation-ia]: **Texte généré automatiquement par IA**, sans relecture humaine systématique : deux assistants "
        "ont mené des recherches indépendantes, un script a vérifié les liens, un assistant a rédigé la "
        "synthèse. Chaque information renvoie à sa source : vérifiez-la avant de la reprendre. "
        f"Une erreur ? [Ouvrez une issue]({REPO_URL}/issues).\n"
    )


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


def research_step(ed: Edition, workdir: Path, label: str, prompt: str, providers: list[str],
                  start: dt.date, end: dt.date) -> tuple[list[dict], dict, list[str]]:
    results = research_all(prompt, SCHEMAS / "research.schema.json", workdir, label, providers)
    for provider, data in results.items():
        save_json(ed.raw_dir / f"{label}.{provider}.json", data)
    kept, rejected, stats = validate_items(results, start, end)
    save_json(ed.raw_dir / f"{label}.validated.json", {"kept": kept, "rejected": rejected, "stats": stats})
    log(f"{label} : {stats}")
    failed = [p for p in providers if p not in results]
    return kept, stats, [f"{p} (échec)" for p in failed]


def build_weekly(ed: Edition, workdir: Path, providers: list[str], missing: list[str]) -> list[Path]:
    prompt = render(
        "research.md", today=fr_date(today_paris()), period=fr_range(ed.start, ed.end),
        period_start=ed.start.isoformat(), period_end=ed.end.isoformat(),
        purpose="l'édition hebdomadaire ai-weekly", target="25 à 45")
    items, stats, failed = research_step(ed, workdir, "research", prompt, providers, ed.start, ed.end)
    if len(items) < 8:
        raise PipelineError(f"seulement {len(items)} sujets retenus : trop peu pour publier")
    synth_prompt = render(
        "synthesis-weekly.md", period=fr_range(ed.start, ed.end), published=fr_date(ed.published),
        items_json=json.dumps(items, ensure_ascii=False, indent=1))
    allowed = {u for e in items for u in [e["url"], *e["other_urls"]]}
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
        prompt = render(
            "research.md", today=fr_date(today_paris()), period=fr_range(gs, ge),
            period_start=gs.isoformat(), period_end=ge.isoformat(),
            purpose=f"l'édition mensuelle ai-monthly de {MOIS[ed.start.month - 1]} {ed.start.year} "
                    "(jours non couverts par les éditions hebdomadaires)",
            target=f"{max(5, min(45, days * 4))} environ")
        items, stats, f = research_step(ed, workdir, f"gap{i}", prompt, providers, gs, ge)
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
    text = (header(ed, f"ai-monthly — {month}", f"Synthèse du mois, publiée le {fr_date(ed.published)}")
            + body.strip() + "\n" + method_section(ed, stats_by_step, writer, missing + failed, extra))
    write_atomic(ed.path, text)
    return [ed.path, ed.raw_dir]


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
    research_prompt = render(
        "research-yearly.md", year=str(ed.published.year), today=fr_date(today_paris()),
        start_note=NEWSLETTER_START_FR, archive=archive,
        previous=previous or "(pas de rétrospective l'année précédente)")
    results = research_all(research_prompt, SCHEMAS / "impact.schema.json", workdir, "impact", providers)
    for provider, data in results.items():
        save_json(ed.raw_dir / f"impact.{provider}.json", data)
    topics, rejected, stats = validate_impact(results, ed.start, ed.end)
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
             + ("" if prev.path.exists() else " ; pas de rétrospective l'année précédente") + ".")
    text = (header(ed, f"ai-yearly — rétrospective {ed.published.year}",
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

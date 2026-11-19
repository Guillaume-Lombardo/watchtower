"""Passages complets avec de faux modèles : aucun appel réseau ni CLI."""
import datetime as dt
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import watchtower as wt  # noqa: E402

REAL_ROOT = Path(__file__).resolve().parent.parent
D = dt.date


def fake_item(i, day):
    return {"title": f"Sujet {i}", "summary": "Résumé.", "category": "modeles", "kind": "fait",
            "date": day.isoformat(), "source": "Ex", "url": f"https://news.example/{day}/{i}",
            "other_urls": [], "importance": 3}


def fake_runner(provider):
    def run(prompt, workdir, label, schema, search):
        if schema and schema.name == "research.schema.json":
            start = D.fromisoformat(wt.re.search(r"\((\d{4}-\d{2}-\d{2}) →", prompt).group(1))
            return json.dumps({"items": [fake_item(f"{provider}{i}", start) for i in range(10)], "notes": ""})
        if schema and schema.name == "impact.schema.json":
            topics = [{"topic": f"T{i}", "category": "modeles", "when": "2026-05", "initial_perception": "p",
                       "hype": 4, "actual_impact": "a", "impact": 2, "verdict": "surestime",
                       "evidence": [{"claim": "c", "url": f"https://news.example/impact/{provider}{i}",
                                     "source": "Ex", "date": "2026-05-01", "role": "impact"}]} for i in range(8)]
            return json.dumps({"topics": topics, "notes": ""})
        urls = sorted(set(wt.re.findall(r"https://news\.example/[^\s\"'`)]+", prompt)))[:8]
        return "## L'essentiel\n" + "\n".join(f"- Fait. ([Ex]({u}))" for u in urls)
    return run


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for d in ("prompts", "schemas"):
            shutil.copytree(REAL_ROOT / d, self.root / d)
        patches = [
            mock.patch.object(wt, "ROOT", self.root),
            mock.patch.object(wt, "PROMPTS", self.root / "prompts"),
            mock.patch.object(wt, "SCHEMAS", self.root / "schemas"),
            mock.patch.object(wt, "RUNS", self.root / "runs"),
            mock.patch.dict(wt.RUNNERS, {"codex": fake_runner("codex"), "claude": fake_runner("claude")}),
            mock.patch.object(wt, "check_urls", lambda urls: {
                u: {"url": u, "verdict": "ok", "page_date": None, "status": 200, "reason": ""} for u in urls}),
            mock.patch.object(wt, "provider_levels", lambda: {}),
            mock.patch.object(wt, "git", lambda *a, **k: ""),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def produce(self, kind, day):
        return wt.produce(wt.Edition(kind, day), ["codex", "claude"], push=False, force=False)

    def test_weekly_then_monthly_then_yearly(self):
        weekly = self.produce("weekly", D(2026, 11, 23))
        text = weekly.read_text()
        self.assertIn("period_start: 2026-11-16", text)
        self.assertIn("## Méthode", text)
        self.assertTrue((self.root / "raw/weekly/2026-11-23/research.validated.json").exists())

        monthly = self.produce("monthly", D(2026, 12, 1))
        inputs = json.loads((self.root / "raw/monthly/2026-12-01/inputs.json").read_text())
        self.assertEqual(inputs["weeklies"], ["2026-11-23_ai-weekly.md"])
        self.assertEqual(inputs["gaps"], [["2026-11-01", "2026-11-15"], ["2026-11-23", "2026-11-30"]])
        self.assertIn("ai-monthly — novembre 2026", monthly.read_text())

        # Hebdo d'octobre sans mensuel d'octobre : il reste dans les archives de l'année.
        legacy = self.produce("weekly", D(2026, 10, 12))
        legacy.write_text(legacy.read_text().replace("<!--\n", "---\n", 1).replace("\n-->\n", "\n---\n", 1))
        yearly = self.produce("yearly", D(2026, 12, 20))
        text = yearly.read_text()
        self.assertIn("rétrospective 2026", text)
        self.assertIn("1 édition(s) mensuelle(s), 1 hebdomadaire(s)", text)

        for edition in (weekly, monthly, yearly):
            with self.subTest(edition=edition.name):
                text = edition.read_text()
                self.assertTrue(text.startswith("<!--\n"))
                self.assertEqual(text.count("[^generation-ia]"), 2)
                self.assertRegex(text, r"\*\[\^generation-ia\]\n\n\[\^generation-ia\]: \*\*Texte généré")
                _, body = wt.read_front_matter(edition)
                self.assertNotIn(f"{wt.REPO_URL}/issues", wt.strip_method_section(body))

    def test_synthesis_with_unknown_link_is_refused(self):
        def bad(prompt, workdir, label, schema, search):
            return "## L'essentiel\n" + "\n".join(f"- x ([A](https://other.example/{i}))" for i in range(6))
        with mock.patch.dict(wt.RUNNERS, {"codex": bad, "claude": bad}):
            with self.assertRaises(wt.PipelineError):
                wt.synthesize("p", self.root, "s", ["claude", "codex"], {"https://news.example/1"})

    def test_due_tries_every_edition_after_a_failure(self):
        calls = []

        def produce(ed, *a, **k):
            calls.append(ed.kind)
            if ed.kind == "weekly":
                raise wt.PipelineError("panne")
            return ed.path

        with mock.patch.object(wt, "produce", produce):
            code = wt.main(["due", "--today", "2027-03-01", "--no-push"])
        self.assertEqual((code, calls), (1, ["weekly", "monthly"]))

    def test_force_needs_no_push(self):
        with self.assertRaises(SystemExit):
            wt.main(["weekly", "--date", "2026-11-23", "--force"])

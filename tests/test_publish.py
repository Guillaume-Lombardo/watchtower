"""Publication avec un vrai dépôt git (origine nue locale) : reprise après un push raté, garde-fous."""
import datetime as dt
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import watchtower as wt  # noqa: E402
from test_pipeline import REAL_ROOT, fake_runner  # noqa: E402


def sh(cwd, *args):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.origin = base / "origin.git"
        sh(base, "git", "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.root = base / "work"
        sh(base, "git", "clone", "-q", str(self.origin), str(self.root))
        for args in (("user.email", "test@example.com"), ("user.name", "test")):
            sh(self.root, "git", "config", *args)
        for d in ("prompts", "schemas"):
            shutil.copytree(REAL_ROOT / d, self.root / d)
        shutil.copy(REAL_ROOT / ".gitignore", self.root / ".gitignore")
        sh(self.root, "git", "add", "-A")
        sh(self.root, "git", "commit", "-qm", "base")
        sh(self.root, "git", "push", "-q", "origin", "HEAD:main")
        self.research_calls = 0
        runner = fake_runner("codex")

        def counting(prompt, workdir, label, schema, search):
            self.research_calls += bool(search)
            return runner(prompt, workdir, label, schema, search)

        patches = [
            mock.patch.object(wt, "ROOT", self.root),
            mock.patch.object(wt, "PROMPTS", self.root / "prompts"),
            mock.patch.object(wt, "SCHEMAS", self.root / "schemas"),
            mock.patch.object(wt, "RUNS", self.root / "runs"),
            mock.patch.dict(wt.RUNNERS, {"codex": counting, "claude": counting}),
            mock.patch.object(wt, "check_urls", lambda urls: {
                u: {"url": u, "verdict": "ok", "page_date": None, "status": 200, "reason": ""} for u in urls}),
            mock.patch.object(wt, "provider_levels", lambda: {}),
            mock.patch.object(wt, "_deadline", None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def on_origin(self, rel):
        return subprocess.run(["git", "cat-file", "-e", f"main:{rel}"], cwd=self.origin,
                              stderr=subprocess.DEVNULL).returncode == 0

    def test_failed_push_is_resumed_without_new_research(self):
        rel = "weekly/2026-11-23_ai-weekly.md"
        sh(self.root, "git", "remote", "set-url", "--push", "origin", str(self.root / "nowhere.git"))
        self.assertEqual(wt.main(["due", "--today", "2026-11-23"]), 1)
        self.assertFalse(self.on_origin(rel))
        calls = self.research_calls
        self.assertGreater(calls, 0)

        sh(self.root, "git", "remote", "set-url", "--push", "origin", str(self.origin))
        self.assertEqual(wt.main(["due", "--today", "2026-11-24"]), 0)
        self.assertTrue(self.on_origin(rel))
        self.assertEqual(self.research_calls, calls)
        self.assertEqual(sh(self.origin, "git", "log", "-1", "--format=%s", "main"),
                         "docs(weekly): ai-weekly 2026-11-23")
        self.assertEqual(wt.main(["due", "--today", "2026-11-25"]), 0)  # RIEN À FAIRE

    def test_local_change_to_a_published_edition_blocks_the_push(self):
        self.assertEqual(wt.main(["due", "--today", "2026-11-23"]), 0)
        published = self.root / "weekly/2026-11-23_ai-weekly.md"
        published.write_text("réécrit\n")
        sh(self.root, "git", "commit", "-qam", "réécriture")
        self.assertEqual(wt.main(["due", "--today", "2026-11-30"]), 1)
        self.assertFalse(self.on_origin("weekly/2026-11-30_ai-weekly.md"))

    def test_dirty_index_after_a_failed_commit_is_resumed(self):
        self.assertEqual(wt.main(["due", "--today", "2026-11-23", "--no-push"]), 0)
        sh(self.root, "git", "add", "weekly", "raw")  # commit jamais fait
        self.assertEqual(wt.main(["due", "--today", "2026-11-24"]), 0)
        self.assertTrue(self.on_origin("weekly/2026-11-23_ai-weekly.md"))

    def test_local_commit_outside_editions_blocks_the_push(self):
        (self.root / "README.md").write_text("local\n")
        sh(self.root, "git", "add", "README.md")
        sh(self.root, "git", "commit", "-qm", "local")
        self.assertEqual(wt.main(["due", "--today", "2026-11-23"]), 1)
        self.assertFalse(self.on_origin("README.md"))
        self.assertFalse(self.on_origin("weekly/2026-11-23_ai-weekly.md"))


if __name__ == "__main__":
    unittest.main()

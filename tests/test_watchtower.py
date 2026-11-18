import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import watchtower as wt  # noqa: E402

D = dt.date


class EditionTests(unittest.TestCase):
    def test_weekly_period_is_previous_monday_to_sunday(self):
        ed = wt.Edition("weekly", D(2026, 11, 23))
        self.assertEqual((ed.start, ed.end), (D(2026, 11, 16), D(2026, 11, 22)))
        self.assertTrue(str(ed.path).endswith("weekly/2026-11-23_ai-weekly.md"))

    def test_monthly_period_is_previous_month(self):
        ed = wt.Edition("monthly", D(2027, 3, 1))
        self.assertEqual((ed.start, ed.end), (D(2027, 2, 1), D(2027, 2, 28)))
        self.assertTrue(str(ed.path).endswith("monthly/2027-03-01_ai-monthly.md"))

    def test_yearly_period(self):
        ed = wt.Edition("yearly", D(2026, 12, 20))
        self.assertEqual((ed.start, ed.end), (D(2026, 1, 1), D(2026, 12, 19)))
        self.assertTrue(str(ed.path).endswith("yearly/2026-12-20_ai-yearly.md"))

    def test_wrong_publication_day_is_refused(self):
        with self.assertRaises(wt.PipelineError):
            wt.Edition("weekly", D(2026, 11, 24))
        with self.assertRaises(wt.PipelineError):
            wt.Edition("monthly", D(2026, 12, 2))


class DueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patch = mock.patch.object(wt, "ROOT", self.root)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def kinds(self, day):
        return [(e.kind, e.published) for e in wt.due_editions(day)]

    def test_nothing_before_first_edition(self):
        self.assertEqual(self.kinds(D(2026, 11, 17)), [])

    def test_monday_weekly_and_catch_up_during_the_week(self):
        self.assertEqual(self.kinds(D(2026, 11, 23)), [("weekly", D(2026, 11, 23))])
        self.assertEqual(self.kinds(D(2026, 11, 26)), [("weekly", D(2026, 11, 23))])

    def test_nothing_when_weekly_exists(self):
        path = wt.Edition("weekly", D(2026, 11, 23)).path
        path.parent.mkdir(parents=True)
        path.write_text("x")
        self.assertEqual(self.kinds(D(2026, 11, 24)), [])

    def test_weekly_before_monthly_on_a_monday_first(self):
        # Le 1er mars 2027 est un lundi.
        self.assertEqual(self.kinds(D(2027, 3, 1)),
                         [("weekly", D(2027, 3, 1)), ("monthly", D(2027, 3, 1))])

    def test_monthly_catch_up_stops_after_the_7th(self):
        self.assertIn(("monthly", D(2026, 12, 1)), self.kinds(D(2026, 12, 7)))
        self.assertNotIn(("monthly", D(2026, 12, 1)), self.kinds(D(2026, 12, 8)))

    def test_yearly_from_december_20(self):
        self.assertNotIn("yearly", [k for k, _ in self.kinds(D(2026, 12, 19))])
        self.assertIn(("yearly", D(2026, 12, 20)), self.kinds(D(2026, 12, 20)))
        self.assertIn(("yearly", D(2026, 12, 20)), self.kinds(D(2026, 12, 31)))


class HelperTests(unittest.TestCase):
    def test_normalize_url(self):
        self.assertEqual(wt.normalize_url("http://www.Example.com/a/?utm_source=x&id=2#top"),
                         "https://example.com/a?id=2")

    def test_uncovered_ranges(self):
        covered = [(D(2026, 11, 2), D(2026, 11, 8)), (D(2026, 11, 9), D(2026, 11, 15))]
        self.assertEqual(wt.uncovered_ranges(D(2026, 11, 1), D(2026, 11, 30), covered),
                         [(D(2026, 11, 1), D(2026, 11, 1)), (D(2026, 11, 16), D(2026, 11, 30))])

    def test_parse_day_formats(self):
        self.assertEqual(wt.parse_day("2026-11-16T08:00:00Z"), D(2026, 11, 16))
        self.assertEqual(wt.parse_day("16/11/2026"), D(2026, 11, 16))
        self.assertIsNone(wt.parse_day("hier"))

    def test_extract_page_date(self):
        html = '<meta content="2026-11-12T10:00:00+00:00" property="article:published_time">'
        self.assertEqual(wt.extract_page_date(html), D(2026, 11, 12))
        self.assertEqual(wt.extract_page_date('{"datePublished": "2026-11-13"}'), D(2026, 11, 13))

    def test_markdown_check_refuses_unknown_links(self):
        allowed = {f"https://a.example/{i}" for i in range(5)}
        md = "## L'essentiel\n" + "\n".join(f"- x ([A](https://a.example/{i}))" for i in range(5))
        self.assertEqual(wt.check_markdown(md, allowed), [])
        bad = md + "\n- y ([B](https://b.example/z))"
        self.assertTrue(any("b.example" in p for p in wt.check_markdown(bad, allowed)))
        self.assertTrue(wt.check_markdown("# Titre\n" + md, allowed))

    def test_markdown_check_sees_every_link_form(self):
        allowed = {f"https://a.example/{i}" for i in range(5)} | {"https://w.example/wiki/Foo_(bar)"}
        md = "\n".join(f"- x ([A](https://a.example/{i}))" for i in range(5))
        self.assertEqual(wt.check_markdown(md + "\n- w ([W](https://w.example/wiki/Foo_(bar)))", allowed), [])
        for bad in ('[B](https://b.example/z "titre")', "<https://b.example/z>", "[B]: https://b.example/z",
                    "[B](https://a.example:8443/0)", "[B](https://a.example/0!)"):
            self.assertTrue(any("lien absent" in p for p in wt.check_markdown(md + "\n- " + bad, allowed)), bad)

    def test_parse_day_converts_offsets_to_paris(self):
        self.assertEqual(wt.parse_day("2026-11-15T23:30:00-08:00"), D(2026, 11, 16))

    def test_extract_page_date_prefers_publication_meta(self):
        html = ('<meta name="date" content="2026-11-30"> '
                '<meta property="article:published_time" content="2026-11-12T10:00:00+00:00">')
        self.assertEqual(wt.extract_page_date(html), D(2026, 11, 12))
        self.assertEqual(wt.extract_page_date('<meta name="date" content="2026-11-30"> '
                                              '<meta name="DC.date.issued" content="2026-11-12">'), D(2026, 11, 12))

    def test_source_follows_the_kept_link(self):
        item = {"title": "T", "summary": "S", "category": "modeles", "kind": "fait", "date": "2026-11-10",
                "source": "Reuters", "url": "https://reuters.example/dead",
                "other_urls": ["https://www.tc.example/a"], "importance": 4}
        checks = {"https://reuters.example/dead": {"verdict": "dead", "reason": "HTTP 404", "page_date": None},
                  "https://www.tc.example/a": {"verdict": "ok", "page_date": "2026-11-10"}}
        with mock.patch.object(wt, "check_urls", lambda urls: {u: checks[u] for u in urls}):
            kept, _, _ = wt.validate_items({"codex": {"items": [item]}}, D(2026, 11, 9), D(2026, 11, 15))
        self.assertEqual((kept[0]["url"], kept[0]["source"]), ("https://www.tc.example/a", "tc.example"))

    def test_impact_topic_needs_valid_impact_evidence(self):
        ev = {"claim": "c", "url": "https://p.example/1", "source": "P", "date": "2026-03-01", "role": "perception"}
        topic = {"topic": "T", "evidence": [ev, {**ev, "url": "https://i.example/dead", "role": "impact"}]}
        checks = {"https://p.example/1": {"verdict": "ok", "page_date": "2026-03-01"},
                  "https://i.example/dead": {"verdict": "dead", "page_date": None}}
        with mock.patch.object(wt, "check_urls", lambda urls: {u: checks[u] for u in urls}):
            kept, rejected, _ = wt.validate_impact({"codex": {"topics": [topic, None]}},
                                                   D(2026, 1, 1), D(2026, 12, 19))
        self.assertEqual((len(kept), len(rejected)), (0, 1))
        undated = {**ev, "role": "impact", "date": ""}
        with mock.patch.object(wt, "check_urls", lambda urls: {u: checks[u] for u in urls}):
            kept, _, _ = wt.validate_impact({"codex": {"topics": [{"topic": "T", "evidence": [undated]}]}},
                                            D(2026, 1, 1), D(2026, 12, 19))
        self.assertEqual(kept, [])

    def test_validate_items_merges_and_rejects(self):
        start, end = D(2026, 11, 9), D(2026, 11, 15)
        item = {"title": "T", "summary": "S", "category": "modeles", "kind": "fait", "date": "2026-11-10",
                "source": "X", "url": "https://ok.example/a", "other_urls": [], "importance": 4}
        results = {
            "codex": {"items": [item, {**item, "url": "https://dead.example/b"}]},
            "claude": {"items": [{**item, "url": "https://www.ok.example/a/"},
                                 {**item, "url": "https://ok.example/old", "date": "2026-10-01"},
                                 {**item, "url": "https://ok.example/a", "date": "hier"}]},
        }
        checks = {
            "https://ok.example/a": {"verdict": "ok", "page_date": "2026-11-10"},
            "https://www.ok.example/a/": {"verdict": "ok", "page_date": "2026-11-10"},
            "https://dead.example/b": {"verdict": "dead", "reason": "HTTP 404", "page_date": None},
            "https://ok.example/old": {"verdict": "ok", "page_date": "2026-10-01"},
        }
        with mock.patch.object(wt, "check_urls", lambda urls: {u: checks[u] for u in urls}):
            kept, rejected, stats = wt.validate_items(results, start, end)
        self.assertEqual(len(kept), 1)
        self.assertEqual(sorted(kept[0]["found_by"]), ["claude", "codex"])
        self.assertEqual(stats["both"], 1)
        self.assertEqual(len(rejected), 3)

    def test_render_refuses_missing_variable(self):
        with self.assertRaises(wt.PipelineError):
            wt.render("synthesis-weekly.md", period="x")


if __name__ == "__main__":
    unittest.main()

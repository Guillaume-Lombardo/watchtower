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


def trend_url(day, n=0):
    """Page tendance X dont l'identifiant encode midi (heure de Paris) du jour donné."""
    ms = int(dt.datetime(day.year, day.month, day.day, 12, tzinfo=wt.TZ).timestamp() * 1000)
    return f"https://x.com/i/trending/{((ms - wt.X_EPOCH_MS) << 22) + n}"


class HelperTests(unittest.TestCase):
    def test_normalize_url(self):
        self.assertEqual(wt.normalize_url("http://www.Example.com/a/?utm_source=x&id=2#top"),
                         "https://example.com/a?id=2")

    def test_normalize_url_drops_x_share_params(self):
        self.assertEqual(wt.normalize_url("https://twitter.com/a/status/123456789?s=20&t=abc"),
                         wt.normalize_url("https://x.com/a/status/123456789"))

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
        self.assertEqual(wt.check_markdown(md + "\n- p [A](https://a.example/0). Fin (voir https://a.example/1).",
                                           allowed), [])
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

    def test_x_post_date_comes_from_the_post_id(self):
        self.assertEqual(wt.x_post_date("https://x.com/elonmusk/status/1585341984679469056"), D(2022, 10, 26))
        self.assertEqual(wt.x_post_date("https://twitter.com/a/status/1585341984679469056?s=20"), D(2022, 10, 26))
        self.assertEqual(wt.x_post_date("https://x.com/i/trending/2122753845734547691"), D(2026, 11, 17))
        self.assertIsNone(wt.x_post_date("https://x.com/OpenAI"))
        self.assertIsNone(wt.x_post_date("https://news.example/a/status/1585341984679469056"))

    def test_x_links_other_than_posts_are_refused_without_fetching(self):
        with mock.patch.object(wt.urllib.request, "urlopen", side_effect=AssertionError("pas de requête")):
            for url in ("https://x.com/OpenAI", "https://x.com/search?q=gpt", "https://x.com/explore/tabs/trending",
                        "https://x.com/i/trending",
                        "https://t.co/abc123"):
                with self.subTest(url=url):
                    self.assertEqual(wt.check_url(url)["verdict"], "dead")

    def test_x_post_keeps_the_date_of_its_id(self):
        class Resp:
            status, headers = 200, {"Content-Type": "text/html"}
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self, n): return b'<meta property="article:published_time" content="2026-11-20">'
        with mock.patch.object(wt.urllib.request, "urlopen", return_value=Resp()):
            res = wt.check_url("https://x.com/elonmusk/status/1585341984679469056")
        self.assertEqual((res["verdict"], res["page_date"]), ("ok", "2022-10-26"))

    def test_x_buzz_is_merged_and_counted(self):
        start, end = D(2026, 11, 9), D(2026, 11, 15)
        item = {"title": "T", "summary": "S", "category": "modeles", "kind": "fait", "date": "2026-11-10",
                "source": "Ex", "url": "https://ok.example/a", "other_urls": ["https://x.com/lab/status/1"],
                "importance": 4, "x_buzz": "moyen", "x_buzz_notes": "Court."}
        results = {"codex": {"items": [item]},
                   "claude": {"items": [{**item, "x_buzz": "fort", "x_buzz_notes": "Plus long, avec détail."},
                                        {**item, "url": "https://ok.example/b", "x_buzz": "énorme"}]}}
        checks = {"https://ok.example/a": {"verdict": "ok", "page_date": "2026-11-10"},
                  "https://ok.example/b": {"verdict": "ok", "page_date": "2026-11-10"},
                  "https://x.com/lab/status/1": {"verdict": "ok", "page_date": "2026-11-10"}}
        with mock.patch.object(wt, "check_urls", lambda urls: {u: checks[u] for u in urls}):
            kept, _, stats = wt.validate_items(results, start, end)
        by_url = {e["url"]: e for e in kept}
        self.assertEqual((by_url["https://ok.example/a"]["x_buzz"], by_url["https://ok.example/a"]["x_buzz_notes"]),
                         ("fort", "Plus long, avec détail."))
        self.assertEqual(by_url["https://ok.example/b"]["x_buzz"], "inconnu")
        self.assertEqual((stats["x_buzz"], stats["x_posts"]), (1, 2))

    def test_x_trending_page_never_becomes_the_main_source(self):
        start, end = D(2026, 11, 16), D(2026, 11, 22)
        trend = "https://x.com/i/trending/2122753845734547691"
        item = {"title": "T", "summary": "S", "category": "modeles", "kind": "fait", "date": "2026-11-17",
                "source": "OpenAI", "url": trend, "other_urls": ["https://ok.example/a"], "importance": 4}
        checks = {trend: {"verdict": "ok", "page_date": "2026-11-17",
                          "x_trend": {"title": "OpenAI Launches GPT-6.2", "description": "New models."}},
                  "https://ok.example/a": {"verdict": "ok", "page_date": "2026-11-17"},
                  "https://dead.example/b": {"verdict": "dead", "reason": "HTTP 404", "page_date": None}}
        results = {"codex": {"items": [item, {**item, "url": "https://dead.example/b", "other_urls": [trend]}]}}
        with mock.patch.object(wt, "check_urls", lambda urls: {u: checks[u] for u in urls}):
            kept, rejected, _ = wt.validate_items(results, start, end)
        self.assertEqual([(e["url"], e["other_urls"], e["source"]) for e in kept],
                         [("https://ok.example/a", [trend], "ok.example")])
        self.assertEqual([r["reason"] for r in rejected], ["seule source : une page tendance X"])
        self.assertEqual(kept[0]["x_trends"], [{"url": trend, "title": "OpenAI Launches GPT-6.2",
                                                "description": "New models."}])

    def test_x_trends_without_ai_are_filtered(self):
        start, end = D(2026, 11, 16), D(2026, 11, 22)
        ai = "https://x.com/i/trending/2122753845734547691"
        off = "https://x.com/i/trending/2122753845734547692"
        blocked = "https://x.com/i/trending/2122753845734547693"
        item = {"title": "T", "summary": "S", "category": "modeles", "kind": "fait", "date": "2026-11-17",
                "source": "Ex", "url": "https://ok.example/a", "other_urls": [ai, off, blocked], "importance": 4}
        checks = {"https://ok.example/a": {"verdict": "ok", "page_date": "2026-11-17"},
                  ai: {"verdict": "ok", "page_date": "2026-11-17",
                       "x_trend": {"title": "Nvidia beats estimates", "description": "Data center sales."}},
                  off: {"verdict": "ok", "page_date": "2026-11-17",
                        "x_trend": {"title": "Real Madrid wins", "description": "Mbappé scores twice."}},
                  blocked: {"verdict": "unverifiable", "page_date": "2026-11-17", "reason": "HTTP 429"}}
        results = {"codex": {"items": [item, {**item, "url": off, "other_urls": []}]}}
        with mock.patch.object(wt, "check_urls", lambda urls: {u: checks[u] for u in urls}):
            kept, rejected, stats = wt.validate_items(results, start, end)
        self.assertEqual(kept[0]["other_urls"], [ai])
        self.assertEqual([t["title"] for t in kept[0]["x_trends"]], ["Nvidia beats estimates"])
        self.assertEqual([r["reason"] for r in rejected], ["page tendance X illisible ou sans rapport avec l'IA"])
        self.assertEqual((stats["x_trends"], stats["x_trends_off_topic"]), (1, 2))

    def test_is_ai_related(self):
        for text in ("OpenAI Launches GPT-6.2 Astra", "Anthropic IPO", "Les GPU de Nvidia", "Une IA générative",
                     "AI agents take over", "Proof of Riemann hypothesis with Claude", "Qwen4-Max released"):
            with self.subTest(text=text):
                self.assertTrue(wt.is_ai_related(text))
        for text in ("J'ai vu le match", "Real Madrid wins", "Election results", "Taylor Swift tour", "ai"):
            with self.subTest(text=text):
                self.assertFalse(wt.is_ai_related(text))

    def test_x_trend_page_text_is_read(self):
        body = ('<title>OpenAI Launches GPT-6.2 / X</title>'
                '<meta property="og:title" content="OpenAI Launches GPT-6.2 &amp; Sol"/>'
                '<meta name="description" content="Short."/>'
                '<meta property="og:description" content="Critics like developer Theo call it verbose; OpenAI&#x27;s team replies."/>')

        class Resp:
            status, headers = 200, {"Content-Type": "text/html"}
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self, n): return body.encode()
        with mock.patch.object(wt.urllib.request, "urlopen", return_value=Resp()):
            res = wt.check_url("https://x.com/i/trending/2122753845734547691")
        self.assertEqual(res["x_trend"], {"title": "OpenAI Launches GPT-6.2 & Sol",
                                          "description": "Critics like developer Theo call it verbose; OpenAI's team replies."})
        self.assertEqual(res["page_date"], "2026-11-17")

    def test_validate_trends(self):
        start, end = D(2026, 11, 16), D(2026, 11, 22)
        ai, off, old = (trend_url(D(2026, 11, 17)), trend_url(D(2026, 11, 18), 1), trend_url(D(2026, 10, 1)))
        dead = trend_url(D(2026, 11, 19), 2)
        results = {"codex": {"trends": [{"url": ai, "subject": "GPT"}, {"url": off, "subject": "Foot"},
                                        {"url": "https://x.com/OpenAI/status/2122750126297161811", "subject": "p"},
                                        {"url": old, "subject": "vieux"}, None]},
                   "claude": {"trends": [{"url": ai + "?s=20", "subject": "GPT bis"}, {"url": dead, "subject": "?"}]}}
        text = {"title": "OpenAI lance GPT-6.2", "description": "Retours mitigés."}
        checks = {ai: {"verdict": "ok", "x_trend": text}, off: {"verdict": "ok", "x_trend": {"title": "Le PSG gagne"}},
                  old: {"verdict": "ok", "x_trend": text}, dead: {"verdict": "dead", "reason": "HTTP 404"}}
        with mock.patch.object(wt, "check_urls", lambda urls: {u: checks[u] for u in urls}):
            kept, rejected, stats = wt.validate_trends(results, start, end)
        self.assertEqual([(t["id"], t["url"], t["date"], t["title"], t["found_by"]) for t in kept],
                         [("T1", ai, "2026-11-17", "OpenAI lance GPT-6.2", ["codex", "claude"])])
        self.assertEqual(sorted(r["reason"] for r in rejected),
                         ["page illisible (HTTP 404)", "pas une page tendance X", "sans rapport avec l'IA",
                          "tendance du 2026-10-01, hors période"])
        self.assertEqual(stats["per_provider"], {"codex": 5, "claude": 2})

    def test_trend_page_is_never_impact_evidence(self):
        trend = trend_url(D(2026, 3, 2))
        ev = {"claim": "c", "url": trend, "source": "X", "date": "2026-03-02", "role": "impact"}
        checks = {trend: {"verdict": "ok", "page_date": "2026-03-02"}}
        with mock.patch.object(wt, "check_urls", lambda urls: {u: checks[u] for u in urls}):
            kept, _, _ = wt.validate_impact({"codex": {"topics": [{"topic": "T", "evidence": [ev]}]}},
                                            D(2026, 1, 1), D(2026, 12, 19))
            self.assertEqual(kept, [])
            kept, _, _ = wt.validate_impact(
                {"codex": {"topics": [{"topic": "T", "evidence": [{**ev, "role": "perception"},
                                                                  {**ev, "url": trend, "role": "impact"}]}]}},
                D(2026, 1, 1), D(2026, 12, 19))
            self.assertEqual(kept, [])

    def test_render_refuses_missing_variable(self):
        with self.assertRaises(wt.PipelineError):
            wt.render("synthesis-weekly.md", period="x")

    def test_synthesis_prompts_include_style(self):
        style = (wt.PROMPTS / "style.md").read_text(encoding="utf-8").strip()
        for name in ("synthesis-weekly.md", "synthesis-monthly.md", "synthesis-yearly.md"):
            with self.subTest(name=name):
                self.assertIn("{{style}}", (wt.PROMPTS / name).read_text(encoding="utf-8"))
        text = wt.render("synthesis-weekly.md", period="p", published="d", items_json="[]", trends_json="[]")
        self.assertIn(style, text)
        self.assertNotIn("{{style}}", text)

    def test_normalize_typography_keeps_urls(self):
        md = ("- **Titre** \u2014 l\u2019annonce, 2026\u20132027 \u2014 suite.\u00a0\u2014 fin "
              "([A](https://a.example/l\u2019x\u2014y))")
        self.assertEqual(wt.normalize_typography(md),
                         "- **Titre** : l'annonce, 2026-2027 - suite. - fin ([A](https://a.example/l\u2019x\u2014y))")

    def test_markdown_check_refuses_emoji(self):
        allowed = {f"https://a.example/{i}" for i in range(5)}
        md = "\n".join(f"- x ([A](https://a.example/{i}))" for i in range(5))
        self.assertEqual(wt.check_markdown(md + "\n- Tests \u2713, note \u2605\u2605, \u2192 \u00d7 2", allowed), [])
        for emoji in ("\U0001F680", "\u2705", "\u26a0\ufe0f", "1\ufe0f\u20e3", "\u2615", "\u26bd"):
            self.assertTrue(any("emoji" in p for p in wt.check_markdown(md + " " + emoji, allowed)), emoji)


if __name__ == "__main__":
    unittest.main()

import tempfile
import unittest
from unittest import mock

from pipeline import sources
from pipeline.run import (
    _added_today,
    _enrich_fulltext_source,
    _load_candidate_cache,
    _remaining_selection_quotas,
    _save_candidate_cache,
    gather,
)
from pipeline.schema import Paper
from pipeline.sources import arxiv


EMPTY_FEED = '<feed xmlns="http://www.w3.org/2005/Atom"></feed>'
VALID_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2601.00001v1</id>
    <title>Reliable Multi-Agent Path Finding</title>
    <summary>Agents find collision-free paths on a shared graph.</summary>
    <published>2026-01-01T00:00:00Z</published>
    <arxiv:comment>Accepted at AAMAS 2026. 12 pages.</arxiv:comment>
    <author><name>A. Researcher</name></author>
    <link title="pdf" href="https://arxiv.org/pdf/2601.00001v1" />
  </entry>
</feed>
"""
SEARCH_HTML = """
<ol>
  <li class="arxiv-result">
    <p class="list-title"><a href="https://arxiv.org/abs/2608.05588v1">arXiv</a></p>
    <p class="title is-5">Reliable <span>Multi-Agent</span> Path Finding</p>
    <p class="authors">Authors: <a>A. Researcher</a>, <a>B. Researcher</a></p>
    <p class="abstract">
      <span class="abstract-full">We plan collision-free warehouse paths. <a>Less</a></span>
    </p>
    <p class="is-size-7"><span>Submitted</span> 6 August, 2026;</p>
    <p class="comments is-size-7"><span>Comments:</span> Accepted to AAAI 2027. 10 pages.</p>
  </li>
</ol>
"""


class SourceResilienceTest(unittest.TestCase):
    @mock.patch.object(arxiv, "_search_html_once", return_value=[])
    @mock.patch.object(arxiv.time, "sleep")
    @mock.patch.object(arxiv, "http_get")
    def test_arxiv_empty_feed_retries_and_recovers(
        self, http_get, sleep, search_html
    ):
        http_get.side_effect = [EMPTY_FEED, EMPTY_FEED, VALID_FEED]

        papers = arxiv.search(["Multi-Agent Path Finding"], limit=40)

        self.assertEqual(http_get.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(papers[0].arxiv_id, "2601.00001")

    @mock.patch.object(arxiv, "_search_html_once", return_value=[])
    @mock.patch.object(arxiv.time, "sleep")
    @mock.patch.object(arxiv, "http_get", return_value=EMPTY_FEED)
    def test_arxiv_repeated_empty_feed_becomes_error(
        self, http_get, sleep, search_html
    ):
        with self.assertRaisesRegex(RuntimeError, "3回試しました"):
            arxiv.search(["MAPF"], limit=40)

        self.assertEqual(http_get.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_source_registry_propagates_errors_to_reporter(self):
        failing_search = mock.Mock(side_effect=RuntimeError("temporary"))
        with mock.patch.dict(sources._SOURCES, {"arxiv": failing_search}):
            with self.assertRaisesRegex(RuntimeError, "temporary"):
                sources.search_source("arxiv", ["MAPF"], 40)

    def test_candidate_cache_round_trip(self):
        recent = [
            Paper(
                source="arxiv",
                title="Cached MAPF Paper",
                abstract="Multi-agent path finding for warehouse robots.",
                arxiv_id="2601.00002",
            )
        ]
        important = [
            Paper(
                source="semanticscholar",
                title="Important MAPD Paper",
                abstract="Multi-agent pickup and delivery tasks.",
                doi="10.1000/mapd",
                citations=100,
            )
        ]

        with tempfile.TemporaryDirectory() as cache_dir:
            _save_candidate_cache("mapf", recent, important, cache_dir=cache_dir)
            loaded_recent, loaded_important = _load_candidate_cache(
                "mapf", cache_dir=cache_dir
            )

        self.assertEqual(loaded_recent[0].arxiv_id, "2601.00002")
        self.assertEqual(loaded_important[0].citations, 100)

    @mock.patch("pipeline.run._find_arxiv_by_title")
    def test_existing_oa_pdf_skips_arxiv_title_lookup(self, find_arxiv):
        paper = Paper(
            source="openalex",
            title="Open access MAPF paper",
            pdf_url="https://example.org/paper.pdf",
        )

        self.assertIs(_enrich_fulltext_source(paper), paper)
        find_arxiv.assert_not_called()

    def test_added_today_counts_only_automatic_papers(self):
        seen = {
            "arxiv:1": {
                "title": "Daily paper",
                "file": "field/1.html",
                "added": "2026-07-28",
                "selection": "recent",
            },
            "arxiv:2": {
                "title": "Manual paper",
                "file": "field/2.html",
                "added": "2026-07-28",
                "selection": "manual",
            },
            "arxiv:3": {
                "title": "Yesterday's paper",
                "file": "field/3.html",
                "added": "2026-07-27",
                "selection": "important",
            },
        }

        added = _added_today(seen, "2026-07-28")

        self.assertEqual([item["title"] for item in added], ["Daily paper"])

    def test_retry_fills_the_missing_selection_slot_only(self):
        self.assertEqual(
            _remaining_selection_quotas(2, [{"selection": "recent"}]),
            (1, 0),
        )
        self.assertEqual(
            _remaining_selection_quotas(2, [{"selection": "important"}]),
            (0, 1),
        )
        self.assertEqual(
            _remaining_selection_quotas(
                2, [{"selection": "important"}, {"selection": "recent"}]
            ),
            (0, 0),
        )

    @mock.patch("pipeline.run.sources.search_source", side_effect=RuntimeError("down"))
    def test_gather_records_source_errors(self, search_source):
        papers, counts = gather(
            {"sources": ["arxiv"], "search_queries": ["MAPF"]},
            offline=False,
            mode="recent",
        )

        self.assertEqual(papers, [])
        self.assertIn("RuntimeError('down')", counts["arxiv/recent/q1"]["error"])

    @mock.patch("pipeline.run.sources.search_source", return_value=[])
    def test_semantic_scholar_uses_one_combined_fallback_query(self, search_source):
        gather(
            {
                "sources": ["semanticscholar"],
                "search_queries": ["Multi-Agent Path Finding", "MAPD"],
            },
            offline=False,
            mode="recent",
        )

        search_source.assert_called_once_with(
            "semanticscholar",
            ["Multi-Agent Path Finding", "MAPD"],
            100,
            mode="recent",
        )

    @mock.patch("pipeline.run.sources.search_source", return_value=[])
    def test_arxiv_important_reuses_recent_pool_without_second_query(
        self, search_source
    ):
        papers, counts = gather(
            {
                "sources": ["arxiv"],
                "search_queries": ["Multi-Agent Path Finding", "MAPD"],
            },
            offline=False,
            mode="important",
        )

        self.assertEqual(papers, [])
        self.assertTrue(counts["arxiv/important"]["reused_recent"])
        search_source.assert_not_called()

    @mock.patch("pipeline.run.sources.search_source", return_value=[])
    def test_arxiv_recent_uses_one_deep_combined_query(self, search_source):
        gather(
            {
                "sources": ["arxiv"],
                "search_queries": ["Multi-Agent Path Finding", "MAPD"],
            },
            offline=False,
            mode="recent",
        )

        search_source.assert_called_once_with(
            "arxiv",
            ["Multi-Agent Path Finding", "MAPD"],
            200,
            mode="recent",
        )

    @mock.patch.object(arxiv, "http_get", return_value=VALID_FEED)
    def test_arxiv_important_search_is_sorted_by_relevance(self, http_get):
        arxiv.search(["Multi-Agent Path Finding"], limit=20, mode="important")

        self.assertIn("sortBy=relevance", http_get.call_args.args[0])

    @mock.patch.object(arxiv, "http_get", return_value=SEARCH_HTML)
    def test_arxiv_html_fallback_extracts_fulltext_metadata(self, http_get):
        papers = arxiv._search_html_once(
            ["Multi-Agent Path Finding"], limit=20, mode="recent"
        )

        self.assertEqual(len(papers), 1)
        self.assertEqual(papers[0].arxiv_id, "2608.05588")
        self.assertEqual(papers[0].published, "2026-08-06")
        self.assertEqual(papers[0].authors, ["A. Researcher", "B. Researcher"])
        self.assertEqual(papers[0].venue, "AAAI 2027")
        self.assertEqual(
            papers[0].pdf_url, "https://arxiv.org/pdf/2608.05588"
        )
        self.assertNotIn("Less", papers[0].abstract)
        self.assertIn("order=-announced_date_first", http_get.call_args.args[0])

    def test_arxiv_comment_requires_explicit_acceptance(self):
        self.assertEqual(
            arxiv._venue_from_comment("Accepted at AAMAS 2026. 12 pages."),
            "AAMAS 2026",
        )
        self.assertEqual(
            arxiv._venue_from_comment("To appear in ICAPS 2027; camera ready."),
            "ICAPS 2027",
        )
        self.assertEqual(arxiv._venue_from_comment("Submitted to AAAI 2027."), "")
        self.assertEqual(arxiv._venue_from_comment("Under review."), "")

    @mock.patch.object(arxiv, "_search_html_once", return_value=[])
    @mock.patch.object(arxiv, "time")
    @mock.patch.object(arxiv, "http_get", return_value=EMPTY_FEED)
    def test_arxiv_search_can_disable_retries(
        self, http_get, time_module, search_html
    ):
        with self.assertRaises(RuntimeError):
            arxiv.search(["Paper title"], limit=5, retries=False)

        http_get.assert_called_once()
        time_module.sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()

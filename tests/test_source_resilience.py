import tempfile
import unittest
from unittest import mock

from pipeline import sources
from pipeline.run import (
    _added_today,
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
    <author><name>A. Researcher</name></author>
    <link title="pdf" href="https://arxiv.org/pdf/2601.00001v1" />
  </entry>
</feed>
"""


class SourceResilienceTest(unittest.TestCase):
    @mock.patch.object(arxiv.time, "sleep")
    @mock.patch.object(arxiv, "http_get")
    def test_arxiv_empty_feed_retries_and_recovers(self, http_get, sleep):
        http_get.side_effect = [EMPTY_FEED, EMPTY_FEED, VALID_FEED]

        papers = arxiv.search(["Multi-Agent Path Finding"], limit=40)

        self.assertEqual(http_get.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(papers[0].arxiv_id, "2601.00001")

    @mock.patch.object(arxiv.time, "sleep")
    @mock.patch.object(arxiv, "http_get", return_value=EMPTY_FEED)
    def test_arxiv_repeated_empty_feed_becomes_error(self, http_get, sleep):
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
            40,
            mode="recent",
        )


if __name__ == "__main__":
    unittest.main()

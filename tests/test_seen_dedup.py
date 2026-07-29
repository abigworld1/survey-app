import unittest

from pipeline.dedup import (
    build_seen_aliases,
    collapse_seen_duplicates,
    paper_is_seen,
)
from pipeline.run import _cacheable_candidates
from pipeline.schema import Paper


class SeenDedupTest(unittest.TestCase):
    def test_doi_candidate_matches_seen_arxiv_record(self):
        seen = {
            "arxiv:1705.10868": {
                "title": "Lifelong Multi-Agent Path Finding for Online Pickup and Delivery Tasks",
                "arxiv_id": "1705.10868",
            }
        }
        candidate = Paper(
            source="semanticscholar",
            title="Lifelong Multi-Agent Path Finding for Online Pickup and Delivery Tasks",
            arxiv_id="1705.10868v2",
            doi="10.65109/tnmo6004",
        )

        self.assertTrue(paper_is_seen(candidate, build_seen_aliases(seen)))

    def test_arxiv_doi_alias_matches_arxiv_record_without_metadata(self):
        seen = {"arxiv:2304.04217": {"title": "The Study of Highway for Lifelong MAPF"}}
        candidate = Paper(
            source="openalex",
            title="A title variant from another source",
            doi="https://doi.org/10.48550/arXiv.2304.04217",
        )

        self.assertTrue(paper_is_seen(candidate, seen))

    def test_exact_normalized_title_matches_without_shared_identifier(self):
        seen = {
            "arxiv:1": {
                "title": "MAPF: Planning With Priorities",
                "arxiv_id": "1",
            }
        }
        candidate = Paper(
            source="openalex",
            title="MAPF - Planning with Priorities",
            doi="10.1000/different-key",
        )

        self.assertTrue(paper_is_seen(candidate, seen))

    def test_distinct_paper_is_not_seen(self):
        seen = {"arxiv:1": {"title": "Existing MAPF Paper", "arxiv_id": "1"}}
        candidate = Paper(source="arxiv", title="New MAPF Paper", arxiv_id="2")

        self.assertFalse(paper_is_seen(candidate, seen))

    def test_candidate_cache_excludes_seen_alias(self):
        seen = {
            "arxiv:1705.10868": {
                "title": "Lifelong Multi-Agent Path Finding for Online Pickup and Delivery Tasks",
                "arxiv_id": "1705.10868",
            }
        }
        duplicate = Paper(
            source="semanticscholar",
            title="Lifelong Multi-Agent Path Finding for Online Pickup and Delivery Tasks",
            abstract="Multi-agent pickup and delivery in a warehouse.",
            arxiv_id="1705.10868",
            doi="10.65109/tnmo6004",
        )

        cached = _cacheable_candidates(
            [duplicate], seen, ["MAPF"], [], []
        )

        self.assertEqual(cached, [])

    def test_duplicate_records_keep_original_addition_time_and_new_metadata(self):
        seen = {
            "mapf": {
                "arxiv:1705.10868": {
                    "title": "Lifelong Multi-Agent Path Finding for Online Pickup and Delivery Tasks",
                    "file": "mapf/1705.10868.html",
                    "arxiv_id": "1705.10868",
                    "added": "2026-07-11",
                    "added_at": "2026-07-11T06:00:00",
                    "citations": 300,
                    "venue": "",
                },
                "doi:10.65109/tnmo6004": {
                    "title": "Lifelong Multi-Agent Path Finding for Online Pickup and Delivery Tasks",
                    "file": "mapf/1705.10868.html",
                    "arxiv_id": "1705.10868",
                    "doi": "10.65109/tnmo6004",
                    "added": "2026-07-29",
                    "added_at": "2026-07-29T06:00:00",
                    "citations": 332,
                    "venue": "AAMAS",
                },
            }
        }

        removed = collapse_seen_duplicates(seen)

        self.assertEqual(len(removed), 1)
        self.assertEqual(list(seen["mapf"]), ["arxiv:1705.10868"])
        merged = seen["mapf"]["arxiv:1705.10868"]
        self.assertEqual(merged["added"], "2026-07-11")
        self.assertEqual(merged["added_at"], "2026-07-11T06:00:00")
        self.assertEqual(merged["citations"], 332)
        self.assertEqual(merged["venue"], "AAMAS")
        self.assertEqual(merged["doi"], "10.65109/tnmo6004")


if __name__ == "__main__":
    unittest.main()

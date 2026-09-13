from contextlib import ExitStack
import datetime
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from pipeline import run
from pipeline.copilot import CopilotError
from pipeline.dedup import load_seen
from pipeline.schema import Paper
from pipeline.summarize import Summarizer
from test_copilot import article

FIELD = "mapf-mapd-warehouse"


def paper(number, **kwargs):
    return Paper(source="arxiv", title=f"{number} Multi-Agent Path Finding",
                 abstract="Collision-free multi-agent path finding.",
                 arxiv_id=f"2609.{number:05d}", **kwargs)


class DailyPipelineTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.seen = self.root / "data/seen.json"
        self.seen.parent.mkdir()
        self.seen.write_text("{}")
        self.subs = run.load_subscriptions()
        for key, value in {"ROOT": str(self.root), "DATA": str(self.root / "data"),
                           "SEEN": str(self.seen), "RUNS": str(self.root / "data/runs"),
                           "CANDIDATE_CACHE": str(self.root / "data/cache/candidates")}.items():
            self.stack.enter_context(mock.patch.object(run, key, value))
        self.stack.enter_context(mock.patch.object(run, "load_subscriptions", return_value=self.subs))
        self.stack.enter_context(mock.patch.object(run, "enrich_venue", side_effect=lambda p: p))
        self.stack.enter_context(mock.patch.object(run, "_enrich_fulltext_source", side_effect=lambda p: p))
        self.fetch = self.stack.enter_context(mock.patch.object(run, "fetch_sections", return_value=([("Method", "MAPF method source.")], "fulltext(arxiv)")))
        self.gather = self.stack.enter_context(mock.patch.object(run, "gather", return_value=([paper(i) for i in range(1, 8)], {})))
        self.llm = mock.Mock(model=None)
        self.llm.generate.return_value = json.dumps(article(), ensure_ascii=False)
        self.summarizer = Summarizer(llm=self.llm)
        self.stack.enter_context(mock.patch.object(run, "Summarizer", return_value=self.summarizer))
        self.stack.enter_context(mock.patch("sys.stdout", new=io.StringIO()))

    def test_mapf_only_max_two_and_same_day_rerun_is_free(self):
        self.assertEqual(run.main(["--limit", "100"]), 0)
        self.assertEqual(len(load_seen(self.seen)[FIELD]), 2)
        self.assertEqual(self.llm.generate.call_count, 4)
        for call in self.gather.call_args_list:
            self.assertEqual(call.args[0]["username"], FIELD)
        self.assertEqual(run.main([]), 0)
        self.assertEqual(self.llm.generate.call_count, 4)
        self.assertEqual(self.gather.call_count, 2)

    def test_dry_run_fetches_only_and_does_not_write(self):
        before = self.seen.read_bytes()
        self.assertEqual(run.main(["--dry-run"]), 0)
        self.assertEqual(self.fetch.call_count, 2)
        self.llm.generate.assert_not_called()
        self.assertEqual(self.seen.read_bytes(), before)
        self.assertEqual(list(self.root.rglob("*.html")), [])

    def test_skip_processed_versions_and_cross_field_duplicates_then_fill_two(self):
        self.seen.write_text(json.dumps({"reading": {"arxiv:2609.00001v2": {"title": paper(1).title, "added": "2026-01-01", "file": "reading/old.html"}},
                                         FIELD: {"doi:10.48550/arxiv.2609.00002": {"title": paper(2).title, "added": "2026-01-01", "file": f"{FIELD}/old.html"}}}))
        for rel in ["reading/old.html", f"{FIELD}/old.html"]:
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("<html>old</html>")
        self.assertEqual(run.main([]), 0)
        self.assertEqual(self.llm.generate.call_count, 4)
        fetched = {call.args[0].arxiv_id for call in self.fetch.call_args_list}
        self.assertNotIn("2609.00001", fetched)
        self.assertNotIn("2609.00002", fetched)
        self.assertEqual(len(load_seen(self.seen)[FIELD]), 3)

    def test_quota_stops_without_marking_failed_paper(self):
        self.llm.generate.side_effect = [json.dumps(article()), CopilotError("quota exceeded")]
        self.assertEqual(run.main([]), 2)
        self.assertEqual(load_seen(self.seen)[FIELD], {})
        self.assertEqual(self.llm.generate.call_count, 2)
        self.assertEqual(list((self.root / FIELD).glob("*.html")), [self.root / FIELD / "index.html"])

    def test_second_failure_keeps_first_verified_article(self):
        self.llm.generate.side_effect = [json.dumps(article()), json.dumps(article()), CopilotError("network down")]
        self.assertEqual(run.main([]), 2)
        self.assertEqual(len(load_seen(self.seen)[FIELD]), 1)
        record = next(iter(load_seen(self.seen)[FIELD].values()))
        self.assertTrue((self.root / record["file"]).read_text().strip().endswith("</html>"))

    def test_bad_json_is_not_retried_across_entire_candidate_queue(self):
        self.llm.generate.return_value = "broken json"
        self.assertEqual(run.main([]), 2)
        self.assertEqual(self.llm.generate.call_count, 2)
        self.assertFalse(load_seen(self.seen)[FIELD])

    def test_fetch_failure_and_abstract_only_try_later_candidates_without_llm(self):
        self.fetch.side_effect = [RuntimeError("HTML/PDF network failure"), ([], "abstract"),
                                 ([("Method", "Body")], "fulltext(arxiv)"),
                                 ([("Method", "Body")], "fulltext(arxiv)")]
        self.assertEqual(run.main([]), 0)
        self.assertEqual(self.fetch.call_count, 4)
        self.assertEqual(self.llm.generate.call_count, 4)
        self.assertEqual(len(load_seen(self.seen)[FIELD]), 2)

    def test_no_eligible_papers_is_valid_zero(self):
        self.gather.return_value = ([], {})
        self.assertEqual(run.main([]), 0)
        self.llm.generate.assert_not_called()

    def test_all_sources_failed_is_visible(self):
        self.gather.return_value = ([], {"arxiv/recent": {"error": "network down"}})
        self.assertEqual(run.main([]), 2)
        self.llm.generate.assert_not_called()

    def test_generic_robotics_rag_and_marl_are_rejected(self):
        self.gather.return_value = ([Paper(source="arxiv", title=t) for t in (
            "Robotic Warehouse Control", "Multi-Agent Task Allocation", "Retrieval-Augmented Generation",
            "Multi-Agent Reinforcement Learning", "Generic Path Planning")], {})
        self.assertEqual(run.main([]), 0)
        self.fetch.assert_not_called()
        self.llm.generate.assert_not_called()

    def test_existing_unindexed_url_is_not_re_summarized(self):
        # Protect HTML even if an old metadata record is missing.
        for i in range(1, 8):
            p = self.root / FIELD / f"2609.{i:05d}.html"
            p.parent.mkdir(exist_ok=True)
            p.write_text("<html>old</html>")
        self.assertEqual(run.main([]), 0)
        self.llm.generate.assert_not_called()


if __name__ == "__main__":
    unittest.main()

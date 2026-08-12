import json
import os
import tempfile
import unittest

from pipeline.run import _should_preserve_existing_report


def report(candidates=0, added=("paper-1",), skipped=0):
    return {
        "date": "2026-08-12",
        "fields": [
            {
                "slug": "field-a",
                "candidates_total": candidates,
                "fresh_total": candidates,
                "relevant_total": candidates,
                "added": [{"id": paper_id} for paper_id in added],
                "skipped": [{"id": f"skip-{i}"} for i in range(skipped)],
            }
        ],
    }


class RunReportTests(unittest.TestCase):
    def write_existing(self, directory, data):
        path = os.path.join(directory, "2026-08-12.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)

    def test_preserves_detailed_report_on_zero_page_rerun(self):
        with tempfile.TemporaryDirectory() as directory:
            self.write_existing(directory, report(candidates=100, skipped=3))
            self.assertTrue(
                _should_preserve_existing_report(report(), 0, runs_dir=directory)
            )

    def test_does_not_preserve_report_missing_a_current_paper(self):
        with tempfile.TemporaryDirectory() as directory:
            self.write_existing(directory, report(candidates=100, added=("paper-1",)))
            self.assertFalse(
                _should_preserve_existing_report(
                    report(added=("paper-1", "paper-2")), 0, runs_dir=directory
                )
            )

    def test_does_not_preserve_when_current_run_generated_pages(self):
        with tempfile.TemporaryDirectory() as directory:
            self.write_existing(directory, report(candidates=100))
            self.assertFalse(
                _should_preserve_existing_report(report(), 1, runs_dir=directory)
            )


if __name__ == "__main__":
    unittest.main()

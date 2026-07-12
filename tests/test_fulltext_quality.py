import unittest

from pipeline import fulltext
from pipeline.run import _reading_value_issues


class FulltextQualityTest(unittest.TestCase):
    TITLE = "Guidance Graph Optimization for Lifelong Multi-Agent Path Finding"

    def test_rejects_conference_formatting_template(self):
        sections = [
            (
                "IJCAI-24 Formatting Instructions",
                "Style and Format requirements cover paper size and camera-ready files.",
            ),
            (
                "Tables and Illustrations",
                "Use the booktabs package and follow the instructions for proofs.",
            ),
        ]

        self.assertFalse(fulltext._sections_are_plausible(sections, self.TITLE))

    def test_accepts_matching_research_article(self):
        sections = [
            (
                "1 Introduction",
                "Guidance graph optimization improves lifelong path finding throughput.",
            ),
            (
                "4 Approach",
                "We optimize guidance graph edge weights for lifelong path planning.",
            ),
        ]

        self.assertTrue(fulltext._sections_are_plausible(sections, self.TITLE))

    def test_rejects_reading_value_one_from_daily_publish(self):
        self.assertTrue(_reading_value_issues({"_reading_value": 1}))
        self.assertFalse(_reading_value_issues({"_reading_value": 2}))


if __name__ == "__main__":
    unittest.main()

import unittest

from pipeline.run import _domain_context_issue, _matched_keywords
from pipeline.schema import Paper


KEYWORDS = [
    "Multi-Agent Path Finding",
    "MAPF",
    "Multi-Agent Pickup and Delivery",
    "MAPD",
]
AMBIGUOUS = ["MAPF", "MAPD"]
CONTEXT = [
    "Multi-Agent",
    "Multi Agent",
    "Path Finding",
    "Pathfinding",
    "Pickup and Delivery",
    "Pickup-and-Delivery",
    "Task Assignment",
    "Path Planning",
    "Multi-Robot",
    "Robot",
    "Warehouse",
]


class DomainSelectionTest(unittest.TestCase):
    def _issue(self, paper):
        matched = _matched_keywords(paper, KEYWORDS)
        return _domain_context_issue(paper, matched, AMBIGUOUS, CONTEXT)

    def test_rejects_mapd_photodetector_acronym_collision(self):
        paper = Paper(
            source="arxiv",
            title="MAPD type avalanche photodetectors",
            abstract="We evaluate micro-pixel avalanche photodiodes for PET cameras.",
        )

        self.assertIn("曖昧な略語のみ一致", self._issue(paper))

    def test_rejects_mapd_gamma_detector_acronym_collision(self):
        paper = Paper(
            source="arxiv",
            title="Gamma ray detection performance with MAPD readout",
            abstract="A scintillator, contrast agent, and photodiode detector are evaluated.",
        )

        self.assertIn("分野文脈なし", self._issue(paper))

    def test_accepts_mapd_acronym_with_multi_agent_context(self):
        paper = Paper(
            source="arxiv",
            title="An Efficient MAPD Solver",
            abstract="Warehouse robots receive pickup-and-delivery tasks as online agents.",
        )

        self.assertEqual(self._issue(paper), "")

    def test_accepts_full_multi_agent_pickup_and_delivery_phrase(self):
        paper = Paper(
            source="openalex",
            title="Multi-Agent Pickup and Delivery Problems",
            abstract="A modular graph generation framework is proposed.",
        )

        self.assertEqual(self._issue(paper), "")


if __name__ == "__main__":
    unittest.main()

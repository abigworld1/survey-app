import unittest
from unittest import mock

from pipeline.schema import Paper
from pipeline import render, venue


class VenueEnrichmentTest(unittest.TestCase):
    @mock.patch("pipeline.venue._crossref_lookup")
    def test_keeps_existing_conference_without_network_lookup(self, crossref):
        paper = Paper(
            source="arxiv", title="A MAPF Paper", venue="AAMAS", published="2026-04-01"
        )

        self.assertIs(venue.enrich_venue(paper), paper)
        self.assertEqual(paper.venue, "AAMAS 2026")
        crossref.assert_not_called()

    @mock.patch("pipeline.venue._semantic_scholar_lookup")
    @mock.patch("pipeline.venue._openalex_lookup")
    @mock.patch("pipeline.venue._dblp_lookup")
    @mock.patch("pipeline.venue._crossref_lookup")
    def test_prefers_crossref_for_doi_venue(
        self, crossref, dblp_lookup, openalex_lookup, semantic_lookup
    ):
        paper = Paper(
            source="arxiv",
            title="Reliable Multi-Agent Path Finding",
            doi="10.1000/mapf",
        )
        crossref.return_value = Paper(
            source="crossref",
            title="Reliable Multi-Agent Path Finding",
            venue="International Conference on Automated Planning and Scheduling",
            published="2026",
            doi="10.1000/mapf",
        )

        venue.enrich_venue(paper)

        self.assertEqual(
            paper.venue,
            "International Conference on Automated Planning and Scheduling 2026",
        )
        dblp_lookup.assert_not_called()
        openalex_lookup.assert_not_called()
        semantic_lookup.assert_not_called()

    @mock.patch("pipeline.venue._semantic_scholar_lookup")
    @mock.patch("pipeline.venue._openalex_lookup")
    @mock.patch("pipeline.venue._dblp_lookup")
    def test_ignores_different_title_and_uses_next_source(
        self, dblp_lookup, openalex_lookup, semantic_lookup
    ):
        paper = Paper(source="arxiv", title="Reliable Multi-Agent Path Finding")
        dblp_lookup.return_value = Paper(
            source="dblp", title="Unrelated Language Model", venue="ACL"
        )
        openalex_lookup.return_value = Paper(
            source="openalex",
            title="Reliable Multi-Agent Path Finding",
            venue="AAAI",
            published="2026-01-20",
            citations=12,
        )

        venue.enrich_venue(paper)

        self.assertEqual(paper.venue, "AAAI 2026")
        self.assertEqual(paper.citations, 12)
        semantic_lookup.assert_not_called()

    def test_preprint_names_are_not_accepted_as_venues(self):
        self.assertFalse(venue._usable_venue("arXiv (Cornell University)"))
        self.assertFalse(venue._usable_venue("CoRR"))
        self.assertTrue(venue._usable_venue("Artificial Intelligence"))
        self.assertEqual(venue.venue_with_year("ICAPS", "2026-05-01"), "ICAPS 2026")
        self.assertEqual(venue.venue_with_year("ICAPS 2026", "2026"), "ICAPS 2026")
        self.assertEqual(
            venue.venue_with_year("Artificial Intelligence", "2026"),
            "Artificial Intelligence",
        )
        self.assertEqual(
            render._venue_label("AAAI", published="2026-02-01"), "AAAI 2026"
        )

    @mock.patch("pipeline.venue._semantic_scholar_lookup", return_value=None)
    @mock.patch("pipeline.venue._openalex_lookup", return_value=None)
    @mock.patch("pipeline.venue._dblp_lookup", return_value=None)
    def test_unresolved_preprint_is_left_as_missing(
        self, dblp_lookup, openalex_lookup, semantic_lookup
    ):
        paper = Paper(
            source="arxiv",
            title="Unpublished MAPF Preprint",
            venue="arXiv (Cornell University)",
        )

        venue.enrich_venue(paper)

        self.assertEqual(paper.venue, "")


if __name__ == "__main__":
    unittest.main()

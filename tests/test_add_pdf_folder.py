import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from pipeline import add_paper
from pipeline.schema import Paper


class PdfFolderTests(unittest.TestCase):
    def test_discovers_pdfs_recursively_and_case_insensitively(self):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root, "incoming")
            nested = folder / "nested"
            nested.mkdir(parents=True)
            (folder / "b.PDF").write_bytes(b"pdf")
            (nested / "a.pdf").write_bytes(b"pdf")
            (nested / "ignore.txt").write_text("x", encoding="utf-8")

            found = add_paper._discover_pdf_files("incoming", root=root)

            self.assertEqual(
                [path.relative_to(root).as_posix() for path in found],
                ["incoming/b.PDF", "incoming/nested/a.pdf"],
            )

    def test_non_recursive_mode_uses_only_direct_children(self):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root, "incoming")
            nested = folder / "nested"
            nested.mkdir(parents=True)
            (folder / "direct.pdf").write_bytes(b"pdf")
            (nested / "nested.pdf").write_bytes(b"pdf")

            found = add_paper._discover_pdf_files(
                "incoming", recursive=False, root=root
            )

            self.assertEqual([path.name for path in found], ["direct.pdf"])

    def test_rejects_folder_outside_repository(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
            with self.assertRaisesRegex(ValueError, "survey-app"):
                add_paper._discover_pdf_files(outside, root=root)

    def test_extracts_only_explicit_doi_and_arxiv_identifiers(self):
        text = (
            "https://doi.org/10.1234/ABC.42.\n"
            "arXiv: 2608.12345v2\n"
            "A cited value 10.9999/not-explicit must not replace it."
        )
        doi, arxiv_id = add_paper._extract_pdf_identifiers(text)
        self.assertEqual(doi, "10.1234/ABC.42")
        self.assertEqual(arxiv_id, "2608.12345")

    @mock.patch("pipeline.add_paper.enrich_venue")
    def test_registered_paper_is_skipped_before_metadata_or_llm(self, enrich_venue):
        paper = Paper(source="pdf", title="Already Added Paper")
        seen = {
            "reading": {
                "title:already added paper": {
                    "title": "Already Added Paper",
                    "file": "reading/already-added-paper.html",
                }
            }
        }
        summarizer = mock.Mock()

        result = add_paper._add_prepared_paper(
            paper,
            [("Body", "text")],
            "fulltext(pdf)",
            "reading",
            {},
            seen,
            summarizer,
        )

        self.assertEqual(result["status"], "skipped")
        enrich_venue.assert_not_called()
        summarizer.summarize.assert_not_called()

    @mock.patch("pipeline.add_paper._print_publish_command")
    @mock.patch("pipeline.add_paper._render_and_save")
    @mock.patch("pipeline.add_paper.save_seen")
    @mock.patch("pipeline.add_paper._add_prepared_paper")
    @mock.patch("pipeline.add_paper._from_pdf_bytes")
    def test_bulk_continues_after_one_pdf_fails(
        self,
        from_pdf,
        add_prepared,
        save_seen,
        render_and_save,
        print_publish,
    ):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root, "incoming")
            folder.mkdir()
            (folder / "a.pdf").write_bytes(b"bad")
            (folder / "b.pdf").write_bytes(b"good")
            args = SimpleNamespace(
                pdf_dir="incoming",
                no_recursive=False,
                limit=0,
                fail_fast=False,
            )
            from_pdf.side_effect = [
                ValueError("broken PDF"),
                (Paper(source="pdf", title="Good Paper"), [("Body", "text")], "fulltext(pdf)"),
            ]
            add_prepared.return_value = {
                "status": "added",
                "title": "Good Paper",
                "file": "reading/good-paper.html",
            }
            with mock.patch.object(add_paper, "ROOT", root):
                rc = add_paper._add_pdf_folder(
                    args,
                    "reading",
                    {},
                    "Reading",
                    [],
                    {},
                    SimpleNamespace(engine="stub"),
                )

        self.assertEqual(rc, 1)
        self.assertEqual(from_pdf.call_count, 2)
        add_prepared.assert_called_once()
        save_seen.assert_called_once()
        render_and_save.assert_called_once()
        print_publish.assert_called_once()


if __name__ == "__main__":
    unittest.main()

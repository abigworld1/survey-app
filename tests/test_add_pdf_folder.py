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
            skip_existing=True,
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
                skip_existing=False,
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


class ManualReadditionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for patcher in (
            mock.patch.object(add_paper, "ROOT", str(self.root)),
            mock.patch.object(add_paper, "SEEN", str(self.root / "data/seen.json")),
            mock.patch.object(add_paper, "_fill_arxiv_metadata", side_effect=lambda p: p),
            mock.patch.object(add_paper, "enrich_venue", side_effect=lambda p: p),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.summarizer = mock.Mock(engine="stub")
        self.summarizer.summarize.return_value = {
            "tldr": "Updated summary", "_basis": "fulltext(pdf)", "_engine": "stub",
        }
        self.summarizer.rate_reading_value.return_value = {}
        self.paper = Paper(source="pdf", title="Existing Paper")
        self.key = self.paper.key()
        self.rel = "reading/original-url.html"
        self.followups = (
            '<!-- followup-qa:start --><section class="followups">'
            'Existing question and answer</section><!-- followup-qa:end -->'
        )
        (self.root / "reading").mkdir()
        (self.root / self.rel).write_text("Old summary" + self.followups, encoding="utf-8")
        self.seen = {"reading": {self.key: {
            "title": self.paper.title, "file": self.rel,
            "added": "2026-01-01", "added_at": "2026-01-01T06:00:00",
            "selection": "important", "tldr": "Old summary",
            "doi": "10.1234/original", "venue": "ICAPS 2024",
            "authors": ["Original Author"], "date": "2024-05-01",
            "citations": 12,
        }}}

    def add(self, paper=None, seen=None):
        return add_paper._add_prepared_paper(
            paper or self.paper, [("Body", "New PDF text")], "fulltext(pdf)",
            "reading", {}, self.seen if seen is None else seen, self.summarizer,
        )

    def test_readdition_updates_same_page_and_keeps_questions_and_metadata(self):
        result = self.add()

        self.assertEqual(result["status"], "updated")
        self.assertEqual(result["file"], self.rel)
        self.assertEqual(list(self.seen["reading"]), [self.key])
        record = self.seen["reading"][self.key]
        self.assertEqual(record["tldr"], "Updated summary")
        self.assertEqual(record["venue"], "ICAPS 2024")
        self.assertEqual(record["doi"], "10.1234/original")
        self.assertEqual(record["authors"], ["Original Author"])
        self.assertEqual(record["citations"], 12)
        self.assertEqual(record["selection"], "manual")
        self.assertGreater(record["added_at"], "2026-01-01T06:00:00")
        html = (self.root / self.rel).read_text(encoding="utf-8")
        self.assertIn("Updated summary", html)
        self.assertIn(self.followups, html)
        self.assertNotIn("Old summary", html)
        self.assertEqual(len(list((self.root / "reading").glob("*.html"))), 1)
        self.summarizer.summarize.assert_called_once_with(
            self.paper, sections=[("Body", "New PDF text")], basis="fulltext(pdf)"
        )

    def test_existing_identifiers_match_even_when_title_changes(self):
        for attrs in (
            {"doi": "https://doi.org/10.1234/ORIGINAL"},
            {"arxiv_id": "2601.12345v2"},
        ):
            with self.subTest(attrs=attrs):
                self.seen["reading"][self.key]["arxiv_id"] = "2601.12345"
                paper = Paper(source="pdf", title="Corrected Paper Title", **attrs)
                self.assertEqual(self.add(paper)["file"], self.rel)
                self.assertEqual(list(self.seen["reading"]), [self.key])

    def test_match_found_after_metadata_enrichment_updates_existing_page(self):
        paper = Paper(source="pdf", title="Filename-derived title")

        def enrich(p):
            p.doi = "10.1234/original"
            return p

        with mock.patch.object(add_paper, "enrich_venue", side_effect=enrich):
            result = self.add(paper)

        self.assertEqual(result["status"], "updated")
        self.assertEqual(result["file"], self.rel)
        self.assertEqual(list(self.seen["reading"]), [self.key])

    def test_failure_keeps_original_page_and_record(self):
        before = copy.deepcopy(self.seen)
        old_html = (self.root / self.rel).read_text(encoding="utf-8")
        for target in ("summarize", "render"):
            with self.subTest(target=target):
                patcher = (
                    mock.patch.object(self.summarizer, "summarize", side_effect=RuntimeError("failed"))
                    if target == "summarize" else
                    mock.patch.object(add_paper.render, "render_paper_page", side_effect=RuntimeError("failed"))
                )
                with patcher, self.assertRaisesRegex(RuntimeError, "failed"):
                    self.add()
                self.assertEqual(self.seen, before)
                self.assertEqual((self.root / self.rel).read_text(encoding="utf-8"), old_html)

    def test_paper_in_other_field_is_added_to_requested_field(self):
        seen = {"mapf-mapd-warehouse": copy.deepcopy(self.seen["reading"])}
        before = copy.deepcopy(seen["mapf-mapd-warehouse"])

        result = self.add(seen=seen)

        self.assertEqual(result["status"], "added")
        self.assertEqual(seen["mapf-mapd-warehouse"], before)
        self.assertEqual(len(seen["reading"]), 1)

    def test_bulk_updates_are_saved_indexed_and_included_in_publish_command(self):
        folder = self.root / "incoming"
        folder.mkdir()
        (folder / "paper.pdf").write_bytes(b"%PDF")
        args = SimpleNamespace(
            pdf_dir="incoming", no_recursive=False, limit=0,
            fail_fast=False, skip_existing=False,
        )
        subs = [{"username": "reading", "label": "Reading", "manual": True}]
        output = io.StringIO()
        with mock.patch.object(add_paper, "_from_pdf_bytes", return_value=(
            self.paper, [("Body", "New PDF text")], "fulltext(pdf)"
        )), redirect_stdout(output):
            rc = add_paper._add_pdf_folder(
                args, "reading", subs[0], "Reading", subs, self.seen, self.summarizer
            )

        self.assertEqual(rc, 0)
        saved = json.loads((self.root / "data/seen.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["reading"][self.key]["tldr"], "Updated summary")
        for rel in ("index.html", "reading/index.html"):
            self.assertIn("Updated summary", (self.root / rel).read_text(encoding="utf-8"))
        self.assertIn("追加 0 / 更新 1 / スキップ 0 / 失敗 0", output.getvalue())
        self.assertIn(f"git add -- data/seen.json index.html reading/index.html {self.rel}", output.getvalue())

    def test_single_arxiv_cli_updates_by_default_and_can_skip(self):
        for skip in (False, True):
            with self.subTest(skip=skip):
                self.summarizer.reset_mock()
                with mock.patch.object(add_paper, "_load_subs", return_value=[]), \
                     mock.patch.object(add_paper, "load_seen", return_value=self.seen), \
                     mock.patch.object(add_paper, "_from_arxiv", return_value=(
                         self.paper, [("Body", "New text")], "fulltext(arxiv)"
                     )), \
                     mock.patch.object(add_paper, "Summarizer", return_value=self.summarizer), \
                     redirect_stdout(io.StringIO()):
                    rc = add_paper.main(
                        ["--arxiv", "2601.12345", "--stub"]
                        + (["--skip-existing"] if skip else [])
                    )
                self.assertEqual(rc, 0)
                self.assertEqual(self.summarizer.summarize.call_count, 0 if skip else 1)


if __name__ == "__main__":
    unittest.main()
import copy
import io
import json
from contextlib import redirect_stdout

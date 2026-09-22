import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from pipeline import publish


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "repo"
        self.root.mkdir()
        self.git = lambda *args: publish.git(*args, root=self.root)
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        (self.root / "data").mkdir()
        (self.root / "data/seen.json").write_text("{}")
        (self.root / ".nojekyll").touch()
        (self.root / "index.html").write_text("<html>old</html>")
        self.git("add", ".")
        self.git("commit", "-m", "baseline")
        self.artifact = Path(self.tmp.name) / "artifact"

    def add_article(self, number=1, field="mapf-mapd-warehouse", engine="copilot-cli:default"):
        seen = json.loads((self.root / "data/seen.json").read_text())
        path = self.root / field / f"2609.{number:05d}.html"
        path.parent.mkdir(exist_ok=True)
        path.write_text("<html>verified summary</html>")
        path_en = self.root / field / f"2609.{number:05d}.en.html"
        path_en.write_text("<html lang=\"en\">verified English summary</html>")
        seen.setdefault(field, {})[f"arxiv:2609.{number:05d}"] = {
            "title": f"MAPF paper {number}", "file": path.relative_to(self.root).as_posix(),
            "file_en": path_en.relative_to(self.root).as_posix(),
            "engine": engine, "basis": "fulltext(arxiv)", "tldr": "詳細な日本語の要約。" * 10,
            "tldr_en": "A detailed English summary of the selected MAPF research paper. " * 2,
            "added": "2026-09-13", "selection": "recent",
        }
        (self.root / "data/seen.json").write_text(json.dumps(seen))

    def test_stage_apply_roundtrip_and_build_excludes_private_files(self):
        baseline = self.git("rev-parse", "HEAD").stdout.strip()
        self.add_article()
        (self.root / ".env").write_text("do-not-publish")
        (self.root / "papers").mkdir()
        (self.root / "papers/input.pdf").write_text("do-not-publish")
        publish.stage(self.artifact, self.root)
        manifest = json.loads((self.artifact / "manifest.json").read_text())
        self.assertEqual(manifest["base_sha"], baseline)
        self.assertNotIn(".env", manifest["files"])
        clone = Path(self.tmp.name) / "clone"
        subprocess.run(["git", "clone", str(self.root), str(clone)], check=True, capture_output=True)
        publish.apply(self.artifact, clone)
        self.assertEqual((clone / "data/seen.json").read_bytes(), (self.root / "data/seen.json").read_bytes())
        site = Path(self.tmp.name) / "site"
        publish.build(site, clone)
        self.assertTrue((site / "mapf-mapd-warehouse/2609.00001.html").is_file())
        self.assertTrue((site / "mapf-mapd-warehouse/2609.00001.en.html").is_file())
        self.assertFalse((site / "data/seen.json").exists())
        self.assertFalse((site / ".env").exists())
        self.assertFalse((site / "papers").exists())

    def test_stub_rag_and_daily_overflow_rejected(self):
        self.add_article(engine="stub")
        with self.assertRaises(ValueError):
            publish.stage(self.artifact, self.root)
        (self.root / "data/seen.json").write_text("{}")
        self.add_article(field="doc-structure-rag")
        with self.assertRaises(ValueError):
            publish.validate_history(self.root)
        (self.root / "data/seen.json").write_text("{}")
        for number in range(1, 4):
            self.add_article(number)
        with self.assertRaisesRegex(ValueError, "two new"):
            publish.validate_history(self.root)

    def test_daily_article_requires_english_counterpart(self):
        self.add_article()
        seen = json.loads((self.root / "data/seen.json").read_text())
        info = next(iter(seen["mapf-mapd-warehouse"].values()))
        (self.root / info["file_en"]).unlink()

        with self.assertRaisesRegex(ValueError, "English article"):
            publish.validate_history(self.root)

    def test_history_loss_and_incomplete_html_rejected(self):
        self.add_article()
        self.git("add", ".")
        self.git("commit", "-m", "old article")
        (self.root / "data/seen.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "Historical metadata"):
            publish.validate_history(self.root)
        self.git("restore", "data/seen.json")
        (self.root / "mapf-mapd-warehouse/2609.00001.html").write_text("<html>broken")
        with self.assertRaisesRegex(ValueError, "incomplete"):
            publish.validate_history(self.root)

    def test_injected_artifact_path_rejected(self):
        publish.stage(self.artifact, self.root)
        manifest = json.loads((self.artifact / "manifest.json").read_text())
        manifest["files"] = ["../escape.py"]
        (self.artifact / "manifest.json").write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "Unexpected"):
            publish.apply(self.artifact, self.root)

    def setup_remote(self):
        remote = Path(self.tmp.name) / "remote.git"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
        self.git("remote", "add", "origin", str(remote))
        self.git("push", "origin", "main")
        return remote

    def test_real_commit_push_persists_article_to_local_remote(self):
        remote = self.setup_remote()
        self.add_article()
        publish.stage(self.artifact, self.root)
        publish.push(self.artifact, self.root, retry_delay=0)
        result = subprocess.run(["git", "--git-dir", str(remote), "show", "main:data/seen.json"],
                                check=True, capture_output=True, text=True)
        self.assertIn("2609.00001", result.stdout)
        self.assertEqual(self.git("status", "--porcelain").stdout, "")

    def test_actual_git_conflict_aborts_and_keeps_recovery_artifact(self):
        self.setup_remote()
        self.add_article()
        publish.stage(self.artifact, self.root)
        other = Path(self.tmp.name) / "other"
        subprocess.run(["git", "clone", "--branch", "main", str(self.root / "../remote.git"), str(other)],
                       check=True, capture_output=True)
        publish.git("config", "user.name", "Other", root=other)
        publish.git("config", "user.email", "other@example.invalid", root=other)
        (other / "data/seen.json").write_text('{"concurrent": {}}')
        publish.git("add", ".", root=other)
        publish.git("commit", "-m", "concurrent history", root=other)
        publish.git("push", "origin", "main", root=other)
        with self.assertRaisesRegex(RuntimeError, "rebase conflict"):
            publish.push(self.artifact, self.root, retry_delay=0)
        self.assertTrue((self.artifact / "files/data/seen.json").is_file())
        self.assertFalse((self.root / ".git/rebase-merge").exists())


if __name__ == "__main__":
    unittest.main()

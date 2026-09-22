import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest import mock

from pipeline.copilot import CopilotError, CopilotLLM, MAX_PROMPT_BYTES
from pipeline.evidence import build_evidence
from pipeline.schema import Paper
from pipeline.summarize import Summarizer, _parse_article, _ARTICLE_KEYS


def article():
    return {
        "title_ja": "競合探索による複数エージェント経路探索",
        "tldr": "複数のエージェントが共有グラフを移動する際の衝突を避けるため、制約に基づく探索を用いて経路を求める研究である。",
        "what": "倉庫内で複数の移動主体が共有グラフを移動する問題を扱う。各主体の始点と目的地が与えられる。経路の衝突を避ける必要がある。",
        "contribution": "従来の全主体を同時に探索する方式から、競合に応じて制約を分割する点に新規性がある。低水準探索と高水準探索を組み合わせる。",
        "method": "個別経路を探索した後、衝突する主体を検出する。禁止制約を子ノードに追加し、該当する経路を再計算する。競合がなくなれば解として返す。",
        "validation": "障害物を含む複数のグリッド地図で、完了までの実行時間を測定している。比較対象ごとの結果を検証する。取得した本文にない数値はここでは報告しない。",
        "discussion": "渋滞する環境では探索木が増大し、計算コストが課題になる。最適性を求める条件が適用範囲を制限する。将来の拡張は原典で確認する必要がある。",
        "background": "自動搬送設備では、多数のロボットを同時に運用する必要がある。",
        "problem": "一般的な単一経路の探索だけでは主体間の干渉を扱えない。",
        "technical_points": "競合から制約を構築し、対象の経路だけを再計算する。",
        "experiments": "複数の地図とエージェント数で探索を比較した。",
        "results": "探索に必要な時間と得られた経路を比較している。",
        "conclusion": "制約の分割によって衝突のない解を探索できる。",
        "limitations": "混雑に伴って探索量が増える点に注意が必要である。",
        "importance": "考察: 最適経路探索を研究する際の基本的な比較対象になる。",
        "recommended_for": "考察: 競合解決と最適性保証を研究するMAPF研究者に適する。",
    }


def bilingual_article():
    japanese = article()
    japanese["title"] = japanese.pop("title_ja")
    english = {
        key: ("Conflict-Based Search for Multi-Agent Path Finding" if key == "title" else f"English version: {value}")
        for key, value in japanese.items()
    }
    return {"ja": japanese, "en": english}


class CopilotAdapterTests(unittest.TestCase):
    @mock.patch.dict(os.environ, {"GITHUB_TOKEN": "test-secret", "OPENAI_API_KEY": "must-not-inherit", "NODE_OPTIONS": "unsafe"})
    @mock.patch("pipeline.copilot.subprocess.run")
    def test_inference_isolated_no_tools_and_utf8(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, "日本語要約", "")
        llm = CopilotLLM()
        self.assertEqual(llm.generate('paper $(touch nope) `whoami`'), "日本語要約")
        command = run.call_args.args[0]
        opts = run.call_args.kwargs
        self.assertEqual(command[2], 'paper $(touch nope) `whoami`')
        self.assertIn("--available-tools=", command)
        for kind in ("shell", "write", "url"):
            self.assertIn(f"--deny-tool={kind}", command)
        self.assertNotIn("--deny-tool=*", command)
        self.assertNotIn("--allow-all", command)
        self.assertFalse(opts.get("shell", False))
        self.assertEqual(opts["encoding"], "utf-8")
        self.assertEqual(opts["stdin"], subprocess.DEVNULL)
        self.assertNotEqual(opts["cwd"], str(Path.cwd()))
        self.assertNotIn("OPENAI_API_KEY", opts["env"])
        self.assertNotIn("NODE_OPTIONS", opts["env"])
        self.assertEqual(opts["env"]["GITHUB_TOKEN"], "test-secret")
        self.assertEqual(llm.calls, 1)

    @mock.patch.dict(os.environ, {}, clear=True)
    @mock.patch("pipeline.copilot.subprocess.run")
    def test_missing_token_does_not_fallback(self, run):
        with self.assertRaisesRegex(CopilotError, "GITHUB_TOKEN is missing"):
            CopilotLLM().generate("test")
        run.assert_not_called()

    @mock.patch.dict(os.environ, {"GITHUB_TOKEN": "test-secret"})
    def test_cli_failures_are_bounded_and_redacted(self):
        for result, reason in [
            (subprocess.CompletedProcess([], 1, "", "quota exceeded test-secret"), "quota"),
            (subprocess.CompletedProcess([], 1, "", "403 token invalid"), "authentication"),
            (subprocess.CompletedProcess([], 1, "", "network down"), "network"),
            (subprocess.CompletedProcess([], 0, "", ""), "empty"),
            (subprocess.CompletedProcess([], 0, "Quota exhausted", ""), "quota"),
        ]:
            with self.subTest(reason=reason), mock.patch("pipeline.copilot.subprocess.run", return_value=result) as run:
                with self.assertRaisesRegex(CopilotError, reason) as error:
                    CopilotLLM().generate("prompt")
                self.assertNotIn("test-secret", str(error.exception))
                self.assertEqual(run.call_count, 1)
        for failure in [FileNotFoundError("not installed"), subprocess.TimeoutExpired("sensitive prompt", 1)]:
            with mock.patch("pipeline.copilot.subprocess.run", side_effect=failure) as run:
                with self.assertRaises(CopilotError):
                    CopilotLLM(timeout=1).generate("prompt")
                self.assertEqual(run.call_count, 1)

    @mock.patch.dict(os.environ, {"GITHUB_TOKEN": "test-secret"})
    @mock.patch("pipeline.copilot.subprocess.run")
    def test_oversized_prompt_rejected_before_invocation(self, run):
        with self.assertRaises(ValueError):
            CopilotLLM().generate("あ" * (MAX_PROMPT_BYTES // 3 + 1))
        run.assert_not_called()


class TwoCallSummaryTests(unittest.TestCase):
    def setUp(self):
        self.paper = Paper(source="arxiv", title="Conflict-based Multi-Agent Path Finding", abstract="We solve MAPF.")
        self.sections = [("Methods", "We split conflicts."), ("Results", "The success rate is 75%.")]
        self.llm = mock.Mock(model=None)
        self.llm.generate.return_value = json.dumps(article(), ensure_ascii=False)
        self.summarizer = Summarizer(llm=self.llm)

    def test_full_article_and_rating_use_only_two_calls(self):
        summary = self.summarizer.summarize(self.paper, self.sections)
        self.summarizer.rate_reading_value(self.paper, summary, "fulltext")
        self.assertEqual(self.llm.generate.call_count, 2)
        self.assertEqual(len(summary["sections"]), 9)
        self.assertEqual(summary["title_ja"], article()["title_ja"])
        review = self.llm.generate.call_args.args[0]
        self.assertIn("75%", review)
        self.assertIn("benchmark", review)
        self.assertIn("初稿データ", review)
        self.assertLessEqual(len(review.encode("utf-8")), MAX_PROMPT_BYTES)

    def test_review_failure_does_not_publish_draft_or_retry(self):
        self.llm.generate.side_effect = [json.dumps(article()), CopilotError("quota")]
        with self.assertRaises(CopilotError):
            self.summarizer.summarize(self.paper, self.sections)
        self.assertEqual(self.llm.generate.call_count, 2)

    def test_bilingual_article_uses_two_calls_for_both_languages(self):
        self.llm.generate.return_value = json.dumps(bilingual_article(), ensure_ascii=False)

        summaries = self.summarizer.summarize_bilingual(self.paper, self.sections)

        self.assertEqual(self.llm.generate.call_count, 2)
        self.assertEqual(set(summaries), {"ja", "en"})
        self.assertEqual(summaries["ja"]["_language"], "ja")
        self.assertEqual(summaries["en"]["_language"], "en")
        self.assertEqual(len(summaries["ja"]["sections"]), 9)
        self.assertEqual(len(summaries["en"]["sections"]), 9)

    def test_bad_json_and_missing_fields_do_not_retry(self):
        for content in ["not json", "[]", '{"tldr":"short"}']:
            self.llm.reset_mock()
            self.llm.generate.return_value = content
            with self.assertRaises(ValueError):
                self.summarizer.summarize(self.paper, self.sections)
            self.assertEqual(self.llm.generate.call_count, 1)

    def test_fenced_json_and_unknown_keys(self):
        data = article() | {"_followups_html": "<script>bad()</script>", "url": "javascript:bad"}
        result = _parse_article("```json\n" + json.dumps(data) + "\n```")
        self.assertEqual(set(result), set(_ARTICLE_KEYS))

    def test_uncorrected_duplicate_fails_after_two_calls(self):
        data = article()
        data["contribution"] = data["what"]
        self.llm.generate.return_value = json.dumps(data)
        with self.assertRaisesRegex(ValueError, "quality"):
            self.summarizer.summarize(self.paper, self.sections)
        self.assertEqual(self.llm.generate.call_count, 2)

    def test_long_paper_preserves_late_evidence_and_removes_noise(self):
        sections = [(f"Proof {i}", f"Proof detail {i}. " * 1000) for i in range(40)]
        sections += [("Methods", "METHOD-EVIDENCE " * 1000), ("Results", "SUCCESS 75%."),
                     ("Conclusion", "CONCLUSION-EVIDENCE."), ("References", "DO-NOT-INCLUDE"),
                     ("Acknowledgements", "ALSO-NO")]
        evidence = build_evidence(self.paper, sections, max_chars=8000)
        self.assertIn("METHOD-EVIDENCE", evidence)
        self.assertIn("SUCCESS 75%", evidence)
        self.assertIn("CONCLUSION-EVIDENCE", evidence)
        self.assertNotIn("DO-NOT-INCLUDE", evidence)
        self.assertNotIn("ALSO-NO", evidence)
        self.assertLessEqual(len(evidence), 8000)

    def test_unicode_budget_and_html_cleanup(self):
        sections = [("Method", '<nav>REMOVE NAV</nav><p>' + '本文。' * 20000 + '</p>'),
                    ("Conclusion", "結論の記述。")]
        evidence = build_evidence(self.paper, sections, max_chars=32000)
        self.assertNotIn("REMOVE NAV", evidence)
        self.assertIn("結論の記述", evidence)
        self.assertLessEqual(len(evidence.encode("utf-8")), 60000)


if __name__ == "__main__":
    unittest.main()

import unittest
from unittest import mock

from pipeline.schema import Paper
from pipeline.summarize import (
    CLARITY_REVIEW_SYSTEM,
    FINAL_FACTCHECK_SYSTEM,
    Summarizer,
    _evidence_excerpt,
    _sanitize_generated_text,
    _section_quality_issues,
    _synthesis_quality_issues,
)


class SummarizeQualityTest(unittest.TestCase):
    def test_invalid_math_does_not_capture_following_japanese(self):
        text = (
            r"まず $K\text{BestJointSequencing$ を用いてタスクを生成し、"
            r"$\mathcal{A}_{lock}$ を求める。"
        )

        cleaned = _sanitize_generated_text(text)

        self.assertIn("KBestJointSequencing を用いて", cleaned)
        self.assertIn("A_lock", cleaned)
        self.assertNotIn("$", cleaned)
        self.assertNotIn("\\mathcal", cleaned)
        self.assertNotIn(r"\text{BestJointSequencing", cleaned)

    def test_japanese_inside_math_falls_back_to_plain_text(self):
        cleaned = _sanitize_generated_text(r"値は $x \text{ は日本語 } y$ である。")

        self.assertEqual(cleaned, "値は x は日本語 y である。")

    def test_valid_tex_is_also_rendered_as_plain_text(self):
        cleaned = _sanitize_generated_text(
            r"計算量は $O(n\log n)$、目的値は $\max_i T_i$ である。"
        )

        self.assertEqual(cleaned, "計算量は O(n log n)、目的値は max_i T_i である。")

    def test_review_excerpt_keeps_results_and_ending(self):
        source = (
            "Introduction sentence. " * 80
            + "The success rate improved from 40% to 75%. "
            + "Background sentence. " * 80
            + "The main limitation is runtime under congestion."
        )

        excerpt = _evidence_excerpt(source, 900)

        self.assertIn("40% to 75%", excerpt)
        self.assertIn("limitation is runtime", excerpt)
        self.assertLessEqual(len(excerpt), 900)

    def test_numbered_references_become_self_contained(self):
        cleaned = _sanitize_generated_text(
            "図3に示すように、成功率が上がる。Algorithm 1を適用し、式(4)で評価する。"
        )

        self.assertEqual(cleaned, "成功率が上がる。提案手順を適用し、この定式化で評価する。")

    def test_pseudocode_only_name_requests_revision(self):
        issues = _section_quality_issues("KBestJointSequencingを用いて候補を生成する。")

        self.assertIn("擬似コード固有の関数名", issues)

    def test_named_methods_are_not_mistaken_for_pseudocode_functions(self):
        self.assertFalse(_section_quality_issues("LaCAMを適用して局所再計画する。"))
        self.assertFalse(_section_quality_issues("SentenceBERTを用いて埋め込みを作る。"))

    def test_duplicate_ochiai_items_request_revision(self):
        repeated = "局所再計画によってデッドロックを解消し、成功率を大幅に向上させる。"
        data = {
            "tldr": "問題と結論の要約。",
            "what": repeated,
            "contribution": repeated,
            "method": "停滞したエージェントだけを局所的に再計画する。",
            "validation": "複数の混雑条件で成功率を比較した。",
            "discussion": "大規模環境への拡張が課題である。",
        }

        issues = _synthesis_quality_issues(data)

        self.assertIn("what と contribution の内容重複", issues)

    def test_structured_summary_retries_duplicate_draft(self):
        repeated = "局所再計画によってデッドロックを解消し、成功率を大幅に向上させる。"
        first = (
            f"@@TLDR@@\n要約。\n@@WHAT@@\n{repeated}\n@@CONTRIBUTION@@\n{repeated}\n"
            "@@METHOD@@\n停滞した対象だけを再計画する。\n"
            "@@VALIDATION@@\n混雑条件で成功率を比較した。\n"
            "@@DISCUSSION@@\n大規模化が課題である。"
        )
        revised = (
            "@@TLDR@@\n要約。\n@@WHAT@@\n複数主体が停滞する問題を扱う。\n"
            "@@CONTRIBUTION@@\n従来法に完全な局所探索を組み合わせた。\n"
            "@@METHOD@@\n停滞した対象だけを再計画する。\n"
            "@@VALIDATION@@\n混雑条件で成功率を比較した。\n"
            "@@DISCUSSION@@\n大規模化が課題である。"
        )
        summarizer = object.__new__(Summarizer)
        responses = iter([first, revised])
        calls = []

        def fake_chat(system, user, max_tokens):
            calls.append((system, user, max_tokens))
            return next(responses)

        summarizer._chat = fake_chat

        data = summarizer._structured_summary("system", "source", 1000)

        self.assertEqual(len(calls), 2)
        self.assertEqual(data["what"], "複数主体が停滞する問題を扱う。")

    def test_final_review_polishes_then_fact_checks_against_source(self):
        marked = (
            "@@TLDR@@\n問題と結論。\n"
            "@@WHAT@@\n対象問題を定義する。\n"
            "@@CONTRIBUTION@@\n従来法との差分を示す。\n"
            "@@METHOD@@\n局所的に再計画する。\n"
            "@@VALIDATION@@\nベンチマークで比較した。\n"
            "@@DISCUSSION@@\n大規模化が課題である。"
        )
        summarizer = object.__new__(Summarizer)
        responses = iter([marked, marked])
        calls = []

        def fake_chat(system, user, max_tokens):
            calls.append((system, user, max_tokens))
            return next(responses)

        summarizer._chat = fake_chat
        paper = type(
            "PaperStub",
            (),
            {
                "title": "Verified MAPF",
                "abstract": "We compare success rates.",
                "authors": ["A. Author"],
            },
        )()
        draft = {
            "tldr": "問題と結論。",
            "what": "対象問題を定義する。",
            "contribution": "従来法との差分を示す。",
            "method": "局所的に再計画する。",
            "validation": "ベンチマークで比較した。",
            "discussion": "大規模化が課題である。",
        }

        result = summarizer._final_review(
            paper,
            draft,
            [("Experiments", "The method improves the measured success rate.")],
            "fulltext(arxiv)",
        )

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0], CLARITY_REVIEW_SYSTEM)
        self.assertEqual(calls[1][0], FINAL_FACTCHECK_SYSTEM)
        self.assertIn("The method improves", calls[1][1])
        self.assertEqual(result["method"], "局所的に再計画する。")

    def test_multistage_summary_reviews_sections_and_final_draft(self):
        marked = (
            "@@TLDR@@\n衝突回避問題と検証結果を要約する。\n"
            "@@WHAT@@\n複数主体の衝突回避経路を求める問題を扱う。\n"
            "@@CONTRIBUTION@@\n従来法に局所再計画を追加した。\n"
            "@@METHOD@@\n停滞した主体だけを再計画する。\n"
            "@@VALIDATION@@\n複数のベンチマークで成功率を比較した。\n"
            "@@DISCUSSION@@\n混雑時の計算時間が課題として残る。"
        )
        responses = iter(
            [
                "初稿では局所再計画による衝突回避を説明する。",
                "本文に基づき、停滞した主体を局所的に再計画すると説明する。",
                marked,
                marked,
                marked,
            ]
        )
        summarizer = object.__new__(Summarizer)
        summarizer.stub = False
        summarizer.engine = "llm:test"
        calls = []

        def fake_chat(system, user, max_tokens):
            calls.append((system, user, max_tokens))
            return next(responses)

        summarizer._chat = fake_chat
        paper = Paper(
            source="arxiv",
            title="Reliable Multi-Agent Path Finding",
            abstract="We plan collision-free paths.",
            authors=["A. Author"],
        )

        result = summarizer.summarize(
            paper,
            sections=[("Method", "We locally replan stalled agents.")],
            basis="fulltext(arxiv)",
        )

        self.assertEqual(len(calls), 5)
        self.assertIn("本文に基づき", result["sections"][0]["summary"])
        self.assertEqual(result["_basis"], "fulltext(arxiv)")
        self.assertEqual(result["_engine"], "llm:test")

    def test_fulltext_review_failure_does_not_fallback_to_abstract(self):
        summarizer = object.__new__(Summarizer)
        summarizer.stub = False
        paper = Paper(source="arxiv", title="Paper", abstract="Abstract")

        with mock.patch.object(
            summarizer, "_summarize_multi", side_effect=RuntimeError("review failed")
        ), mock.patch.object(summarizer, "_summarize_abstract") as abstract_summary:
            with self.assertRaisesRegex(RuntimeError, "review failed"):
                summarizer.summarize(
                    paper,
                    sections=[("Method", "Source text")],
                    basis="fulltext(arxiv)",
                )

        abstract_summary.assert_not_called()


if __name__ == "__main__":
    unittest.main()

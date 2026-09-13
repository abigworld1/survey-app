import unittest
from unittest import mock

from pipeline.schema import Paper
from pipeline.summarize import (
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


if __name__ == "__main__":
    unittest.main()

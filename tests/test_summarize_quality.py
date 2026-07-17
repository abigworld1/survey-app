import unittest

from pipeline.summarize import (
    Summarizer,
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
        self.assertIn(r"$\mathcal{A}_{lock}$", cleaned)
        self.assertNotIn(r"\text{BestJointSequencing", cleaned)

    def test_japanese_inside_math_falls_back_to_plain_text(self):
        cleaned = _sanitize_generated_text(r"値は $x \text{ は日本語 } y$ である。")

        self.assertEqual(cleaned, "値は x は日本語 y である。")

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


if __name__ == "__main__":
    unittest.main()

"""arXiv 本文（HTML）を取得して本文テキストを抽出する（Phase 1, 依存ゼロ）。

arXiv は新しめの投稿に `https://arxiv.org/html/<id>` で HTML 版を提供しており、
PDF ライブラリ無し（標準ライブラリの html.parser）で本文テキストを取り出せる。
取得・抽出に失敗した場合は空文字を返し、呼び出し側は abstract にフォールバックする。
"""
import os
import re
from html.parser import HTMLParser

from .util import http_get

ARXIV_HTML = "https://arxiv.org/html/"
SKIP_TAGS = {"script", "style", "noscript"}
# 32k コンテキストに対する入力上限（おおよそ 1.4万〜1.6万トークン相当）。環境変数で調整可。
MAX_CHARS = int(os.environ.get("FULLTEXT_MAX_CHARS", "50000"))


class _TextExtractor(HTMLParser):
    """script/style 等を除いて可視テキストだけを集める。"""

    def __init__(self):
        super().__init__()
        self.parts = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth == 0:
            t = data.strip()
            if t:
                self.parts.append(t)


def _strip_references(text):
    """末尾の参考文献（References/Bibliography）以降を落として本文に集中させる。"""
    idx = text.lower().rfind("references")
    if idx > len(text) * 0.5:
        return text[:idx]
    return text


def fetch_arxiv_fulltext(arxiv_id):
    """arXiv HTML から本文テキストを返す。取れなければ ''。"""
    if not arxiv_id:
        return ""
    try:
        html = http_get(
            ARXIV_HTML + arxiv_id, timeout=40, min_interval=3.0, expect="text"
        )
    except Exception:
        return ""
    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:
        return ""
    text = " ".join(parser.parts)
    text = re.sub(r"[ \t]{2,}", " ", text).strip()
    text = _strip_references(text)
    return text[:MAX_CHARS].strip()


# ---- Phase 2: 非arXiv の OA(オープンアクセス) 論文を PDF から本文化 ----

EMAIL = "hirayama.h77@gmail.com"


def _unpaywall_pdf_url(doi):
    """Unpaywall で OA の PDF 直リンクを引く（無ければ ''）。"""
    if not doi:
        return ""
    try:
        data = http_get(
            f"https://api.unpaywall.org/v2/{doi}?email={EMAIL}",
            timeout=20,
            min_interval=0.5,
        )
    except Exception:
        return ""
    loc = data.get("best_oa_location") or {}
    return loc.get("url_for_pdf") or loc.get("url") or ""


def _pdf_to_text(data):
    """PDF バイト列からテキスト抽出。PyMuPDF 未導入なら ''（依存はオプション）。"""
    try:
        import fitz  # PyMuPDF
    except Exception:
        return ""
    try:
        doc = fitz.open(stream=data, filetype="pdf")
        parts = [page.get_text() for page in doc]
        doc.close()
    except Exception:
        return ""
    text = re.sub(r"[ \t]{2,}", " ", "\n".join(parts)).strip()
    return _strip_references(text)[:MAX_CHARS].strip()


def fetch_oa_pdf_text(paper):
    """OA の PDF（paper.pdf_url か Unpaywall 経由）から本文を返す。取れなければ ''。"""
    url = paper.pdf_url or _unpaywall_pdf_url(paper.doi)
    if not url:
        return ""
    try:
        data = http_get(url, timeout=60, min_interval=0.5, expect="bytes")
    except Exception:
        return ""
    if not data[:5].startswith(b"%PDF"):  # HTML ランディング等は弾く
        return ""
    return _pdf_to_text(data)


def fetch_fulltext(paper):
    """本文取得のオーケストレータ。(text, basis) を返す。

    優先順: arXiv HTML > OA PDF > なし(abstract)。
    """
    if paper.arxiv_id:
        t = fetch_arxiv_fulltext(paper.arxiv_id)
        if t:
            return t, "fulltext(arxiv)"
    t = fetch_oa_pdf_text(paper)
    if t:
        return t, "fulltext(oa-pdf)"
    return "", "abstract"

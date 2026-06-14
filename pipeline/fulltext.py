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
ARXIV_PDF = "https://arxiv.org/pdf/"
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
    """本文取得のオーケストレータ（フラット版）。(text, basis) を返す。"""
    if paper.arxiv_id:
        t = fetch_arxiv_fulltext(paper.arxiv_id)
        if t:
            return t, "fulltext(arxiv)"
    t = fetch_oa_pdf_text(paper)
    if t:
        return t, "fulltext(oa-pdf)"
    return "", "abstract"


# ---- セクション単位の本文取得（多段要約用） ----

# 主要セクションだけで分割する（h3以下の定義/証明/補題は親セクションに含める）
SPLIT_HEADING_TAGS = {"h1", "h2"}
SECTION_SKIP_TAGS = {"script", "style", "noscript", "math", "table"}
# 本文でない見出し（参考文献・謝辞・arXivのUI・前文等）は落とす
_DENY_HEADINGS = (
    "reference", "bibliography", "acknowledg", "instructions for reporting",
    "report issue", "github issue", "back to", "why html", "download pdf",
    "appendix", "(前文)",
)
PER_SECTION_MAX = int(os.environ.get("SECTION_MAX_CHARS", "14000"))
MAX_SECTIONS = int(os.environ.get("MAX_SECTIONS", "14"))
MIN_SECTION_CHARS = 150


class _SectionParser(HTMLParser):
    """h1〜h6 の見出しを境界に (見出し, 本文) のリストを作る。"""

    def __init__(self):
        super().__init__()
        self.sections = []
        self._heading = "(前文)"
        self._parts = []
        self._in_heading = False
        self._hbuf = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in SECTION_SKIP_TAGS:
            self._skip += 1
        elif tag in SPLIT_HEADING_TAGS and self._skip == 0:
            self._flush()
            self._in_heading = True
            self._hbuf = []

    def handle_endtag(self, tag):
        if tag in SECTION_SKIP_TAGS and self._skip:
            self._skip -= 1
        elif tag in SPLIT_HEADING_TAGS and self._in_heading:
            self._in_heading = False
            self._heading = " ".join(self._hbuf).strip() or "Section"

    def handle_data(self, data):
        if self._skip:
            return
        t = data.strip()
        if not t:
            return
        (self._hbuf if self._in_heading else self._parts).append(t)

    def _flush(self):
        text = re.sub(r"[ \t]{2,}", " ", " ".join(self._parts)).strip()
        if text:
            self.sections.append((self._heading, text))
        self._heading = ""
        self._parts = []

    def result(self):
        self._flush()
        return self.sections


def _clean_sections(sections):
    out = []
    for heading, text in sections:
        hl = heading.lower()
        if any(d in hl for d in _DENY_HEADINGS):
            continue
        if len(text) < MIN_SECTION_CHARS:
            continue
        out.append((heading[:120], text[:PER_SECTION_MAX]))
        if len(out) >= MAX_SECTIONS:
            break
    return out


def fetch_arxiv_sections(arxiv_id):
    if not arxiv_id:
        return []
    try:
        html = http_get(ARXIV_HTML + arxiv_id, timeout=40, min_interval=3.0, expect="text")
    except Exception:
        return []
    parser = _SectionParser()
    try:
        parser.feed(html)
    except Exception:
        return []
    return _clean_sections(parser.result())


_TEXT_HEADING_RE = re.compile(r"(?m)^\s*(\d{1,2}(?:\.\d{1,2})*\.?\s+[A-Z][A-Za-z0-9 ,\-:]{2,60})\s*$")


def _sections_from_text(text):
    """PDF 等のプレーンテキストを見出しらしい行で分割。無ければサイズで分割。"""
    idxs = [m.start() for m in _TEXT_HEADING_RE.finditer(text)]
    out = []
    if len(idxs) >= 3:
        bounds = idxs + [len(text)]
        for i in range(len(idxs)):
            chunk = text[bounds[i]:bounds[i + 1]]
            head = chunk.split("\n", 1)[0].strip()[:120]
            body = chunk[len(head):].strip()
            if len(body) >= MIN_SECTION_CHARS:
                out.append((head, body[:PER_SECTION_MAX]))
    if not out:
        step = PER_SECTION_MAX
        for i in range(0, min(len(text), step * MAX_SECTIONS), step):
            out.append((f"Part {i // step + 1}", text[i:i + step]))
    return out[:MAX_SECTIONS]


def fetch_arxiv_pdf_text(arxiv_id):
    """arXiv の PDF から本文テキスト（HTML版が無い古い論文向け）。PyMuPDF が必要。"""
    if not arxiv_id:
        return ""
    try:
        data = http_get(ARXIV_PDF + arxiv_id, timeout=60, min_interval=3.0, expect="bytes")
    except Exception:
        return ""
    if not data[:5].startswith(b"%PDF"):
        return ""
    return _pdf_to_text(data)


def fetch_sections(paper):
    """(sections, basis) を返す。sections=[(heading, text)]。取れなければ ([], 'abstract')。"""
    if paper.arxiv_id:
        secs = fetch_arxiv_sections(paper.arxiv_id)
        if secs:
            return secs, "fulltext(arxiv)"
        # 古い arXiv は HTML 版が無い → PDF から本文を取る
        txt = fetch_arxiv_pdf_text(paper.arxiv_id)
        if txt:
            return _sections_from_text(txt), "fulltext(arxiv-pdf)"
    pdf_text = fetch_oa_pdf_text(paper)
    if pdf_text:
        return _sections_from_text(pdf_text), "fulltext(oa-pdf)"
    return [], "abstract"

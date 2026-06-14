"""arXiv 本文（HTML）を取得して本文テキストを抽出する（Phase 1, 依存ゼロ）。

arXiv は新しめの投稿に `https://arxiv.org/html/<id>` で HTML 版を提供しており、
PDF ライブラリ無し（標準ライブラリの html.parser）で本文テキストを取り出せる。
取得・抽出に失敗した場合は空文字を返し、呼び出し側は abstract にフォールバックする。
"""
import os
import re
import unicodedata
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
        parts = [page.get_text("text", sort=True) for page in doc]  # 読み順で抽出
        doc.close()
    except Exception:
        return ""
    # 合字(ﬁ→fi)や互換文字を正規化
    text = unicodedata.normalize("NFKC", "\n".join(parts))
    text = re.sub(r"[ \t]{2,}", " ", text).strip()
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


def _sections_from_text(text):
    """PDF 等のプレーンテキストを読み順に一定サイズで分割する。

    PDF からの見出し検出は（2段組などで）不安定なので行わず、読み順のまま
    一定サイズのチャンクに分け、クリーンなラベル「本文 (i/n)」を付ける。
    """
    text = (text or "").strip()
    if not text:
        return []
    size = max(3000, PER_SECTION_MAX // 2)  # 1チャンク約7000字
    chunks = [text[i:i + size] for i in range(0, len(text), size)][:MAX_SECTIONS]
    n = len(chunks)
    return [(f"本文 ({i + 1}/{n})", c) for i, c in enumerate(chunks)]


def _download_pdf(url, min_interval=0.5):
    """URL から PDF バイト列を取得（PDFでなければ None）。"""
    if not url:
        return None
    try:
        data = http_get(url, timeout=60, min_interval=min_interval, expect="bytes")
    except Exception:
        return None
    return data if data[:5].startswith(b"%PDF") else None


# 見出しに現れやすい語（フォント検出の補助）
_SECTION_WORDS = (
    "introduction", "related work", "background", "preliminar", "problem",
    "method", "approach", "algorithm", "framework", "model", "architecture",
    "experiment", "evaluation", "setup", "result", "analysis", "ablation",
    "discussion", "limitation", "conclusion", "future work",
)


def _is_heading(txt, size, bold, body_size):
    """本文より大きい/太字で、見出しらしい短い行か。"""
    if len(txt) < 3 or len(txt) > 90 or len(txt.split()) > 12:
        return False
    strong = (size >= body_size + 0.6) or (bold and size >= body_size - 0.1)
    if not strong:
        return False
    hl = txt.lower()
    # 図表・式・アルゴリズムのキャプション/フロートは見出しにしない
    if re.match(r"^(figure|fig\.?|table|tab\.?|algorithm|equation|eq\.?)\s*\d", hl):
        return False
    numbered = bool(re.match(r"^\d+(?:\.\d+)*\.?\s+\S", txt))
    known = any(w in hl for w in _SECTION_WORDS)
    titleish = (txt == txt.upper()) or txt[:1].isupper()
    return numbered or known or (titleish and size >= body_size + 0.6)


def _mode_size(lines):
    """行リストから本文サイズ（文字数で最頻のフォントサイズ）を返す。"""
    w = {}
    for size, _bold, txt in lines:
        w[size] = w.get(size, 0) + len(txt)
    return max(w, key=w.get) if w else 0


def _pdf_lines_in_order(data):
    """PDF を読み順（2段組対応）で (size, bold, text) の行リストにする。NFKC 正規化。"""
    try:
        import fitz
    except Exception:
        return []
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception:
        return []
    lines = []
    for page in doc:
        try:
            d = page.get_text("dict")
        except Exception:
            continue
        # PDF のコンテンツストリーム順（自然な読み順）をそのまま使う。
        # 段組を中心xで並べ替えるとフルWidthのタイトル/ABSTRACTが誤判定され順序が崩れるため。
        for b in d.get("blocks", []):
            if not b.get("lines"):
                continue
            for ln in b["lines"]:
                ss = ln.get("spans", [])
                if not ss:
                    continue
                txt = unicodedata.normalize(
                    "NFKC", "".join(s.get("text", "") for s in ss)
                ).strip()
                if not txt:
                    continue
                s0 = ss[0]
                size = round(float(s0.get("size", 0)), 1)
                font = str(s0.get("font", "")).lower()
                bold = bool(int(s0.get("flags", 0)) & 16) or "bold" in font or "black" in font
                lines.append((size, bold, txt))
    doc.close()
    return lines


def _build_sections(lines):
    """(size,bold,text) の読み順行リストを、見出しで (見出し, 本文) に区切る。

    短すぎる節（タイトル断片など）は直前に統合し、References 以降は打ち切る。
    """
    if not lines:
        return []
    body_size = _mode_size(lines)
    raw, cur = [], None
    for size, bold, txt in lines:
        if _is_heading(txt, size, bold, body_size):
            if cur:
                raw.append(cur)
            cur = [txt, []]
        elif cur is not None:
            cur[1].append(txt)
    if cur:
        raw.append(cur)

    out = []
    for heading, parts in raw:
        if any(d in heading.lower() for d in _DENY_HEADINGS):
            break  # References/謝辞/付録 以降は打ち切り
        body = re.sub(r"[ \t]{2,}", " ", " ".join(parts)).strip()
        # ほぼ空（タイトル直後の著者行など）だけ捨てる。実セクションは短くても見出し名を残す。
        if len(body) < 40:
            continue
        out.append((heading[:120], body[:PER_SECTION_MAX]))
        if len(out) >= MAX_SECTIONS:
            break
    return out


def _sections_from_pdf(data):
    """PDF を読み順にたどり、フォント見出しでセクション化。<2見出しなら 本文(i/n) に。"""
    secs = _build_sections(_pdf_lines_in_order(data))
    return secs if len(secs) >= 2 else _sections_from_text(_pdf_to_text(data))


def fetch_sections(paper):
    """(sections, basis) を返す。sections=[(heading, text)]。取れなければ ([], 'abstract')。"""
    if paper.arxiv_id:
        secs = fetch_arxiv_sections(paper.arxiv_id)
        if secs:
            return secs, "fulltext(arxiv)"
        # 古い arXiv は HTML 版が無い → PDF をフォントベースで分割
        data = _download_pdf(ARXIV_PDF + paper.arxiv_id, min_interval=3.0)
        if data:
            secs = _sections_from_pdf(data)
            if secs:
                return secs, "fulltext(arxiv-pdf)"
    # 非arXiv の OA PDF
    url = paper.pdf_url or _unpaywall_pdf_url(paper.doi)
    if url:
        data = _download_pdf(url)
        if data:
            secs = _sections_from_pdf(data)
            if secs:
                return secs, "fulltext(oa-pdf)"
    return [], "abstract"

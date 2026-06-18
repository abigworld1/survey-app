#!/usr/bin/env python3
"""任意の論文を手動で1枚 HTML 化する（日次cronとは独立）。

使い方（repo ルートで、LLM 環境変数を付けて実行）:
  python -m pipeline.add_paper --arxiv 2606.12345
  python -m pipeline.add_paper --pdf ~/papers/foo.pdf --title "論文タイトル"
  python -m pipeline.add_paper --url https://example.org/paper.pdf

生成物は <field>/<id>.html（既定 field=reading「個別に読んだ論文」）。
その後 git add/commit/push で公開。PDF 抽出には PyMuPDF が必要。
"""
import argparse
import datetime
import os
import re
import sys

import yaml

from . import render
from .dedup import load_seen, save_seen
from .fulltext import _pdf_to_text, _sections_from_text, fetch_sections
from .schema import Paper
from .sources import arxiv as arxiv_src
from .summarize import Summarizer
from .util import http_get, slugify

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TPL = os.path.join(ROOT, "templates")
SEEN = os.path.join(ROOT, "data", "seen.json")
DEFAULT_FIELD = "reading"


def _load_subs():
    with open(os.path.join(ROOT, "subscriptions.yml"), encoding="utf-8") as f:
        return (yaml.safe_load(f) or {}).get("subscriptions", [])


def _matched_keywords(paper, keywords):
    title = (paper.title or "").lower()
    abstract = (paper.abstract or "").lower()
    out = []
    for kw in keywords:
        word = (kw or "").strip()
        if not word:
            continue
        pt = re.compile(r"\b" + re.escape(word.lower()) + r"\b")
        if pt.search(title) or pt.search(abstract):
            out.append(word)
    return out


def _source_quality(basis):
    return "fulltext" if str(basis or "").startswith("fulltext") else "abstract"


def _from_arxiv(raw):
    aid = raw.rsplit("/abs/", 1)[-1].replace("arxiv:", "").replace("arXiv:", "").strip()
    paper = arxiv_src.fetch_meta(aid) or Paper(source="arxiv", title=aid, arxiv_id=aid)
    paper.arxiv_id = aid
    # HTML → PDF(古い論文) → abstract の順で本文取得（基準も実態に合わせて返す）
    sections, basis = fetch_sections(paper)
    return paper, sections, basis


def _from_pdf_bytes(data, title, url=""):
    text = _pdf_to_text(data)
    if not text:
        raise SystemExit("PDFからテキスト抽出に失敗（PyMuPDF未導入、または画像PDF）。")
    if not title:
        try:
            import fitz

            title = (fitz.open(stream=data, filetype="pdf").metadata or {}).get("title") or ""
        except Exception:
            title = ""
    if not title:
        title = text.strip().split("\n", 1)[0][:120]
    paper = Paper(source="pdf", title=(title.strip() or "Untitled"), url=url)
    return paper, _sections_from_text(text), "fulltext(pdf)"


def main(argv=None):
    ap = argparse.ArgumentParser(description="手動で論文を1枚HTML化")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--arxiv", help="arXiv ID または URL")
    src.add_argument("--pdf", help="ローカルPDFのパス")
    src.add_argument("--url", help="PDF の URL")
    ap.add_argument("--title", default="", help="タイトル（PDFで自動取得できない場合に指定）")
    # 追加先フィールド（指定なしは reading）。--mapf / --rag はショートカット。
    dest = ap.add_mutually_exclusive_group()
    dest.add_argument("--mapf", dest="field", action="store_const",
                      const="mapf-mapd-warehouse", help="MAPF/MAPD/倉庫 分野に追加")
    dest.add_argument("--rag", dest="field", action="store_const",
                      const="doc-structure-rag", help="文書構造解析/RAG 分野に追加")
    dest.add_argument("--field", default=None,
                      help="任意のフィールドスラッグに追加（既定: reading = 個別に読んだ論文）")
    args = ap.parse_args(argv)
    field = args.field or DEFAULT_FIELD

    if args.arxiv:
        paper, sections, basis = _from_arxiv(args.arxiv)
    elif args.pdf:
        with open(os.path.expanduser(args.pdf), "rb") as f:
            paper, sections, basis = _from_pdf_bytes(f.read(), args.title)
    else:
        data = http_get(args.url, expect="bytes", timeout=60)
        if not data[:5].startswith(b"%PDF"):
            raise SystemExit("URL の中身が PDF ではありません。")
        paper, sections, basis = _from_pdf_bytes(data, args.title, url=args.url)

    print(f"タイトル: {paper.title}")
    print(f"セクション数: {len(sections)} / 根拠: {basis}")

    summarizer = Summarizer()
    print(f"要約エンジン: {summarizer.engine}")
    summary = summarizer.summarize(paper, sections=sections, basis=basis)

    uslug = slugify(field, fallback="reading")
    subs = _load_subs()
    sub = next((s for s in subs if slugify(s.get("username", "")) == uslug), {})
    matched_keywords = _matched_keywords(paper, sub.get("keywords", []))
    paper.matched_keywords = matched_keywords
    summary.update(summarizer.rate_reading_value(paper, summary, basis))
    paper.selection_type = "manual"
    paper.selection_label = "手動追加"
    paper.relevance_score = len(matched_keywords)
    paper.source_quality = _source_quality(summary.get("_basis", basis))
    paper.reading_value = summary.get("_reading_value", "")
    paper.reading_value_reason = summary.get("_reading_value_reason", "")
    pid = slugify(args.title or paper.title or paper.paper_id(), fallback="paper")
    rel = f"{uslug}/{pid}.html"
    os.makedirs(os.path.join(ROOT, uslug), exist_ok=True)
    with open(os.path.join(ROOT, rel), "w", encoding="utf-8") as f:
        f.write(render.render_paper_page(TPL, paper, summary))

    # seen 更新 → フィールドindex・全体index を再生成
    seen = load_seen(SEEN)
    useen = seen.setdefault(uslug, {})
    added_at = datetime.datetime.now().isoformat(timespec="seconds")
    useen[paper.key()] = {
        "title": paper.title,
        "file": rel,
        "date": paper.published,
        "venue": render._venue_label(paper.venue, missing=""),
        "url": paper.url,
        "pdf_url": paper.pdf_url,
        "arxiv_id": paper.arxiv_id,
        "doi": paper.doi,
        "added": added_at[:10],
        "added_at": added_at,
        "authors": paper.authors,
        "tldr": summary.get("tldr", ""),
        "engine": summary.get("_engine", ""),
        "basis": summary.get("_basis", ""),
        "matched_keywords": matched_keywords,
        "selection": "manual",
        "selection_label": "手動追加",
        "citations": paper.citations,
        "relevance": len(matched_keywords),
        "source_quality": _source_quality(summary.get("_basis", basis)),
        "reading_value": summary.get("_reading_value", ""),
        "reading_value_reason": summary.get("_reading_value_reason", ""),
    }
    if uslug not in {slugify(s.get("username", "")) for s in subs}:
        print(f"  [note] '{uslug}' は subscriptions.yml に無いため、トップ一覧には出ません（ページは生成されます）。")
    label = next(
        (s.get("label") for s in subs if slugify(s.get("username", "")) == uslug), None
    ) or field
    render.render_user_index(TPL, ROOT, uslug, label, useen, sub.get("keywords", []))
    render.render_global_index(TPL, ROOT, subs, seen, slugify)
    save_seen(SEEN, seen)

    print(f"生成: {rel}")
    print("公開: git add -A && git commit -m 'add paper' && git push origin main")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""既存の論文HTMLを、現在のテンプレートで再描画する。

既存HTMLに含まれる要約本文・リンク・セクション詳細を保持しつつ、
paper.html の見た目や上部メタ情報だけを最新化する。LLMやネットワークは使わない。
"""
import argparse
import html
import os
import re
import sys

import yaml

from . import render
from .dedup import load_seen
from .schema import Paper
from .util import slugify

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TPL = os.path.join(ROOT, "templates")
SEEN = os.path.join(ROOT, "data", "seen.json")


def _load_subs():
    with open(os.path.join(ROOT, "subscriptions.yml"), encoding="utf-8") as f:
        return (yaml.safe_load(f) or {}).get("subscriptions", [])


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _write(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _strip_tags(value):
    value = re.sub(r"<[^>]+>", "", value or "")
    return html.unescape(value).strip()


def _find(pattern, text, default=""):
    m = re.search(pattern, text, re.S)
    return m.group(1).strip() if m else default


def _parse_meta(text, info):
    meta = _find(r'<div class="meta">(.*?)</div>', text)
    authors = info.get("authors") or []
    if isinstance(authors, list):
        authors_text = ", ".join(str(a).strip() for a in authors if str(a).strip())
    else:
        authors_text = str(authors or "").strip()
    venue = ""
    date = info.get("date", "")
    source = ""
    if "<br>" in meta:
        raw_authors, rest = meta.split("<br>", 1)
        authors_text = authors_text or _strip_tags(raw_authors)
        rest_text = _strip_tags(rest)
        m = re.match(r"(.+?) ・ ([^・]+) ・ source: (.+)$", rest_text)
        if m:
            venue, date, source = (x.strip() for x in m.groups())
    return authors_text, venue, date, source


def _summary_body(text):
    tldr_m = re.search(r'<div class="tldr"><strong>一言で:</strong>\s*(.*?)</div>', text, re.S)
    footer_m = re.search(r"\n\s*<footer>", text)
    if not tldr_m or not footer_m or footer_m.start() <= tldr_m.end():
        return "", ""
    return tldr_m.group(1).strip(), text[tldr_m.end():footer_m.start()].strip()


def _old_footer_value(text, name):
    if name == "engine":
        return _strip_tags(_find(r"要約エンジン:\s*(.*?)\s*・\s*生成日", text))
    if name == "basis":
        return _strip_tags(_find(r"情報源:\s*(.*?)\s*・\s*要約エンジン", text))
    return ""


def _safe_path(rel):
    root_abs = os.path.abspath(ROOT)
    path = os.path.abspath(os.path.join(root_abs, rel))
    if not (path == root_abs or path.startswith(root_abs + os.sep)):
        raise ValueError(f"unsafe path: {rel}")
    return path


def _matched_keywords(title, tldr, keywords):
    text = f"{title} {tldr}".lower()
    out = []
    for kw in keywords or []:
        word = str(kw or "").strip()
        if not word:
            continue
        if re.search(r"\b" + re.escape(word.lower()) + r"\b", text):
            out.append(word)
    return out


def _rebuild_one(rel, info, keywords=None, dry_run=False):
    path = _safe_path(rel)
    text = _read(path)
    title = info.get("title") or _strip_tags(_find(r"<h1>(.*?)</h1>", text))
    authors_text, venue, date, source = _parse_meta(text, info)
    tldr, body = _summary_body(text)
    if not title or not tldr or not body:
        raise ValueError("title/tldr/body を既存HTMLから抽出できません")

    links = _find(r'<div class="links">(.*?)</div>', text)
    basis = info.get("basis") or _old_footer_value(text, "basis") or "abstract"
    engine = info.get("engine") or _old_footer_value(text, "engine")
    matched_keywords = info.get("matched_keywords") or _matched_keywords(title, tldr, keywords)
    paper = Paper(
        source=source,
        title=title,
        authors=[a.strip() for a in authors_text.split(",") if a.strip()],
        published=date,
        venue=venue,
        citations=info.get("citations", 0),
    )
    paper.citations_known = "citations" in info
    paper.matched_keywords = matched_keywords
    paper.selection_type = info.get("selection", "")
    paper.selection_label = info.get("selection_label", "")
    if info.get("relevance") is not None:
        paper.relevance_score = info.get("relevance")

    summary = {
        "tldr": tldr,
        "_basis": basis,
        "_engine": engine,
        "_reading_value": info.get("reading_value", ""),
        "_reading_value_reason": info.get("reading_value_reason", ""),
    }
    ctx = {
        "title": render._esc(title),
        "authors": render._esc(authors_text),
        "venue": render._esc(venue or source),
        "published": render._esc(date),
        "source": render._esc(source),
        "links": links,
        "paper_facts": render._paper_facts(paper, summary),
        "source_notice": render._source_notice(summary),
        "keyword_tags": render._keyword_tags(matched_keywords),
        "tldr": tldr,
        "sections": body,
        "sections_detail": "",
        "basis": render._basis_label(basis),
        "engine": render._esc(engine),
        "generated": render._today(),
    }
    out = render.render_template(_read(os.path.join(TPL, "paper.html")), ctx)
    if not dry_run:
        _write(path, out)


def main(argv=None):
    ap = argparse.ArgumentParser(description="既存HTMLを現在のテンプレートで再描画")
    ap.add_argument("--dry-run", action="store_true", help="書き換えずに対象と抽出結果だけ確認")
    args = ap.parse_args(argv)

    seen = load_seen(SEEN)
    subs = _load_subs()
    rebuilt = 0
    skipped = 0
    for uslug, useen in seen.items():
        sub = next((s for s in subs if slugify(s.get("username", "")) == uslug), {})
        keywords = sub.get("keywords", [])
        for info in useen.values():
            rel = info.get("file", "")
            if not rel or os.path.basename(rel) == "index.html":
                continue
            try:
                _rebuild_one(rel, info, keywords=keywords, dry_run=args.dry_run)
                rebuilt += 1
                print(("確認" if args.dry_run else "再生成") + f": {rel}")
            except Exception as e:
                skipped += 1
                print(f"[skip] {rel}: {e}")

    if not args.dry_run:
        for sub in subs:
            user = (sub.get("username") or "").strip()
            if not user:
                continue
            uslug = slugify(user, fallback="user")
            render.render_user_index(
                TPL,
                ROOT,
                uslug,
                sub.get("label") or user,
                seen.get(uslug, {}),
                sub.get("keywords", []),
            )
        render.render_global_index(TPL, ROOT, subs, seen, slugify)
    print(f"完了: {rebuilt} 件再生成 / {skipped} 件スキップ (dry-run={args.dry_run})")
    return 1 if skipped else 0


if __name__ == "__main__":
    sys.exit(main())

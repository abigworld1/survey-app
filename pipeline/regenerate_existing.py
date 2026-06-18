#!/usr/bin/env python3
"""既存登録済み論文を再取得・再要約してHTMLを作り直す。

data/seen.json の登録を入力として、arXiv/DOI/タイトルからメタデータを再取得し、
fulltext.fetch_sections → Summarizer → render_paper_page を実行する。
既存のURLは保ったままHTMLを上書きし、seen.jsonのメタデータも更新する。
"""
import argparse
import datetime
import os
import re
import sys
import time
import urllib.error
import urllib.parse

import yaml

from . import render
from .dedup import load_seen, save_seen
from .fulltext import fetch_sections
from .schema import Paper
from .sources import arxiv as arxiv_src
from .summarize import Summarizer
from .util import http_get, slugify

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TPL = os.path.join(ROOT, "templates")
SEEN = os.path.join(ROOT, "data", "seen.json")

S2_FIELDS = "title,abstract,year,publicationDate,venue,authors,externalIds,url,openAccessPdf,citationCount"


def _load_subs():
    with open(os.path.join(ROOT, "subscriptions.yml"), encoding="utf-8") as f:
        return (yaml.safe_load(f) or {}).get("subscriptions", [])


def _keyword_patterns(keywords):
    return [
        re.compile(r"\b" + re.escape(str(w).lower().strip()) + r"\b")
        for w in keywords or []
        if str(w or "").strip()
    ]


def _matched_keywords(paper, keywords):
    title = (paper.title or "").lower()
    abstract = (paper.abstract or "").lower()
    out = []
    for kw in keywords or []:
        word = str(kw or "").strip()
        if not word:
            continue
        pt = re.compile(r"\b" + re.escape(word.lower()) + r"\b")
        if pt.search(title) or pt.search(abstract):
            out.append(word)
    return out


def _relevance(paper, patterns):
    title = (paper.title or "").lower()
    abstract = (paper.abstract or "").lower()
    return sum(
        (3 if pt.search(title) else 0) + (1 if pt.search(abstract) else 0)
        for pt in patterns
    )


def _citations(paper):
    try:
        return int(paper.citations or 0)
    except (TypeError, ValueError):
        return 0


def _source_quality(basis):
    return "fulltext" if str(basis or "").startswith("fulltext") else "abstract"


def _from_s2(item):
    ext = item.get("externalIds") or {}
    published = item.get("publicationDate") or (str(item.get("year")) if item.get("year") else "")
    return Paper(
        source="semanticscholar",
        title=item.get("title") or "",
        abstract=item.get("abstract") or "",
        authors=[a.get("name") for a in (item.get("authors") or []) if a.get("name")],
        published=published,
        venue=item.get("venue") or "",
        url=item.get("url", ""),
        pdf_url=(item.get("openAccessPdf") or {}).get("url", "") or "",
        arxiv_id=ext.get("ArXiv", "") or "",
        doi=ext.get("DOI", "") or "",
        citations=int(item.get("citationCount") or 0),
    )


def _from_openalex(item):
    inv = item.get("abstract_inverted_index") or {}
    pos = {}
    for word, idxs in inv.items():
        for i in idxs:
            pos[i] = word
    abstract = " ".join(pos[i] for i in sorted(pos)) if pos else ""
    authors = [
        (a.get("author") or {}).get("display_name")
        for a in item.get("authorships", [])
    ]
    venue = (((item.get("primary_location") or {}).get("source") or {}).get("display_name") or "")
    best = item.get("best_oa_location") or {}
    oa = item.get("open_access") or {}
    return Paper(
        source="openalex",
        title=item.get("title") or item.get("display_name") or "",
        abstract=abstract,
        authors=[a for a in authors if a],
        published=(item.get("publication_date") or "")[:10],
        venue=venue,
        url=item.get("id", ""),
        pdf_url=best.get("pdf_url") or oa.get("oa_url") or "",
        doi=(item.get("doi") or "").replace("https://doi.org/", ""),
        citations=int(item.get("cited_by_count") or 0),
    )


def _read_existing_html(info):
    rel = info.get("file", "")
    if not rel:
        return ""
    root_abs = os.path.abspath(ROOT)
    path = os.path.abspath(os.path.join(root_abs, rel))
    if not (path == root_abs or path.startswith(root_abs + os.sep)):
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def _strip_tags(value):
    value = re.sub(r"<[^>]+>", "", value or "")
    return value.strip()


def _existing_paper(info):
    text = _read_existing_html(info)
    links = re.findall(r'<a href="([^"]+)"', text)
    pdf_url = next((u for u in links if ".pdf" in u or "/pdf/" in u), "")
    url = next((u for u in links if u != pdf_url), "")
    doi_m = re.search(r"https?://doi\.org/([^\"<]+)", text)
    doi = doi_m.group(1).strip() if doi_m else ""
    arxiv_m = re.search(r"arxiv\.org/(?:abs|pdf)/([0-9][0-9.]+)(?:v\d+)?", text, re.I)
    arxiv_id = arxiv_m.group(1) if arxiv_m else ""
    meta = re.search(r'<div class="meta">(.*?)</div>', text, re.S)
    authors = []
    venue = ""
    source = "existing"
    if meta:
        parts = meta.group(1).split("<br>", 1)
        authors = [a.strip() for a in _strip_tags(parts[0]).split(",") if a.strip()]
        if len(parts) > 1:
            rest = _strip_tags(parts[1])
            m = re.match(r"(.+?) ・ ([^・]+) ・ source: (.+)$", rest)
            if m:
                venue, _, source = (x.strip() for x in m.groups())
                venue = render._clean_venue(venue)
    if arxiv_id:
        paper = arxiv_src.fetch_meta(arxiv_id)
        if paper:
            paper.pdf_url = paper.pdf_url or pdf_url or f"https://arxiv.org/pdf/{arxiv_id}"
            paper.venue = paper.venue or render._venue_label(venue, missing="")
            return paper
    return Paper(
        source=source,
        title=info.get("title", ""),
        authors=authors,
        published=info.get("date", ""),
        venue=venue,
        url=url,
        pdf_url=pdf_url,
        doi=doi,
        arxiv_id=arxiv_id,
    )


def _retry_fetch(fetch, value, label):
    last = None
    for attempt in range(4):
        try:
            return fetch(value)
        except urllib.error.HTTPError as e:
            last = e
            if e.code not in {429, 500, 502, 503, 504} or attempt >= 3:
                raise
            wait = 5 * (attempt + 1)
            print(f"      [retry] {label}: HTTP {e.code}、{wait}秒待機")
            time.sleep(wait)
        except Exception:
            raise
    raise last


def _fetch_s2_by_doi(doi):
    q = urllib.parse.quote("DOI:" + doi, safe="")
    data = http_get(
        f"https://api.semanticscholar.org/graph/v1/paper/{q}?fields={S2_FIELDS}",
        timeout=40,
        min_interval=1.2,
    )
    return _from_s2(data) if data.get("title") else None


def _fetch_openalex_by_doi(doi):
    q = urllib.parse.urlencode(
        {
            "filter": "doi:" + doi,
            "per-page": 1,
            "mailto": "hirayama.h77@gmail.com",
        }
    )
    data = http_get("https://api.openalex.org/works?" + q, timeout=40, min_interval=0.2)
    results = data.get("results") or []
    return _from_openalex(results[0]) if results else None


def _search_s2_by_title(title):
    q = urllib.parse.urlencode({"query": title, "limit": 1, "fields": S2_FIELDS})
    data = http_get(
        "https://api.semanticscholar.org/graph/v1/paper/search?" + q,
        timeout=40,
        min_interval=1.2,
    )
    results = data.get("data") or []
    return _from_s2(results[0]) if results else None


def _search_openalex_by_title(title):
    q = urllib.parse.urlencode(
        {
            "search": title,
            "per-page": 1,
            "mailto": "hirayama.h77@gmail.com",
        }
    )
    data = http_get("https://api.openalex.org/works?" + q, timeout=40, min_interval=0.2)
    results = data.get("results") or []
    return _from_openalex(results[0]) if results else None


def _resolve_paper(key, info):
    if key.startswith("arxiv:"):
        arxiv_id = re.sub(r"v\d+$", "", key.split(":", 1)[1])
        paper = arxiv_src.fetch_meta(arxiv_id)
        if paper:
            paper.pdf_url = paper.pdf_url or f"https://arxiv.org/pdf/{arxiv_id}"
            return paper
    if key.startswith("doi:"):
        doi = key.split(":", 1)[1]
        for fetch in (_fetch_s2_by_doi, _fetch_openalex_by_doi):
            try:
                paper = _retry_fetch(fetch, doi, f"DOI {fetch.__name__}")
            except Exception as e:
                print(f"      [warn] DOI取得失敗 {fetch.__name__}: {e!r}")
                paper = None
            if paper:
                return paper
    title = info.get("title") or key.removeprefix("title:")
    for fetch in (_search_s2_by_title, _search_openalex_by_title):
        try:
            paper = _retry_fetch(fetch, title, f"タイトル検索 {fetch.__name__}")
        except Exception as e:
            print(f"      [warn] タイトル検索失敗 {fetch.__name__}: {e!r}")
            paper = None
        if paper:
            return paper
    fallback = _existing_paper(info)
    if fallback and (fallback.pdf_url or fallback.url or fallback.doi or fallback.arxiv_id):
        print("      [note] 既存HTMLのリンクから再取得します")
        return fallback
    return None


def _safe_path(rel):
    root_abs = os.path.abspath(ROOT)
    path = os.path.abspath(os.path.join(root_abs, rel))
    if not (path == root_abs or path.startswith(root_abs + os.sep)):
        raise ValueError(f"unsafe path: {rel}")
    return path


def main(argv=None):
    ap = argparse.ArgumentParser(description="既存HTMLを再取得・LLM再要約で作り直す")
    ap.add_argument("--field", help="対象分野スラッグ。省略時は全分野")
    ap.add_argument("--limit", type=int, default=0, help="処理件数上限。0なら無制限")
    ap.add_argument("--dry-run", action="store_true", help="取得可能性だけ確認し、要約・書き換えしない")
    ap.add_argument("--require-fulltext", action="store_true", help="本文が取れない論文は上書きしない")
    args = ap.parse_args(argv)

    seen = load_seen(SEEN)
    subs = _load_subs()
    sub_by_slug = {slugify(s.get("username", ""), fallback="user"): s for s in subs}
    summarizer = None if args.dry_run else Summarizer()
    if summarizer:
        print(f"要約エンジン: {summarizer.engine}")

    targets = []
    for uslug, useen in seen.items():
        if args.field and uslug != slugify(args.field, fallback=args.field):
            continue
        for key, info in useen.items():
            targets.append((uslug, key, info))
    if args.limit > 0:
        targets = targets[: args.limit]

    today = datetime.date.today().isoformat()
    rebuilt = 0
    skipped = 0
    for uslug, key, info in targets:
        rel = info.get("file", "")
        print(f"\n=== {rel or key} ===")
        paper = _resolve_paper(key, info)
        if not paper:
            skipped += 1
            print("  [skip] メタデータを再取得できません")
            continue
        if not render._venue_label(paper.venue, missing=""):
            try:
                fallback = info.get("venue", "") or _existing_paper(info).venue
            except OSError:
                fallback = info.get("venue", "")
            paper.venue = render._venue_label(fallback, missing="")
        sub = sub_by_slug.get(uslug, {})
        keywords = sub.get("keywords", [])
        patterns = _keyword_patterns(keywords)
        matched_keywords = _matched_keywords(paper, keywords)
        relevance = _relevance(paper, patterns)
        paper.matched_keywords = matched_keywords

        if args.dry_run:
            print(
                f"  確認: {paper.title} / 関連度{relevance} / "
                f"被引用{_citations(paper)} / キーワード{len(matched_keywords)}"
            )
            continue

        sections, basis = fetch_sections(paper)
        print(
            f"  再要約: 関連度{relevance} / 被引用{_citations(paper)} / "
            f"{len(sections)}セクション / 根拠 {basis}"
        )
        if args.require_fulltext and not str(basis or "").startswith("fulltext"):
            skipped += 1
            print("  [skip] 本文未取得のため上書きしません")
            continue
        if not sections and not (paper.abstract or "").strip():
            skipped += 1
            print("  [skip] 本文もアブストラクトも取得できません")
            continue

        summary = summarizer.summarize(paper, sections=sections, basis=basis)
        summary.update(summarizer.rate_reading_value(paper, summary, basis))

        paper.selection_type = info.get("selection", "")
        paper.selection_label = info.get("selection_label", "")
        paper.relevance_score = relevance
        paper.source_quality = _source_quality(summary.get("_basis", basis))
        paper.reading_value = summary.get("_reading_value", "")
        paper.reading_value_reason = summary.get("_reading_value_reason", "")

        path = _safe_path(rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(render.render_paper_page(TPL, paper, summary))

        info.update(
            {
                "title": paper.title,
                "file": rel,
                "date": paper.published,
                "venue": render._venue_label(paper.venue, missing=""),
                "authors": paper.authors,
                "tldr": summary.get("tldr", ""),
                "engine": summary.get("_engine", ""),
                "basis": summary.get("_basis", ""),
                "matched_keywords": matched_keywords,
                "citations": _citations(paper),
                "relevance": relevance,
                "source_quality": _source_quality(summary.get("_basis", basis)),
                "reading_value": summary.get("_reading_value", ""),
                "reading_value_reason": summary.get("_reading_value_reason", ""),
                "regenerated": today,
            }
        )
        rebuilt += 1
        print(f"  + {rel}")

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
        save_seen(SEEN, seen)

    print(f"\n完了: {rebuilt} 件再生成 / {skipped} 件スキップ (dry-run={args.dry_run})")
    return 1 if skipped and not args.dry_run else 0


if __name__ == "__main__":
    sys.exit(main())

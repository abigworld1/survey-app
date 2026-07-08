#!/usr/bin/env python3
"""日次パイプラインの本体。

  取得(sources) -> 名寄せ(dedup) -> 既出除外(seen) -> 要約(LLM) -> HTML生成 -> seen更新

実行（repo ルートから）:
  python -m pipeline.run            # 実運用（vLLMで要約）
  python -m pipeline.run --offline  # ネット未使用・サンプル＋スタブで動作確認
  python -m pipeline.run --stub     # 論文は取得するが要約はスタブ
  python -m pipeline.run --dry-run  # 生成も seen 更新もしない
"""
import argparse
import datetime
import difflib
import html
import json
import os
import re
import shutil
import sys

import yaml

from . import render, sources
from .dedup import dedup, load_seen, save_seen
from .fulltext import fetch_sections
from .schema import Paper, normalize_title
from .sources import arxiv as arxiv_src
from .summarize import Summarizer
from .util import slugify


def _fulltext_score(p):
    """本文の取りやすさで採用を優先する（arXiv > OA-PDF候補 > DOIあり > なし）。"""
    if p.arxiv_id:
        return 3
    if p.pdf_url:
        return 2
    if p.doi:
        return 1
    return 0


def _title_similarity(a, b):
    an = normalize_title(a)
    bn = normalize_title(b)
    if not an or not bn:
        return 0.0
    if an == bn:
        return 1.0
    return difflib.SequenceMatcher(None, an, bn).ratio()


def _find_arxiv_by_title(title):
    try:
        results = arxiv_src.search([title], limit=5, mode="recent")
    except Exception as e:
        print(f"      [warn] arXivタイトル補完失敗: {e!r}")
        return None
    candidates = [p for p in results if _title_similarity(p.title, title) >= 0.82]
    if not candidates:
        return None
    return max(candidates, key=lambda p: (_title_similarity(p.title, title), p.published or ""))


def _enrich_fulltext_source(paper):
    """採択先・被引用数を保ったまま、本文取得用のarXiv/PDF情報を補完する。"""
    if paper.arxiv_id:
        return paper
    arxiv_paper = _find_arxiv_by_title(paper.title)
    if not arxiv_paper:
        return paper
    paper.arxiv_id = paper.arxiv_id or arxiv_paper.arxiv_id
    paper.pdf_url = paper.pdf_url or arxiv_paper.pdf_url
    paper.abstract = paper.abstract or arxiv_paper.abstract
    paper.authors = paper.authors or arxiv_paper.authors
    paper.published = paper.published or arxiv_paper.published
    paper.url = paper.url or arxiv_paper.url
    print(f"      [note] arXiv本文候補を補完: {paper.arxiv_id}")
    return paper


def _keyword_patterns(keywords):
    """キーワードを単語境界マッチ用の正規表現に（'RAG' が 'storage' に誤マッチしない）。"""
    return [
        re.compile(r"\b" + re.escape(w.lower().strip()) + r"\b")
        for w in keywords
        if w and w.strip()
    ]


def _relevance(paper, patterns):
    """キーワード適合度。タイトル一致=3点、アブストラクト一致=1点。"""
    title = (paper.title or "").lower()
    abstract = (paper.abstract or "").lower()
    return sum(
        (3 if pt.search(title) else 0) + (1 if pt.search(abstract) else 0)
        for pt in patterns
    )


def _matched_keywords(paper, keywords):
    """タイトルまたはアブストラクトに一致した購読キーワードを返す。"""
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


def _citations(paper):
    """被引用数。取れないソースは 0 として扱う。"""
    try:
        return int(paper.citations or 0)
    except (TypeError, ValueError):
        return 0


def _important_quota(k):
    """1本は新着枠として残し、最大2本を重要論文枠にする。"""
    if k <= 1:
        return 0
    return min(2, k - 1)


def _rank_recent(papers, patterns):
    """新着枠: 関連度 → 本文の取りやすさ → 新しさ。"""
    return sorted(
        papers,
        key=lambda p: (_relevance(p, patterns), _fulltext_score(p), p.published or ""),
        reverse=True,
    )


def _rank_important(papers, patterns):
    """重要枠: 関連度 → 被引用数 → 本文の取りやすさ → 新しさ。"""
    return sorted(
        papers,
        key=lambda p: (
            _relevance(p, patterns),
            _citations(p),
            _fulltext_score(p),
            p.published or "",
        ),
        reverse=True,
    )


def _take_ranked(ranked, patterns, limit, used):
    """関連ありを優先し、不足時のみ関連度0も補充する。"""
    if limit <= 0:
        return []
    picked = []
    for require_relevant in (True, False):
        for p in ranked:
            key = p.key()
            if key in used:
                continue
            if require_relevant and _relevance(p, patterns) <= 0:
                continue
            if not require_relevant and _relevance(p, patterns) > 0:
                continue
            picked.append(p)
            used.add(key)
            if len(picked) >= limit:
                return picked
    return picked


def _has_abstract(paper):
    return bool((paper.abstract or "").strip())

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TPL = os.path.join(ROOT, "templates")
DATA = os.path.join(ROOT, "data")
SEEN = os.path.join(DATA, "seen.json")
RUNS = os.path.join(DATA, "runs")

# 安全上限（暴走・肥大化の防止）
MAX_K = 20                  # 1購読あたり1日に生成する最大ページ数
FETCH_CAP = 40              # 各ソース・各採用モードから取得する最大件数
MAX_PAGES_PER_RUN = 100     # 1回の実行で生成する総ページ数の上限
MIN_RELEVANCE = 1           # 自動採用に必要な最低キーワード適合度
MIN_TLDR_CHARS = 40         # 短すぎる要約を落とす
MIN_SUMMARY_CHARS = 260
MAX_UNKNOWN_PHRASES = 1


def _basis_is_fulltext(basis):
    return str(basis or "").startswith("fulltext")


def _source_quality(basis):
    return "fulltext" if _basis_is_fulltext(basis) else "abstract"


def _selection_label(kind):
    return {
        "important": "重要論文",
        "recent": "新着論文",
        "fallback": "補充候補",
        "manual": "手動追加",
    }.get(kind or "", "")


def _summary_text(summary):
    parts = []
    for key in ("tldr", "what", "contribution", "method", "validation", "discussion"):
        val = (summary.get(key) or "").strip()
        if val:
            parts.append(val)
    for sec in summary.get("sections") or []:
        val = (sec.get("summary") or "").strip()
        if val:
            parts.append(val)
    return "\n".join(parts)


def _pre_quality_issues(relevance, matched_keywords, basis, strict_source=True):
    issues = []
    if relevance < MIN_RELEVANCE or not matched_keywords:
        issues.append("関連キーワードが弱い")
    if strict_source and not _basis_is_fulltext(basis):
        issues.append("本文未取得（アブストラクトのみ）")
    return issues


def _post_quality_issues(summary, strict_summary=True):
    if not strict_summary:
        return []
    issues = []
    text = _summary_text(summary)
    tldr = (summary.get("tldr") or "").strip()
    if len(tldr) < MIN_TLDR_CHARS or len(text) < MIN_SUMMARY_CHARS:
        issues.append("要約が短すぎる")
    if text.count("提供された情報からは不明") > MAX_UNKNOWN_PHRASES:
        issues.append("不明項目が多い")
    return issues


def _report_paper(paper, pid, selection_kind, relevance, basis, extra=None):
    item = {
        "id": pid,
        "title": paper.title,
        "selection": selection_kind,
        "selection_label": _selection_label(selection_kind),
        "published": paper.published,
        "venue": render._venue_label(paper.venue, missing=""),
        "source": paper.source,
        "basis": basis,
        "source_quality": _source_quality(basis),
        "citations": _citations(paper),
        "relevance": relevance,
        "matched_keywords": getattr(paper, "matched_keywords", []) or [],
    }
    if extra:
        item.update(extra)
    return item


def _write_run_report(report):
    os.makedirs(RUNS, exist_ok=True)
    date = report.get("date") or datetime.date.today().isoformat()
    json_path = os.path.join(RUNS, f"{date}.json")
    html_path = os.path.join(RUNS, f"{date}.html")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, sort_keys=True)

    rows = []
    for field in report.get("fields", []):
        rows.append(f"<h2>{html.escape(field.get('label', field.get('slug', '')))}</h2>")
        rows.append(
            "<p>"
            f"候補 {field.get('candidates_total', 0)} / "
            f"新規 {field.get('fresh_total', 0)} / "
            f"追加 {len(field.get('added', []))} / "
            f"スキップ {len(field.get('skipped', []))}"
            "</p>"
        )
        if field.get("added"):
            rows.append("<h3>追加</h3><ul>")
            for item in field["added"]:
                rows.append(
                    "<li>"
                    f"{html.escape(item.get('selection_label', ''))} / "
                    f"採択先 {html.escape(render._venue_label(item.get('venue')))} / "
                    f"読む価値 {item.get('reading_value', '-')} / "
                    f"関連度 {item.get('relevance', 0)} / 被引用 {item.get('citations', 0)}: "
                    f"{html.escape(item.get('title', ''))}"
                    "</li>"
                )
            rows.append("</ul>")
        if field.get("skipped"):
            rows.append("<h3>スキップ</h3><ul>")
            for item in field["skipped"]:
                reasons = "、".join(item.get("reasons", []))
                rows.append(
                    "<li>"
                    f"{html.escape(item.get('title', ''))} "
                    f"（{html.escape(reasons)}）"
                    "</li>"
                )
            rows.append("</ul>")
    body = "\n".join(rows)
    page = f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>日次実行レポート {html.escape(date)} | Paper Survey</title>
<style>
body {{ margin:0; background:#121212; color:#e8e8e8; font-family:-apple-system,"Hiragino Kaku Gothic ProN","Noto Sans JP",Meiryo,sans-serif; line-height:1.7; }}
.wrap {{ max-width:820px; margin:0 auto; padding:32px 20px 64px; }}
a {{ color:#7cc6ff; text-decoration:none; }}
h1 {{ font-size:1.5rem; }}
h2 {{ font-size:1.1rem; color:#7ec699; border-bottom:1px solid #2a2a2a; padding-bottom:6px; margin-top:28px; }}
h3 {{ font-size:0.98rem; color:#cda; margin-bottom:4px; }}
li {{ margin:6px 0; }}
.meta {{ color:#9a9a9a; font-size:13px; }}
</style>
</head>
<body><div class="wrap">
<nav><a href="../../index.html">← Paper Survey トップ</a></nav>
<h1>日次実行レポート {html.escape(date)}</h1>
<p class="meta">生成: {html.escape(report.get('generated_at', ''))} ・ エンジン: {html.escape(report.get('engine', ''))}</p>
{body}
</div></body></html>
"""
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(page)
    return json_path, html_path


def load_subscriptions():
    with open(os.path.join(ROOT, "subscriptions.yml"), encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("subscriptions", [])


def load_sample():
    with open(os.path.join(DATA, "sample_papers.json"), encoding="utf-8") as f:
        return [Paper(**p) for p in json.load(f)]


def _search_groups(sub):
    queries = sub.get("search_queries")
    if not queries:
        return [sub.get("keywords", [])]
    groups = []
    for q in queries:
        if isinstance(q, str):
            terms = [q]
        else:
            terms = [str(x) for x in (q or []) if str(x).strip()]
        if terms:
            groups.append(terms)
    return groups or [sub.get("keywords", [])]


def _query_label(terms):
    return " + ".join(str(t) for t in terms)


def gather(sub, offline, mode="recent"):
    if offline:
        return load_sample(), {"sample/" + mode: len(load_sample())}
    papers = []
    counts = {}
    groups = _search_groups(sub)
    per_query_limit = max(8, min(FETCH_CAP, (FETCH_CAP + len(groups) - 1) // max(1, len(groups))))
    for src in sub.get("sources") or ["arxiv"]:
        src_total = 0
        for i, terms in enumerate(groups, start=1):
            label = f"{src}/{mode}/q{i}"
            try:
                got = sources.search_source(src, terms, per_query_limit, mode=mode)
                counts[label] = {"query": _query_label(terms), "count": len(got)}
            except Exception as e:
                got = []
                counts[label] = {"query": _query_label(terms), "error": repr(e)}
                print(f"  [warn] {label} 取得失敗: {e!r}")
            src_total += len(got)
            papers += got
        print(f"  {src}/{mode}: {src_total} 件 ({len(groups)} queries)")
    return papers, counts


def main(argv=None):
    ap = argparse.ArgumentParser(description="survey-app daily pipeline")
    ap.add_argument("--offline", action="store_true", help="ネット未使用・サンプル＋スタブ要約")
    ap.add_argument("--stub", action="store_true", help="論文は取得するが要約はスタブ")
    ap.add_argument("--dry-run", action="store_true", help="生成・seen更新を行わない")
    ap.add_argument("--reset", action="store_true", help="既存ページとseenを消してから再生成（本文版へ作り直し）")
    ap.add_argument("--render-indexes-only", action="store_true", help="取得・要約をせず既存seenから一覧HTMLだけ再生成")
    ap.add_argument("--limit", type=int, default=MAX_PAGES_PER_RUN, help="今回の総生成ページ上限")
    args = ap.parse_args(argv)

    subs = load_subscriptions()
    if not subs:
        print("subscriptions.yml に購読がありません。")
        return 1

    seen = load_seen(SEEN)
    if args.render_indexes_only:
        for sub in subs:
            user = (sub.get("username") or "").strip()
            if not user:
                continue
            uslug = slugify(user, fallback="user")
            display = sub.get("label") or user
            render.render_user_index(
                TPL, ROOT, uslug, display, seen.get(uslug, {}), sub.get("keywords", [])
            )
        render.render_global_index(TPL, ROOT, subs, seen, slugify)
        print("完了: 一覧HTMLを再生成")
        return 0

    summarizer = Summarizer(stub=args.offline or args.stub)
    print(f"要約エンジン: {summarizer.engine}")

    if args.reset:
        if not args.dry_run:
            for sub in subs:
                d = os.path.join(ROOT, slugify(sub.get("username", ""), fallback="user"))
                if os.path.isdir(d):
                    shutil.rmtree(d)
        seen = {}
        print("reset: seen を初期化" + ("" if args.dry_run else " ＋ 既存ページ削除"))
    today = datetime.date.today().isoformat()
    now = datetime.datetime.now().isoformat(timespec="seconds")
    report = {
        "date": today,
        "generated_at": now,
        "dry_run": args.dry_run,
        "engine": summarizer.engine,
        "fields": [],
    }
    produced = 0

    for sub in subs:
        user = (sub.get("username") or "").strip()
        if not user:
            print("[warn] username 無しの購読をスキップ")
            continue
        uslug = slugify(user, fallback="user")
        display = sub.get("label") or user
        # manual フィールド（手動追加 add_paper 用）は自動取得しない。indexだけ更新。
        if sub.get("manual"):
            print(f"\n=== {display} (slug={uslug}) [manual] ===")
            seen.setdefault(uslug, {})
            if not args.dry_run:
                render.render_user_index(TPL, ROOT, uslug, display, seen[uslug], sub.get("keywords", []))
            continue
        k = max(1, min(int(sub.get("k", 5)), MAX_K))
        print(f"\n=== {display} (slug={uslug}, k={k}) ===")

        recent_raw, recent_counts = gather(sub, args.offline, mode="recent")
        important_raw, important_counts = gather(sub, args.offline, mode="important")
        recent_papers = dedup(recent_raw)
        important_papers = dedup(important_raw)
        papers = dedup(important_papers + recent_papers)
        useen = seen.setdefault(uslug, {})
        fresh_recent = [p for p in recent_papers if p.key() not in useen]
        fresh_important = [p for p in important_papers if p.key() not in useen]
        fresh_all = [p for p in papers if p.key() not in useen]
        keywords = sub.get("keywords", [])
        kw_pats = _keyword_patterns(keywords)
        important_quota = _important_quota(k)
        recent_quota = k - important_quota
        used = set()
        picked = []
        # 重要枠: 分野内での被引用数が高い論文を優先。新着枠: 投稿日が新しい論文を優先。
        selection_kind = {}
        important_pick = _take_ranked(
            _rank_important(fresh_important, kw_pats), kw_pats, important_quota, used
        )
        for p in important_pick:
            selection_kind[p.key()] = "important"
        picked += important_pick
        recent_pick = _take_ranked(
            _rank_recent(fresh_recent, kw_pats), kw_pats, recent_quota, used
        )
        for p in recent_pick:
            selection_kind[p.key()] = "recent"
        picked += recent_pick
        # 片方の枠が不足した場合は、全候補から重要度順に補充して k 本に近づける。
        if len(picked) < k:
            fill_pick = _take_ranked(
                _rank_important(fresh_all, kw_pats), kw_pats, k - len(picked), used
            )
            for p in fill_pick:
                selection_kind[p.key()] = "fallback"
            picked += fill_pick
        fallback = _take_ranked(
            _rank_important(fresh_all, kw_pats), kw_pats, len(fresh_all), used
        )
        for p in fallback:
            selection_kind.setdefault(p.key(), "fallback")
        candidate_queue = picked + fallback
        relevant = [p for p in fresh_all if _relevance(p, kw_pats) > 0]
        field_report = {
            "slug": uslug,
            "label": display,
            "k": k,
            "source_counts": {**recent_counts, **important_counts},
            "candidates_total": len(papers),
            "fresh_total": len(fresh_all),
            "relevant_total": len(relevant),
            "picked_initial": len(picked),
            "quota": {"important": important_quota, "recent": recent_quota},
            "added": [],
            "skipped": [],
        }
        print(
            f"  候補 {len(papers)} / 新規 {len(fresh_all)} / 関連 {len(relevant)} / "
            f"採用 {len(picked)} (重要枠 {important_quota}, 新着枠 {recent_quota})"
        )

        produced_for_sub = 0
        for p in candidate_queue:
            if produced >= args.limit:
                print("  [stop] 総ページ上限に到達")
                break
            if produced_for_sub >= k:
                break
            pid = slugify(p.paper_id(), fallback="paper")
            rel = f"{uslug}/{pid}.html"
            relevance_score = _relevance(p, kw_pats)
            matched_keywords = _matched_keywords(p, keywords)
            p.matched_keywords = matched_keywords
            kind = selection_kind.get(p.key(), "fallback")
            if not args.offline:
                p = _enrich_fulltext_source(p)
            # 本文をセクション分割して多段要約（取れなければ abstract にフォールバック）
            if args.offline:
                fsections, basis = [], "abstract"
            else:
                fsections, basis = fetch_sections(p)
            print(
                f"    {pid}: {_selection_label(kind)} / 関連度{relevance_score} / "
                f"被引用{_citations(p)} / {len(fsections)}セクション / 根拠 {basis}"
            )
            if not fsections and not _has_abstract(p):
                reasons = ["本文もアブストラクトも取得できない"]
                field_report["skipped"].append(
                    _report_paper(p, pid, kind, relevance_score, basis, {"reasons": reasons})
                )
                print(f"      [skip] {'、'.join(reasons)}")
                continue
            pre_issues = _pre_quality_issues(
                relevance_score,
                matched_keywords,
                basis,
                strict_source=not args.offline,
            )
            if pre_issues:
                field_report["skipped"].append(
                    _report_paper(p, pid, kind, relevance_score, basis, {"reasons": pre_issues})
                )
                print(f"      [skip] {'、'.join(pre_issues)}")
                continue
            try:
                summary = summarizer.summarize(p, sections=fsections, basis=basis)
            except Exception as e:
                reasons = [f"LLM要約失敗: {e!r}"]
                field_report["skipped"].append(
                    _report_paper(p, pid, kind, relevance_score, basis, {"reasons": reasons})
                )
                print(f"      [skip] {reasons[0]}")
                continue
            post_issues = _post_quality_issues(summary, strict_summary=not (args.offline or args.stub))
            if post_issues:
                field_report["skipped"].append(
                    _report_paper(p, pid, kind, relevance_score, basis, {"reasons": post_issues})
                )
                print(f"      [skip] {'、'.join(post_issues)}")
                continue
            summary.update(summarizer.rate_reading_value(p, summary, basis))
            p.selection_type = kind
            p.selection_label = _selection_label(kind)
            p.relevance_score = relevance_score
            p.source_quality = _source_quality(basis)
            p.reading_value = summary.get("_reading_value", "")
            p.reading_value_reason = summary.get("_reading_value_reason", "")
            if not args.dry_run:
                os.makedirs(os.path.join(ROOT, uslug), exist_ok=True)
                with open(os.path.join(ROOT, rel), "w", encoding="utf-8") as f:
                    f.write(render.render_paper_page(TPL, p, summary))
            added_at = datetime.datetime.now().isoformat(timespec="microseconds")
            useen[p.key()] = {
                "title": p.title,
                "file": rel,
                "date": p.published,
                "venue": render._venue_label(p.venue, missing=""),
                "url": p.url,
                "pdf_url": p.pdf_url,
                "arxiv_id": p.arxiv_id,
                "doi": p.doi,
                "added": today,
                "added_at": added_at,
                "authors": p.authors,
                "tldr": summary.get("tldr", ""),
                "engine": summary.get("_engine", ""),
                "basis": summary.get("_basis", ""),
                "matched_keywords": matched_keywords,
                "selection": kind,
                "selection_label": _selection_label(kind),
                "citations": _citations(p),
                "relevance": relevance_score,
                "source_quality": _source_quality(summary.get("_basis", basis)),
                "reading_value": summary.get("_reading_value", ""),
                "reading_value_reason": summary.get("_reading_value_reason", ""),
            }
            field_report["added"].append(
                _report_paper(
                    p,
                    pid,
                    kind,
                    relevance_score,
                    summary.get("_basis", basis),
                    {
                        "file": rel,
                        "reading_value": summary.get("_reading_value", ""),
                        "reading_value_reason": summary.get("_reading_value_reason", ""),
                    },
                )
            )
            produced += 1
            produced_for_sub += 1
            print(f"  + {rel}")
        if produced < args.limit and produced_for_sub < min(k, len(fresh_all)):
            print(f"  [warn] 生成可能な候補が不足: {produced_for_sub}/{k} ページ")

        if not args.dry_run:
            render.render_user_index(TPL, ROOT, uslug, display, useen, keywords)
        report["fields"].append(field_report)

    if not args.dry_run:
        report_paths = _write_run_report(report)
        print(f"実行レポート: {os.path.relpath(report_paths[0], ROOT)} / {os.path.relpath(report_paths[1], ROOT)}")
        render.render_global_index(TPL, ROOT, subs, seen, slugify)
        save_seen(SEEN, seen)

    print(f"\n完了: {produced} ページ生成 (dry-run={args.dry_run})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

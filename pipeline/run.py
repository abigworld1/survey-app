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
import json
import os
import re
import shutil
import sys

import yaml

from . import render, sources
from .dedup import dedup, load_seen, save_seen
from .fulltext import fetch_sections
from .schema import Paper
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

# 安全上限（暴走・肥大化の防止）
MAX_K = 20                  # 1購読あたり1日に生成する最大ページ数
FETCH_CAP = 40              # 各ソース・各採用モードから取得する最大件数
MAX_PAGES_PER_RUN = 100     # 1回の実行で生成する総ページ数の上限


def load_subscriptions():
    with open(os.path.join(ROOT, "subscriptions.yml"), encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("subscriptions", [])


def load_sample():
    with open(os.path.join(DATA, "sample_papers.json"), encoding="utf-8") as f:
        return [Paper(**p) for p in json.load(f)]


def gather(sub, offline, mode="recent"):
    if offline:
        return load_sample()
    papers = []
    for src in sub.get("sources") or ["arxiv"]:
        got = sources.search_source(src, sub["keywords"], FETCH_CAP, mode=mode)
        print(f"  {src}/{mode}: {len(got)} 件")
        papers += got
    return papers


def main(argv=None):
    ap = argparse.ArgumentParser(description="survey-app daily pipeline")
    ap.add_argument("--offline", action="store_true", help="ネット未使用・サンプル＋スタブ要約")
    ap.add_argument("--stub", action="store_true", help="論文は取得するが要約はスタブ")
    ap.add_argument("--dry-run", action="store_true", help="生成・seen更新を行わない")
    ap.add_argument("--reset", action="store_true", help="既存ページとseenを消してから再生成（本文版へ作り直し）")
    ap.add_argument("--limit", type=int, default=MAX_PAGES_PER_RUN, help="今回の総生成ページ上限")
    args = ap.parse_args(argv)

    subs = load_subscriptions()
    if not subs:
        print("subscriptions.yml に購読がありません。")
        return 1

    summarizer = Summarizer(stub=args.offline or args.stub)
    print(f"要約エンジン: {summarizer.engine}")

    seen = load_seen(SEEN)
    if args.reset:
        if not args.dry_run:
            for sub in subs:
                d = os.path.join(ROOT, slugify(sub.get("username", ""), fallback="user"))
                if os.path.isdir(d):
                    shutil.rmtree(d)
        seen = {}
        print("reset: seen を初期化" + ("" if args.dry_run else " ＋ 既存ページ削除"))
    today = datetime.date.today().isoformat()
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
                render.render_user_index(TPL, ROOT, uslug, display, seen[uslug])
            continue
        k = max(1, min(int(sub.get("k", 5)), MAX_K))
        print(f"\n=== {display} (slug={uslug}, k={k}) ===")

        recent_papers = dedup(gather(sub, args.offline, mode="recent"))
        important_papers = dedup(gather(sub, args.offline, mode="important"))
        papers = dedup(important_papers + recent_papers)
        useen = seen.setdefault(uslug, {})
        fresh_recent = [p for p in recent_papers if p.key() not in useen]
        fresh_important = [p for p in important_papers if p.key() not in useen]
        fresh_all = [p for p in papers if p.key() not in useen]
        kw_pats = _keyword_patterns(sub.get("keywords", []))
        important_quota = _important_quota(k)
        recent_quota = k - important_quota
        used = set()
        picked = []
        # 重要枠: 分野内での被引用数が高い論文を優先。新着枠: 投稿日が新しい論文を優先。
        picked += _take_ranked(
            _rank_important(fresh_important, kw_pats), kw_pats, important_quota, used
        )
        picked += _take_ranked(
            _rank_recent(fresh_recent, kw_pats), kw_pats, recent_quota, used
        )
        # 片方の枠が不足した場合は、全候補から重要度順に補充して k 本に近づける。
        if len(picked) < k:
            picked += _take_ranked(
                _rank_important(fresh_all, kw_pats), kw_pats, k - len(picked), used
            )
        fallback = _take_ranked(
            _rank_important(fresh_all, kw_pats), kw_pats, len(fresh_all), used
        )
        candidate_queue = picked + fallback
        relevant = [p for p in fresh_all if _relevance(p, kw_pats) > 0]
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
            # 本文をセクション分割して多段要約（取れなければ abstract にフォールバック）
            if args.offline:
                fsections, basis = [], "abstract"
            else:
                fsections, basis = fetch_sections(p)
            print(
                f"    {pid}: 関連度{_relevance(p, kw_pats)} / 被引用{_citations(p)} / "
                f"{len(fsections)}セクション / 根拠 {basis}"
            )
            if not fsections and not _has_abstract(p):
                print("      [skip] 本文もアブストラクトも取得できないため生成しません")
                continue
            summary = summarizer.summarize(p, sections=fsections, basis=basis)
            if not args.dry_run:
                os.makedirs(os.path.join(ROOT, uslug), exist_ok=True)
                with open(os.path.join(ROOT, rel), "w", encoding="utf-8") as f:
                    f.write(render.render_paper_page(TPL, p, summary))
            useen[p.key()] = {
                "title": p.title,
                "file": rel,
                "date": p.published,
                "added": today,
                "tldr": summary.get("tldr", ""),
                "engine": summary.get("_engine", ""),
                "basis": summary.get("_basis", ""),
            }
            produced += 1
            produced_for_sub += 1
            print(f"  + {rel}")
        if produced < args.limit and produced_for_sub < min(k, len(fresh_all)):
            print(f"  [warn] 生成可能な候補が不足: {produced_for_sub}/{k} ページ")

        if not args.dry_run:
            render.render_user_index(TPL, ROOT, uslug, display, useen)

    if not args.dry_run:
        render.render_global_index(TPL, ROOT, subs, seen, slugify)
        save_seen(SEEN, seen)

    print(f"\n完了: {produced} ページ生成 (dry-run={args.dry_run})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

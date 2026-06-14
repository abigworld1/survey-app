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
import shutil
import sys

import yaml

from . import render, sources
from .dedup import dedup, load_seen, save_seen
from .fulltext import fetch_fulltext
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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TPL = os.path.join(ROOT, "templates")
DATA = os.path.join(ROOT, "data")
SEEN = os.path.join(DATA, "seen.json")

# 安全上限（暴走・肥大化の防止）
MAX_K = 20                  # 1購読あたり1日に生成する最大ページ数
FETCH_CAP = 40              # 各ソースから取得する最大件数
MAX_PAGES_PER_RUN = 100     # 1回の実行で生成する総ページ数の上限


def load_subscriptions():
    with open(os.path.join(ROOT, "subscriptions.yml"), encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("subscriptions", [])


def load_sample():
    with open(os.path.join(DATA, "sample_papers.json"), encoding="utf-8") as f:
        return [Paper(**p) for p in json.load(f)]


def gather(sub, offline):
    if offline:
        return load_sample()
    papers = []
    for src in sub.get("sources") or ["arxiv"]:
        got = sources.search_source(src, sub["keywords"], FETCH_CAP)
        print(f"  {src}: {len(got)} 件")
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
        k = max(1, min(int(sub.get("k", 5)), MAX_K))
        print(f"\n=== {user} (slug={uslug}, k={k}) ===")

        papers = dedup(gather(sub, args.offline))
        useen = seen.setdefault(uslug, {})
        fresh = [p for p in papers if p.key() not in useen]
        # 本文が取れる論文を優先、同点は新しい順
        fresh.sort(key=lambda p: (_fulltext_score(p), p.published or ""), reverse=True)
        picked = fresh[:k]
        print(f"  候補 {len(papers)} / 新規 {len(fresh)} / 採用 {len(picked)}")

        for p in picked:
            if produced >= args.limit:
                print("  [stop] 総ページ上限に到達")
                break
            pid = slugify(p.paper_id(), fallback="paper")
            rel = f"{uslug}/{pid}.html"
            # 本文(arXiv HTML / OA PDF)を取得して要約に使う（取れなければ abstract にフォールバック）
            if args.offline:
                fulltext, basis = "", "abstract"
            else:
                fulltext, basis = fetch_fulltext(p)
            summary = summarizer.summarize(p, fulltext=fulltext, basis=basis)
            print(f"    {pid}: 本文 {len(fulltext)}字 / 根拠 {summary.get('_basis')}")
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
            print(f"  + {rel}")

        if not args.dry_run:
            render.render_user_index(TPL, ROOT, uslug, user, useen)

    if not args.dry_run:
        render.render_global_index(TPL, ROOT, subs, seen, slugify)
        save_seen(SEEN, seen)

    print(f"\n完了: {produced} ページ生成 (dry-run={args.dry_run})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

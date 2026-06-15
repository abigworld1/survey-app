#!/usr/bin/env python3
"""手動追加した論文ページを削除する（HTMLファイル＋seen＋index再生成）。LLM 不要。

例:
  python -m pipeline.remove_paper --arxiv 1901.11282            # 既定 reading から削除
  python -m pipeline.remove_paper --mapf --arxiv 1901.11282     # MAPF/MAPD/倉庫 分野から
  python -m pipeline.remove_paper --field reading --slug foo    # ファイル名(拡張子なし)で
  python -m pipeline.remove_paper --field reading --list        # その分野の一覧を表示

削除後: git add -A && git commit -m 'remove paper' && git push origin main
"""
import argparse
import os
import re
import sys

import yaml

from . import render
from .dedup import load_seen, save_seen
from .util import slugify

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TPL = os.path.join(ROOT, "templates")
SEEN = os.path.join(ROOT, "data", "seen.json")
DEFAULT_FIELD = "reading"


def _load_subs():
    with open(os.path.join(ROOT, "subscriptions.yml"), encoding="utf-8") as f:
        return (yaml.safe_load(f) or {}).get("subscriptions", [])


def _norm_arxiv(raw):
    aid = raw.rsplit("/abs/", 1)[-1].replace("arxiv:", "").replace("arXiv:", "").strip()
    return re.sub(r"v\d+$", "", aid)


def main(argv=None):
    ap = argparse.ArgumentParser(description="手動追加した論文ページを削除")
    dest = ap.add_mutually_exclusive_group()
    dest.add_argument("--mapf", dest="field", action="store_const", const="mapf-mapd-warehouse")
    dest.add_argument("--rag", dest="field", action="store_const", const="doc-structure-rag")
    dest.add_argument("--field", default=None, help="分野スラッグ（既定 reading）")
    ap.add_argument("--arxiv", help="arXiv ID または URL で指定")
    ap.add_argument("--slug", help="ファイル名（拡張子なし）で指定")
    ap.add_argument("--title", help="タイトルの部分一致で指定")
    ap.add_argument("--list", action="store_true", help="その分野の登録一覧を表示して終了")
    args = ap.parse_args(argv)

    field = args.field or DEFAULT_FIELD
    uslug = slugify(field, fallback="reading")
    seen = load_seen(SEEN)
    useen = seen.get(uslug, {})

    # 一覧表示（指定が無いときも一覧を出す）
    if args.list or not (args.arxiv or args.slug or args.title):
        print(f"=== {uslug} の登録 ({len(useen)}件) ===")
        for key, info in useen.items():
            print(f"  {key}  file={os.path.basename(info.get('file', ''))}  {info.get('title', '')[:60]}")
        if not args.list:
            print("削除するには --arxiv / --slug / --title のいずれかを指定してください。")
        return 0

    arxiv_key = ("arxiv:" + _norm_arxiv(args.arxiv).lower()) if args.arxiv else None
    targets = []
    for key, info in useen.items():
        base = os.path.basename(info.get("file", "")).removesuffix(".html")
        if (arxiv_key and key == arxiv_key) or \
           (args.slug and base == args.slug) or \
           (args.title and args.title.lower() in info.get("title", "").lower()):
            targets.append(key)

    if not targets:
        print(f"[該当なし] {uslug} に一致する論文がありません。--list で確認してください。")
        return 1

    for key in targets:
        info = useen.pop(key)
        path = os.path.join(ROOT, info.get("file", ""))
        if info.get("file") and os.path.exists(path):
            os.remove(path)
        print(f"削除: {info.get('file')}  ({info.get('title', '')[:60]})")

    subs = _load_subs()
    sub = next((s for s in subs if slugify(s.get("username", "")) == uslug), {})
    label = sub.get("label") or field
    render.render_user_index(TPL, ROOT, uslug, label, useen, sub.get("keywords", []))
    render.render_global_index(TPL, ROOT, subs, seen, slugify)
    save_seen(SEEN, seen)
    print("公開: git add -A && git commit -m 'remove paper' && git push origin main")
    return 0


if __name__ == "__main__":
    sys.exit(main())

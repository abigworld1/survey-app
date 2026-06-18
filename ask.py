#!/usr/bin/env python3
"""Short command for adding a follow-up Q&A to a generated paper page.

Usage:
  ./ask.py --paper "Paper title" --question "Question"

"""
import argparse
import difflib
import os
import subprocess
import sys

from pipeline.ask_paper import main as ask_paper_main
from pipeline.dedup import load_seen
from pipeline.schema import normalize_title

ROOT = os.path.dirname(os.path.abspath(__file__))
SEEN = os.path.join(ROOT, "data", "seen.json")


def _run(cmd, *, check=True):
    print("+", " ".join(cmd))
    return subprocess.run(cmd, cwd=ROOT, check=check)


def _display_title(item):
    return item["info"].get("title", "")


def _score(query, title):
    q = query.casefold().strip()
    t = (title or "").casefold().strip()
    qn = normalize_title(query)
    tn = normalize_title(title)
    if q and q == t:
        return 1.2
    if q and q in t:
        return 1.1
    if qn and qn == tn:
        return 1.0
    if qn and tn and qn in tn:
        return 0.95
    raw_score = difflib.SequenceMatcher(None, q, t).ratio() if q and t else 0.0
    norm_score = difflib.SequenceMatcher(None, qn, tn).ratio() if qn and tn else 0.0
    return max(raw_score, norm_score)


def _all_entries(field=None):
    seen = load_seen(SEEN)
    rows = []
    for uslug, useen in seen.items():
        if field and uslug != field:
            continue
        for key, info in useen.items():
            if info.get("file") and info.get("title"):
                rows.append({"field": uslug, "key": key, "info": info})
    return rows


def _resolve_paper(query, field=None):
    rows = _all_entries(field=field)
    scored = sorted(
        ((row, _score(query, _display_title(row))) for row in rows),
        key=lambda x: x[1],
        reverse=True,
    )
    if not scored or scored[0][1] < 0.45:
        print(f"[該当なし] title={query!r}")
        _print_candidates(scored[:8])
        raise SystemExit(1)

    best, best_score = scored[0]
    near = [x for x in scored[1:5] if best_score - x[1] < 0.02 and x[1] >= 0.8]
    if near and best_score < 1.0:
        print("[候補が曖昧です] もう少し長いタイトルで指定してください。")
        _print_candidates([scored[0]] + near)
        raise SystemExit(1)
    return best


def _print_candidates(scored):
    if not scored:
        return
    print("候補:")
    for row, score in scored:
        print(f"  {score:.2f}  [{row['field']}] {_display_title(row)}")


def _commit_message(title):
    compact = " ".join((title or "paper").split())
    if len(compact) > 64:
        compact = compact[:61].rstrip() + "..."
    return f"add followup qa for {compact}"


def _git_has_staged_changes():
    return subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=ROOT,
        check=False,
    ).returncode != 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Ask Gemma a follow-up question and push the updated paper HTML")
    ap.add_argument("--paper", required=True, help="論文タイトル")
    ap.add_argument("--question", action="append", required=True, help="質問。複数指定可")
    ap.add_argument("--field", help="分野スラッグで絞り込み")
    ap.add_argument("--mapf", dest="field", action="store_const", const="mapf-mapd-warehouse", help="MAPF/MAPD分野に絞り込み")
    ap.add_argument("--rag", dest="field", action="store_const", const="doc-structure-rag", help="RAG分野に絞り込み")
    ap.add_argument("--reading", dest="field", action="store_const", const="reading", help="reading分野に絞り込み")
    ap.add_argument("--message", help="commit message")
    ap.add_argument("--arxiv-id", help="本文取得に使うarXiv IDを明示指定する")
    ap.add_argument("--pdf-url", help="本文取得に使うPDF URLを明示指定する")
    ap.add_argument("--context-chars", type=int, default=60000, help="Gemmaに渡す元論文本文の最大文字数")
    ap.add_argument("--replace-followups", action="store_true", help="既存の追加質問を消してから追記する")
    ap.add_argument(
        "--allow-html-fallback",
        action="store_true",
        help="arXiv/PDF本文を取得できない場合のみ生成済みHTMLで回答する",
    )
    ap.add_argument("--dry-run", action="store_true", help="回答だけ表示し、HTML/Gitは変更しない")
    ap.add_argument("--stub", action="store_true", help="LLMを呼ばずスタブ回答で動作確認")
    ap.add_argument("--no-push", action="store_true", help="commitまで行い、pushしない")
    args = ap.parse_args(argv)

    os.environ.setdefault("LLM_BASE_URL", "http://localhost:8000/v1")
    os.environ.setdefault("LLM_API_KEY", "dummy")

    if not args.dry_run:
        _run(["git", "pull", "--rebase", "origin", "main"])

    row = _resolve_paper(args.paper, field=args.field)
    rel = row["info"]["file"]
    title = _display_title(row)
    print(f"対象: [{row['field']}] {title}")
    print(f"HTML: {rel}")

    ask_args = ["--file", rel]
    for question in args.question:
        ask_args += ["--question", question]
    if args.arxiv_id:
        ask_args += ["--arxiv-id", args.arxiv_id]
    if args.pdf_url:
        ask_args += ["--pdf-url", args.pdf_url]
    ask_args += ["--context-chars", str(args.context_chars)]
    if args.replace_followups:
        ask_args.append("--replace-followups")
    if args.allow_html_fallback:
        ask_args.append("--allow-html-fallback")
    if args.dry_run:
        ask_args.append("--dry-run")
    if args.stub:
        ask_args.append("--stub")
    rc = ask_paper_main(ask_args)
    if rc:
        return rc

    if args.dry_run:
        return 0

    _run(["git", "add", rel])
    if not _git_has_staged_changes():
        print("変更がないため commit/push は行いません。")
        return 0
    _run(["git", "commit", "-m", args.message or _commit_message(title)])
    if not args.no_push:
        _run(["git", "push", "origin", "main"])
    return 0


if __name__ == "__main__":
    sys.exit(main())

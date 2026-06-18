#!/usr/bin/env python3
"""生成済み論文HTMLに、Gemmaへの追加質問と回答を対話形式で追記する。

例:
  LLM_BASE_URL=http://localhost:8000/v1 LLM_API_KEY=dummy \
    python -m pipeline.ask_paper --mapf --slug 2606.04746 \
    --question "実機実験の設定はどこまで一般化できる？"

追記後: git add -A && git commit -m 'add paper followup qa' && git push origin main
"""
import argparse
import datetime
import html
import os
import re
import sys

from . import render
from .dedup import load_seen
from .summarize import Summarizer
from .util import slugify

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEEN = os.path.join(ROOT, "data", "seen.json")
DEFAULT_FIELD = "reading"

FOLLOWUP_START = "<!-- followup-qa:start -->"
FOLLOWUP_END = "<!-- followup-qa:end -->"
FOLLOWUP_CSS = """
  .followups { margin:34px 0 8px; }
  .followups h2 { font-size:1.1rem; color:#cda; border-bottom:1px solid #2a2a2a;
                  padding-bottom:6px; margin:0 0 14px; }
  .dialogue { margin:0 0 18px; }
  .turn { display:grid; grid-template-columns:32px 1fr; gap:9px; margin:9px 0; }
  .speaker { width:28px; height:28px; border-radius:50%; display:flex; align-items:center;
             justify-content:center; font-size:12px; font-weight:700; }
  .turn.question .speaker { background:#263238; color:#b9d8e6; }
  .turn.answer .speaker { background:#1f3327; color:#a7dfb8; }
  .bubble { background:#181d20; border:1px solid #2d383d; border-radius:8px;
            padding:9px 11px; color:#d8d8d8; }
  .turn.question .bubble { color:#e6edf0; }
  .dialogue-meta { color:#888; font-size:12px; margin:5px 0 0 41px; }
"""

QA_SYSTEM = (
    "あなたは計算機科学の研究者を補助する論文読解アシスタントです。"
    "与えられた論文ページ本文だけを根拠に、ユーザーの追加質問へ日本語で答えてください。"
    "ページ本文に根拠がない場合は推測せず、何が不明かを明確に述べてください。"
    "必要なら、どのセクションの記述に基づくかを短く示してください。"
    "数式や記号は LaTeX で書き、インラインは $〜$、独立した式は $$〜$$ で囲んでください。"
    "出力は回答本文のみ。挨拶や前置きは不要です。"
)


def _safe_path(rel_or_path):
    root_abs = os.path.abspath(ROOT)
    path = rel_or_path
    if not os.path.isabs(path):
        path = os.path.join(root_abs, path)
    path = os.path.abspath(path)
    if not (path == root_abs or path.startswith(root_abs + os.sep)):
        raise SystemExit(f"unsafe path: {rel_or_path}")
    return path


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _write(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _strip_tags(value):
    value = re.sub(r"<(script|style)\b.*?</\1>", "", value or "", flags=re.I | re.S)
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
    value = re.sub(r"</(h1|h2|h3|p|div|section|li)>", "\n", value, flags=re.I)
    value = re.sub(r"<[^>]+>", "", value)
    value = html.unescape(value)
    value = re.sub(r"[ \t]{2,}", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def _page_text(text):
    body = re.search(r"<body\b[^>]*>(.*?)<footer\b", text, re.I | re.S)
    chunk = body.group(1) if body else text
    chunk = re.sub(r"<nav\b.*?</nav>", "", chunk, flags=re.I | re.S)
    return _strip_tags(chunk)


def _page_title(text):
    m = re.search(r"<h1\b[^>]*>(.*?)</h1>", text, re.I | re.S)
    return _strip_tags(m.group(1)) if m else "Untitled"


def _resolve_from_seen(field, slug):
    uslug = slugify(field or DEFAULT_FIELD, fallback=DEFAULT_FIELD)
    seen = load_seen(SEEN)
    useen = seen.get(uslug, {})
    for key, info in useen.items():
        file_slug = os.path.splitext(os.path.basename(info.get("file", "")))[0]
        key_slug = slugify(key, fallback=key)
        title_slug = slugify(info.get("title", ""), fallback="")
        if slug in {file_slug, key, key_slug, title_slug}:
            return info.get("file", "")
    raise SystemExit(f"[該当なし] {uslug} に slug={slug} の論文がありません。")


def _resolve_path(args):
    if args.file:
        return _safe_path(args.file)
    field = args.field or DEFAULT_FIELD
    if not args.slug:
        raise SystemExit("--file または --slug を指定してください。")
    return _safe_path(_resolve_from_seen(field, args.slug))


def _ensure_dialogue_css(text):
    if ".followups" in text and ".dialogue" in text:
        return text
    style_end = text.find("</style>")
    if style_end == -1:
        return text
    return text[:style_end] + FOLLOWUP_CSS + text[style_end:]


def _dialogue_html(question, answer, engine):
    generated = datetime.datetime.now().isoformat(timespec="seconds")
    return (
        '<div class="dialogue">\n'
        '  <div class="turn question">'
        '<div class="speaker">Q</div>'
        f'<div class="bubble">{render._multiline(question)}</div>'
        '</div>\n'
        '  <div class="turn answer">'
        '<div class="speaker">A</div>'
        f'<div class="bubble">{render._multiline(answer)}</div>'
        '</div>\n'
        f'  <div class="dialogue-meta">回答エンジン: {render._esc(engine)} ・ 追記日: {render._esc(generated)}</div>\n'
        '</div>\n'
    )


def _append_followup(text, question, answer, engine):
    text = _ensure_dialogue_css(text)
    block = _dialogue_html(question, answer, engine)
    if FOLLOWUP_START in text and FOLLOWUP_END in text:
        return text.replace(FOLLOWUP_END, block + FOLLOWUP_END, 1)
    section = (
        f"{FOLLOWUP_START}\n"
        '<section class="followups" id="followup-qa">\n'
        "  <h2>追加質問</h2>\n"
        f"{block}"
        "</section>\n"
        f"{FOLLOWUP_END}\n"
    )
    footer = re.search(r"\n\s*<footer\b", text, re.I)
    if not footer:
        raise SystemExit("footer が見つからないため追記位置を決められません。")
    return text[:footer.start()] + "\n" + section + text[footer.start():]


def _ask_llm(summarizer, title, page_body, question):
    if summarizer.stub:
        return "（スタブ回答）LLM未接続のため、実運用ではGemmaがこの質問に回答します。"
    return summarizer._chat(
        QA_SYSTEM,
        f"論文タイトル:\n{title}\n\n"
        f"論文ページ本文:\n{page_body[:14000]}\n\n"
        f"追加質問:\n{question}",
        max_tokens=900,
    ).strip()


def main(argv=None):
    ap = argparse.ArgumentParser(description="生成済み論文HTMLに追加質問とGemma回答を追記")
    dest = ap.add_mutually_exclusive_group()
    dest.add_argument("--mapf", dest="field", action="store_const", const="mapf-mapd-warehouse")
    dest.add_argument("--rag", dest="field", action="store_const", const="doc-structure-rag")
    dest.add_argument("--reading", dest="field", action="store_const", const="reading")
    dest.add_argument("--field", default=None, help="分野スラッグ（既定 reading）")
    ap.add_argument("--slug", help="対象論文のHTMLファイル名slug、seenキー、またはタイトルslug")
    ap.add_argument("--file", help="対象HTMLファイルのパス（--slug の代わり）")
    ap.add_argument("--question", action="append", required=True, help="追記する質問。複数指定可")
    ap.add_argument("--dry-run", action="store_true", help="LLM回答だけ表示し、HTMLは書き換えない")
    ap.add_argument("--stub", action="store_true", help="LLMを呼ばずスタブ回答で動作確認")
    args = ap.parse_args(argv)

    path = _resolve_path(args)
    text = _read(path)
    title = _page_title(text)
    body = _page_text(text)
    if not body:
        raise SystemExit("HTML本文を抽出できません。")

    summarizer = Summarizer(stub=args.stub)
    print(f"対象: {os.path.relpath(path, ROOT)}")
    print(f"タイトル: {title}")
    print(f"回答エンジン: {summarizer.engine}")

    updated = text
    for question in args.question:
        answer = _ask_llm(summarizer, title, body, question)
        print("\nQ:", question)
        print("A:", answer)
        updated = _append_followup(updated, question, answer, summarizer.engine)
        body += f"\n\n追加質問: {question}\n回答: {answer}"

    if not args.dry_run:
        _write(path, updated)
        print(f"\n追記: {os.path.relpath(path, ROOT)}")
        print("公開: git add -A && git commit -m 'add paper followup qa' && git push origin main")
    return 0


if __name__ == "__main__":
    sys.exit(main())

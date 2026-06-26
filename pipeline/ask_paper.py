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

from . import fulltext, render
from .dedup import load_seen
from .regenerate_existing import _resolve_paper
from .schema import Paper, normalize_title
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
  .bubble h3 { font-size:0.98rem; color:#b9c6d6; margin:0.8em 0 0.35em; }
  .bubble h3:first-child { margin-top:0; }
  .bubble p { margin:0 0 0.65em; }
  .bubble p:last-child { margin-bottom:0; }
  .bubble ul { margin:0.35em 0 0.65em 1.25em; padding:0; }
  .bubble li { margin:0.2em 0; }
  .bubble .table-wrap { overflow-x:auto; margin:0.45em 0 0.8em; }
  .bubble table { width:100%; border-collapse:collapse; font-size:0.92rem; }
  .bubble th, .bubble td { border:1px solid #2d383d; padding:6px 8px; vertical-align:top; }
  .bubble th { background:#20282c; color:#dce7ed; font-weight:700; }
  .bubble code { background:#11181b; border:1px solid #2b363a; border-radius:4px;
                 padding:0 0.25em; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }
  .turn.question .bubble { color:#e6edf0; }
  .dialogue-meta { color:#888; font-size:12px; margin:5px 0 0 41px; }
"""

QA_SYSTEM = (
    "あなたは計算機科学の研究者を補助する論文読解アシスタントです。"
    "与えられた元論文本文を主根拠に、ユーザーの追加質問へ日本語で答えてください。"
    "論文固有の主張・実験結果・提案手法の詳細は本文に書かれている範囲に限定してください。"
    "一方で、MAPF、Lifelong MAPF、MAPD、RAGなど、その分野で標準的な一般用語や周辺概念の"
    "定義・比較は、一般的な研究知識で補って構いません。"
    "その場合は『一般的には』や『標準的なMAPDでは』のように、本文由来ではない補足だと分かる形で書いてください。"
    "本文に根拠がない論文固有事項は推測せず、何が不明かを明確に述べてください。"
    "必要なら、どのセクションの記述に基づくかを短く示してください。"
    "数式や記号は LaTeX で書き、インラインは $〜$、独立した式は $$〜$$ で囲んでください。"
    "出力は回答本文のみ。挨拶や前置きは不要です。"
)

HTML_FALLBACK_SYSTEM = (
    "あなたは計算機科学の研究者を補助する論文読解アシスタントです。"
    "元論文本文を取得できなかったため、与えられた生成済みHTMLページ本文だけを根拠に、"
    "ユーザーの追加質問へ日本語で答えてください。"
    "ページ固有の内容はHTMLに書かれている範囲に限定してください。"
    "ただし、分野で標準的な一般用語や周辺概念の定義・比較は一般知識で補って構いません。"
    "その場合は本文由来ではない補足だと分かる形で書いてください。"
    "ページ本文に根拠がない論文固有事項は推測せず、何が不明かを明確に述べてください。"
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


def _html_metadata(text, title, rel):
    links = re.findall(r'<a href="([^"]+)"', text or "")
    links = [u for u in links if re.match(r"https?://", u, re.I)]
    pdf_url = next(
        (u for u in links if ".pdf" in u.lower() or "/pdf/" in u.lower()),
        "",
    )
    url = next((u for u in links if u != pdf_url), "")
    doi_m = re.search(r"https?://doi\.org/([^\"<]+)", text or "", re.I)
    arxiv_m = re.search(
        r"arxiv\.org/(?:abs|pdf|html)/([0-9][0-9.]+)(?:v\d+)?",
        text or "",
        re.I,
    )
    return {
        "file": rel,
        "title": title,
        "url": url,
        "pdf_url": pdf_url,
        "doi": doi_m.group(1).strip() if doi_m else "",
        "arxiv_id": arxiv_m.group(1) if arxiv_m else "",
    }


def _relpath(path):
    return os.path.relpath(path, ROOT).replace(os.sep, "/")


def _normalize_rel(path):
    return os.path.normpath(path or "").replace(os.sep, "/")


def _seen_entry_for_path(path):
    rel = _normalize_rel(_relpath(path))
    seen = load_seen(SEEN)
    for uslug, useen in seen.items():
        for key, info in useen.items():
            if _normalize_rel(info.get("file", "")) == rel:
                return uslug, key, dict(info)
    return "", "", {}


def _merge_info(base, fallback):
    out = dict(fallback or {})
    out.update({k: v for k, v in (base or {}).items() if v not in (None, "", [])})
    return out


def _looks_like_pdf_url(url):
    return bool(url and (".pdf" in url.lower() or "/pdf/" in url.lower()))


def _paper_from_info(key, info):
    title = info.get("title", "")
    arxiv_id = info.get("arxiv_id", "")
    if arxiv_id:
        pdf_url = info.get("pdf_url", "")
        if not _looks_like_pdf_url(pdf_url):
            pdf_url = f"https://arxiv.org/pdf/{arxiv_id}"
        return Paper(
            source="arxiv",
            title=title,
            abstract=info.get("abstract", ""),
            authors=info.get("authors", []) or [],
            published=info.get("date", "") or info.get("published", ""),
            venue=info.get("venue", ""),
            url=info.get("url", "") or f"https://arxiv.org/abs/{arxiv_id}",
            pdf_url=pdf_url,
            arxiv_id=arxiv_id,
            doi=info.get("doi", ""),
            citations=int(info.get("citations") or 0),
            matched_keywords=info.get("matched_keywords", []) or [],
        )
    resolved_key = key
    if not resolved_key:
        if info.get("doi"):
            resolved_key = "doi:" + info["doi"].lower()
        elif title:
            resolved_key = "title:" + normalize_title(title)
    if resolved_key:
        try:
            paper = _resolve_paper(resolved_key, info)
            if paper:
                return paper
        except Exception as e:
            print(f"      [warn] 論文メタデータ再解決に失敗: {e!r}")
    if info.get("pdf_url") or info.get("doi"):
        return Paper(
            source=info.get("source", "existing"),
            title=title,
            abstract=info.get("abstract", ""),
            authors=info.get("authors", []) or [],
            published=info.get("date", "") or info.get("published", ""),
            venue=info.get("venue", ""),
            url=info.get("url", ""),
            pdf_url=info.get("pdf_url", ""),
            arxiv_id="",
            doi=info.get("doi", ""),
            citations=int(info.get("citations") or 0),
            matched_keywords=info.get("matched_keywords", []) or [],
        )
    return None


def _format_sections(sections, limit):
    cleaned = []
    for heading, body in sections or []:
        heading = re.sub(r"\s+", " ", str(heading or "本文")).strip()
        body = re.sub(r"\s+", " ", str(body or "")).strip()
        if body:
            cleaned.append((heading, body))
    if not cleaned:
        return "", False
    full = "\n\n".join(f"## {h}\n{b}" for h, b in cleaned)
    if len(full) <= limit:
        return full, False

    parts = []
    remaining = limit
    per_section = max(1800, limit // max(1, len(cleaned)) - 80)
    for heading, body in cleaned:
        if remaining <= 500:
            break
        chunk_limit = min(per_section, max(0, remaining - len(heading) - 5))
        chunk = body[:chunk_limit]
        if len(body) > chunk_limit:
            chunk = chunk.rstrip() + "\n[このセクションは長いため以降を省略]"
        block = f"## {heading}\n{chunk}"
        parts.append(block)
        remaining -= len(block) + 2
    return "\n\n".join(parts), True


def _sections_from_pdf_bytes(data, title):
    sections = fulltext._sections_from_pdf(data, title)
    if sections:
        return sections
    return fulltext._sections_from_text(fulltext._pdf_to_text(data))


def _fallback_fulltext_sections(paper):
    if not paper:
        return [], "abstract"
    if paper.arxiv_id:
        text = fulltext.fetch_arxiv_fulltext(paper.arxiv_id)
        sections = fulltext._sections_from_text(text)
        if sections:
            return sections, "fulltext(arxiv-text)"
        sections = fulltext.fetch_ar5iv_sections(paper.arxiv_id)
        if sections:
            return sections, "fulltext(ar5iv)"
        text = fulltext.fetch_ar5iv_fulltext(paper.arxiv_id)
        sections = fulltext._sections_from_text(text)
        if sections:
            return sections, "fulltext(ar5iv-text)"
        for url in fulltext._arxiv_pdf_urls(paper.arxiv_id):
            data = fulltext._download_pdf(url, min_interval=3.0)
            if not data:
                continue
            sections = _sections_from_pdf_bytes(data, paper.title)
            if sections:
                return sections, "fulltext(arxiv-pdf)"

    urls = []
    if _looks_like_pdf_url(paper.pdf_url):
        urls.append(paper.pdf_url)
    if paper.doi:
        try:
            url = fulltext._unpaywall_pdf_url(paper.doi)
        except Exception:
            url = ""
        if url:
            urls.append(url)
    for url in dict.fromkeys(urls):
        data = fulltext._download_pdf(url)
        if not data:
            continue
        sections = _sections_from_pdf_bytes(data, paper.title)
        if sections:
            return sections, "fulltext(oa-pdf)"
    return [], "abstract"


def _paper_fetch_hint(paper):
    if not paper:
        return "paper=未解決"
    return (
        f"arxiv_id={paper.arxiv_id or '-'} / "
        f"doi={paper.doi or '-'} / "
        f"pdf_url={paper.pdf_url or '-'} / "
        f"url={paper.url or '-'}"
    )


def _source_context(
    path,
    html_text,
    title,
    html_body,
    limit,
    allow_html_fallback,
    arxiv_id_override="",
    pdf_url_override="",
):
    rel = _relpath(path)
    _, key, seen_info = _seen_entry_for_path(path)
    html_info = _html_metadata(html_text, title, rel)
    info = _merge_info(seen_info, html_info)
    if arxiv_id_override:
        info["arxiv_id"] = arxiv_id_override
    if pdf_url_override:
        info["pdf_url"] = pdf_url_override
    paper = _paper_from_info(key, info)
    if paper:
        print(f"本文候補: {_paper_fetch_hint(paper)}")
        sections, basis = fulltext.fetch_sections(paper)
        if not sections:
            sections, basis = _fallback_fulltext_sections(paper)
        context, truncated = _format_sections(sections, limit)
        if context:
            if truncated:
                basis += ", truncated"
            return context, basis, paper

    if not allow_html_fallback:
        hint = "HTML要約での回答は行いません。必要なら --allow-html-fallback を付けてください。"
        raise SystemExit(f"arXiv/PDF本文を取得できませんでした。{_paper_fetch_hint(paper)}。{hint}")
    fallback = html_body[:limit]
    return fallback, "generated-html", paper


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
    if ".followups" in text and ".dialogue" in text and ".bubble table" in text:
        return text
    style_end = text.find("</style>")
    if style_end == -1:
        return text
    return text[:style_end] + FOLLOWUP_CSS + text[style_end:]


def _inline_markdown(text):
    text = render._esc(text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*\n]+)\*\*", r"<strong>\1</strong>", text)
    return text


def _is_table_separator(line):
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", c or "") for c in cells)


def _is_table_row(line):
    raw = line.strip()
    return raw.startswith("|") and "|" in raw[1:]


def _split_table_row(line):
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    return cells


def _table_html(lines):
    rows = [_split_table_row(line) for line in lines if not _is_table_separator(line)]
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    head, body = rows[0], rows[1:]
    html = ['<div class="table-wrap"><table><thead><tr>']
    html.extend(f"<th>{_inline_markdown(cell)}</th>" for cell in head)
    html.append("</tr></thead>")
    if body:
        html.append("<tbody>")
        for row in body:
            html.append("<tr>")
            html.extend(f"<td>{_inline_markdown(cell)}</td>" for cell in row)
            html.append("</tr>")
        html.append("</tbody>")
    html.append("</table></div>")
    return "".join(html)


def _bubble_html(text):
    lines = str(text or "").splitlines()
    blocks = []
    para = []
    items = []
    table = []

    def flush_para():
        nonlocal para
        if para:
            blocks.append("<p>" + "<br>".join(_inline_markdown(x) for x in para) + "</p>")
            para = []

    def flush_items():
        nonlocal items
        if items:
            blocks.append(
                "<ul>"
                + "".join(f"<li>{_inline_markdown(x)}</li>" for x in items)
                + "</ul>"
            )
            items = []

    def flush_table():
        nonlocal table
        if table:
            blocks.append(_table_html(table))
            table = []

    for line in lines:
        raw = line.strip()
        if not raw:
            flush_table()
            flush_items()
            flush_para()
            continue
        if _is_table_row(raw):
            flush_items()
            flush_para()
            table.append(raw)
            continue
        flush_table()
        h = re.match(r"^#{1,6}\s+(.+)$", raw)
        if h:
            flush_items()
            flush_para()
            blocks.append(f"<h3>{_inline_markdown(h.group(1).strip())}</h3>")
            continue
        m = re.match(r"^[*-]\s+(.*)$", raw)
        if m:
            flush_para()
            items.append(m.group(1).strip())
        else:
            flush_items()
            para.append(raw)

    flush_table()
    flush_items()
    flush_para()
    return "".join(blocks) if blocks else "<p></p>"


def _dialogue_html(question, answer, engine, basis):
    generated = datetime.datetime.now().isoformat(timespec="seconds")
    return (
        '<div class="dialogue">\n'
        '  <div class="turn question">'
        '<div class="speaker">Q</div>'
        f'<div class="bubble">{_bubble_html(question)}</div>'
        '</div>\n'
        '  <div class="turn answer">'
        '<div class="speaker">A</div>'
        f'<div class="bubble">{_bubble_html(answer)}</div>'
        '</div>\n'
        f'  <div class="dialogue-meta">回答根拠: {render._esc(basis)}'
        f' ・ 回答エンジン: {render._esc(engine)}'
        f' ・ 追記日: {render._esc(generated)}</div>\n'
        '</div>\n'
    )


def _append_followup(text, question, answer, engine, basis):
    text = _ensure_dialogue_css(text)
    block = _dialogue_html(question, answer, engine, basis)
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


def _clear_followups(text):
    start = text.find(FOLLOWUP_START)
    end = text.find(FOLLOWUP_END)
    if start != -1 and end != -1 and end > start:
        end += len(FOLLOWUP_END)
        return (text[:start].rstrip() + "\n\n" + text[end:].lstrip()).strip() + "\n"
    return re.sub(
        r'\n?\s*<section class="followups"[\s\S]*?</section>\s*\n?',
        "\n",
        text,
        count=1,
        flags=re.I,
    )


def _ask_llm(summarizer, title, source_body, question, basis, history):
    if summarizer.stub:
        return "（スタブ回答）LLM未接続のため、実運用ではGemmaがこの質問に回答します。"
    system = HTML_FALLBACK_SYSTEM if basis == "generated-html" else QA_SYSTEM
    history_part = (
        f"\n\n会話履歴（文脈用。根拠は元論文本文を優先）:\n{history[-6000:]}"
        if history
        else ""
    )
    return summarizer._chat(
        system,
        f"論文タイトル:\n{title}\n\n"
        f"回答根拠:\n{basis}\n\n"
        f"元論文本文:\n{source_body}\n"
        f"{history_part}\n\n"
        "回答方針:\n"
        "論文固有の説明は上の本文に基づける。質問に含まれる標準的な研究用語や周辺概念は、"
        "一般知識で補足してよいが、その場合は本文由来ではない補足だと分かるように書く。\n\n"
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
    ap.add_argument("--arxiv-id", help="本文取得に使うarXiv IDを明示指定する")
    ap.add_argument("--pdf-url", help="本文取得に使うPDF URLを明示指定する")
    ap.add_argument("--context-chars", type=int, default=60000, help="Gemmaに渡す元論文本文の最大文字数")
    ap.add_argument("--replace-followups", action="store_true", help="既存の追加質問を消してから追記する")
    ap.add_argument(
        "--allow-html-fallback",
        action="store_true",
        help="arXiv/PDF本文を取得できない場合のみ生成済みHTMLで回答する",
    )
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
    if args.stub:
        source_body, basis = body[:args.context_chars], "stub"
    else:
        source_body, basis, _ = _source_context(
            path,
            text,
            title,
            body,
            max(1000, args.context_chars),
            args.allow_html_fallback,
            args.arxiv_id or "",
            args.pdf_url or "",
        )
    print(f"対象: {os.path.relpath(path, ROOT)}")
    print(f"タイトル: {title}")
    print(f"回答根拠: {basis}")
    print(f"回答エンジン: {summarizer.engine}")

    updated = _clear_followups(text) if args.replace_followups else text
    history = ""
    for question in args.question:
        answer = _ask_llm(summarizer, title, source_body, question, basis, history)
        print("\nQ:", question)
        print("A:", answer)
        updated = _append_followup(updated, question, answer, summarizer.engine, basis)
        history += f"\n\nQ: {question}\nA: {answer}"

    if not args.dry_run:
        _write(path, updated)
        print(f"\n追記: {os.path.relpath(path, ROOT)}")
        print("公開: git add -A && git commit -m 'add paper followup qa' && git push origin main")
    return 0


if __name__ == "__main__":
    sys.exit(main())

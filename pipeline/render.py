"""テンプレートにHTMLを流し込む。ユーザー/論文/LLM由来テキストは必ずエスケープ。

テンプレートは {{token}} を置換するだけの最小実装（CSSの単一波括弧は触らない）。
リンクは相対パスにして、ローカル(file://)でも GitHub Pages でも動くようにする。
"""
import datetime
import html
import os
import re

from .summarize import SECTIONS


def _esc(s):
    return html.escape(str(s or ""))


def _multiline(s):
    return _esc(s).replace("\n", "<br>")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def render_template(text, ctx):
    return re.sub(r"\{\{\s*(\w+)\s*\}\}", lambda m: str(ctx.get(m.group(1), "")), text)


def _today():
    return datetime.date.today().isoformat()


def _basis_label(basis):
    return {
        "fulltext(arxiv)": "本文(arXiv)",
        "fulltext(oa-pdf)": "本文(OA-PDF)",
        "fulltext(arxiv-pdf)": "本文(arXiv PDF)",
        "fulltext(pdf)": "本文(PDF)",
        "fulltext": "本文",
    }.get(basis, "アブストラクト")


def _selection_label(kind, fallback=""):
    return {
        "important": "重要論文",
        "recent": "新着論文",
        "fallback": "補充候補",
        "manual": "手動追加",
    }.get(kind or "", fallback or "")


def _basis_quality(basis):
    return "fulltext" if str(basis or "").startswith("fulltext") else "abstract"


def _as_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _latest_added(entries):
    dates = [e.get("added", "") for e in entries if e.get("added", "")]
    return max(dates) if dates else ""


def _entry_sort_key(entry):
    return (entry.get("added_at") or entry.get("added", ""), entry.get("date", ""))


def _keyword_tags(keywords):
    items = [str(k).strip() for k in (keywords or []) if str(k).strip()]
    if not items:
        return ""
    return '<div class="tags">' + "".join(
        f'<span class="tag">{_esc(k)}</span>' for k in items
    ) + "</div>"


def _chip(text, klass=""):
    cls = f' class="chip {klass}"' if klass else ' class="chip"'
    return f"<span{cls}>{_esc(text)}</span>"


def _class_token(s):
    return re.sub(r"[^a-z0-9_-]+", "-", str(s or "").lower()).strip("-")


def _reading_value_label(value):
    score = _as_int(value, 0)
    return f"読む価値 {score}/5" if score else ""


def _clean_venue(venue):
    venue = re.sub(r"\s+", " ", str(venue or "")).strip()
    venue = re.sub(r"^採択先:\s*", "", venue)
    venue = re.sub(r"^掲載先:\s*", "", venue)
    return venue


def _is_preprint_venue(venue):
    v = _clean_venue(venue).lower()
    v = re.sub(r"[\s._-]+", "", v)
    return v in {"arxiv", "arxivorg", "arxivcornelluniversity"}


def _venue_label(venue, missing="未取得"):
    venue = _clean_venue(venue)
    if (
        not venue
        or venue.lower() in {"unknown", "n/a", "none", "-"}
        or venue == "未取得"
        or _is_preprint_venue(venue)
    ):
        return missing
    return venue


def _entry_reason_chips(entry, keywords=None):
    chips = []
    selection = _selection_label(entry.get("selection"), entry.get("selection_label", ""))
    if selection:
        chips.append(_chip(selection, f"selection-{_class_token(entry.get('selection', ''))}"))
    kw_count = len([k for k in (keywords or []) if str(k).strip()])
    if kw_count:
        chips.append(_chip(f"キーワード一致 {kw_count}", "keyword-match"))
    if entry.get("citations") is not None:
        chips.append(_chip(f"被引用 {_as_int(entry.get('citations'))}"))
    if entry.get("relevance") is not None:
        chips.append(_chip(f"関連度 {_as_int(entry.get('relevance'))}"))
    basis = entry.get("basis", "")
    if basis:
        quality = _basis_quality(basis)
        chips.append(_chip(_basis_label(basis), f"basis-{quality}"))
    rv = _reading_value_label(entry.get("reading_value"))
    if rv:
        chips.append(_chip(rv, "reading-value"))
    if not chips:
        return ""
    return '<div class="reason-chips">' + "".join(chips) + "</div>"


def _paper_facts(paper, summary):
    chips = []
    selection = getattr(paper, "selection_label", "") or _selection_label(
        getattr(paper, "selection_type", "")
    )
    if selection:
        chips.append(_chip(selection, f"selection-{_class_token(getattr(paper, 'selection_type', ''))}"))
    venue = _venue_label(getattr(paper, "venue", ""), missing="")
    if venue:
        chips.append(_chip(f"採択先 {venue}", "venue"))
    chips.append(_chip(f"公開日 {paper.published or '-'}"))
    kw_count = len([k for k in (getattr(paper, "matched_keywords", []) or []) if str(k).strip()])
    if kw_count:
        chips.append(_chip(f"キーワード一致 {kw_count}", "keyword-match"))
    if getattr(paper, "citations_known", True):
        chips.append(_chip(f"被引用 {_as_int(getattr(paper, 'citations', 0))}"))
    relevance = getattr(paper, "relevance_score", None)
    if relevance is not None:
        chips.append(_chip(f"関連度 {_as_int(relevance)}"))
    basis = summary.get("_basis", "")
    chips.append(_chip(_basis_label(basis), f"basis-{_basis_quality(basis)}"))
    rv = _reading_value_label(summary.get("_reading_value"))
    if rv:
        chips.append(_chip(rv, "reading-value"))
    reason = (summary.get("_reading_value_reason") or "").strip()
    reason_html = f'<div class="rating-reason">{_esc(reason)}</div>' if reason else ""
    return '<div class="paper-facts">' + "".join(chips) + reason_html + "</div>"


def _source_notice(summary):
    basis = summary.get("_basis", "")
    if _basis_quality(basis) == "fulltext":
        return (
            '<div class="source-notice fulltext">'
            f"本文取得済み: {_basis_label(basis)}を根拠に要約しています。"
            "</div>"
        )
    return (
        '<div class="source-notice abstract">'
        "本文未取得: アブストラクトのみを根拠にしています。詳細確認には原典を参照してください。"
        "</div>"
    )


def _latest_report_link(root):
    run_dir = os.path.join(root, "data", "runs")
    try:
        names = sorted(
            n for n in os.listdir(run_dir)
            if re.match(r"\d{4}-\d{2}-\d{2}\.html$", n)
        )
    except OSError:
        return ""
    if not names:
        return ""
    href = "data/runs/" + names[-1]
    return f'<div class="report-link"><a href="{_esc(href)}">最新実行レポート</a></div>'


def _matched_entry_keywords(entry, keywords):
    if entry.get("matched_keywords"):
        return entry.get("matched_keywords", [])
    text = f"{entry.get('title', '')} {entry.get('tldr', '')}".lower()
    out = []
    for kw in keywords or []:
        word = str(kw or "").strip()
        if not word:
            continue
        if re.search(r"\b" + re.escape(word.lower()) + r"\b", text):
            out.append(word)
    return out


def _entry_with_keywords(entry, keywords):
    item = dict(entry)
    item["matched_keywords"] = _matched_entry_keywords(item, keywords)
    return item


def _entry_authors(entry, root=None):
    authors = entry.get("authors")
    if isinstance(authors, list):
        return ", ".join(str(a).strip() for a in authors if str(a).strip())
    if authors:
        return str(authors)
    if not root or not entry.get("file"):
        return ""
    root_abs = os.path.abspath(root)
    path = os.path.abspath(os.path.join(root_abs, entry.get("file", "")))
    if not (path == root_abs or path.startswith(root_abs + os.sep)):
        return ""
    try:
        text = _read(path)
    except OSError:
        return ""
    m = re.search(r'<div class="meta">(.*?)<br>', text, re.S)
    if not m:
        return ""
    return html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()


def _entry_venue(entry, root=None):
    venue = _venue_label(entry.get("venue", ""), missing="")
    if venue:
        return venue
    if not root or not entry.get("file"):
        return ""
    root_abs = os.path.abspath(root)
    path = os.path.abspath(os.path.join(root_abs, entry.get("file", "")))
    if not (path == root_abs or path.startswith(root_abs + os.sep)):
        return ""
    try:
        text = _read(path)
    except OSError:
        return ""
    m = re.search(r'<div class="meta">.*?<br>(.*?)</div>', text, re.S)
    if not m:
        return ""
    rest = html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()
    venue_text = rest.split("・", 1)[0].strip()
    return _venue_label(venue_text, missing="")


def render_paper_page(tpl_dir, paper, summary):
    sections_html = ""
    for key, heading in SECTIONS:
        sections_html += (
            f'<section class="qa"><h2>{_esc(heading)}</h2>'
            f"<p>{_multiline(summary.get(key, ''))}</p></section>\n"
        )
    # セクション別の詳細要約（多段要約のときのみ）
    secsum = summary.get("sections") or []
    detail_html = ""
    if secsum:
        detail_html = '<h2 class="secs-title">セクション別の詳細要約</h2>\n'
        for s in secsum:
            detail_html += (
                f'<section class="secsum"><h3>{_esc(s.get("heading", ""))}</h3>'
                f"<p>{_multiline(s.get('summary', ''))}</p></section>\n"
            )
    links = []
    if paper.url:
        links.append(f'<a href="{_esc(paper.url)}" target="_blank" rel="noopener">原典</a>')
    if paper.pdf_url:
        links.append(f'<a href="{_esc(paper.pdf_url)}" target="_blank" rel="noopener">PDF</a>')
    if paper.doi:
        links.append(
            f'<a href="https://doi.org/{_esc(paper.doi)}" target="_blank" rel="noopener">DOI</a>'
        )
    ctx = {
        "title": _esc(paper.title),
        "tldr": _multiline(summary.get("tldr", "")),
        "authors": _esc(", ".join(paper.authors[:12])),
        "venue": _esc(_venue_label(paper.venue)),
        "published": _esc(paper.published),
        "source": _esc(paper.source),
        "links": " ・ ".join(links),
        "keyword_tags": _keyword_tags(getattr(paper, "matched_keywords", [])),
        "paper_facts": _paper_facts(paper, summary),
        "source_notice": _source_notice(summary),
        "sections": sections_html,
        "sections_detail": detail_html,
        "followups": summary.get("_followups_html", ""),
        "engine": _esc(summary.get("_engine", "")),
        "basis": _basis_label(summary.get("_basis", "")),
        "generated": _today(),
    }
    return render_template(_read(os.path.join(tpl_dir, "paper.html")), ctx)


def _list_items(entries, link_basename=False, highlight_added="", keywords=None, root=None):
    rows = ""
    for idx, it in enumerate(entries):
        tags = _matched_entry_keywords(it, keywords)
        authors = _entry_authors(it, root)
        venue = _entry_venue(it, root)
        href = os.path.basename(it["file"]) if link_basename else it["file"]
        latest = bool(highlight_added and it.get("added") == highlight_added)
        klass = ' class="latest"' if latest else ""
        badge = '<span class="badge-latest">New</span>' if latest else ""
        added_sort = "|".join(_entry_sort_key(it))
        title_sort = (it.get("title") or "").lower()
        author_html = f'<div class="authors">{_esc(authors)}</div>' if authors else ""
        venue_html = f'<div class="venue">採択先: {_esc(venue or "未取得")}</div>'
        reason_html = _entry_reason_chips(it, tags)
        item_id = it.get("file", href)
        reading_value = _as_int(it.get("reading_value"), 0)
        search_text = " ".join(
            [
                it.get("title", ""),
                authors,
                venue,
                it.get("date", ""),
                it.get("tldr", ""),
                it.get("selection_label", ""),
                _selection_label(it.get("selection")),
                " ".join(tags),
            ]
        ).lower()
        rows += (
            f'<li{klass} data-added="{_esc(added_sort)}" '
            f'data-published="{_esc(it.get("date", ""))}" '
            f'data-value="{reading_value}" data-item-id="{_esc(item_id)}" '
            f'data-title="{_esc(title_sort)}" data-search="{_esc(search_text)}" '
            f'data-original="{idx}">'
            f'{badge}<a href="{_esc(href)}">{_esc(it["title"])}</a>'
            f"{author_html}"
            f"{venue_html}"
            f"{reason_html}"
            f"{_keyword_tags(tags)}"
            f'<div class="meta">{_esc(it.get("date", ""))} ・ {_esc(it.get("tldr", ""))}</div>'
            '<div class="paper-actions" aria-label="論文操作">'
            '<button type="button" data-state-button="read">既読</button>'
            '<button type="button" data-state-button="want">あとで</button>'
            '<button type="button" data-state-button="favorite">★</button>'
            '<button type="button" data-state-button="hidden">非表示</button>'
            '</div></li>\n'
        )
    return rows


def render_user_index(tpl_dir, root, uslug, username, useen, keywords=None):
    entries = sorted(useen.values(), key=_entry_sort_key, reverse=True)
    ctx = {
        "username": _esc(username),
        "count": str(len(entries)),
        # 同ディレクトリ内なのでファイル名だけの相対リンク
        "items": _list_items(
            entries,
            link_basename=True,
            highlight_added=_latest_added(entries),
            keywords=keywords,
            root=root,
        ),
        "generated": _today(),
    }
    out = render_template(_read(os.path.join(tpl_dir, "user_index.html")), ctx)
    os.makedirs(os.path.join(root, uslug), exist_ok=True)
    with open(os.path.join(root, uslug, "index.html"), "w", encoding="utf-8") as f:
        f.write(out)


def render_global_index(tpl_dir, root, subs, seen, slugify):
    cards = ""
    recent = []
    latest_auto_dates = []
    for sub in subs:
        username = sub.get("username", "")
        uslug = slugify(username, fallback="user")
        display = sub.get("label") or username
        useen = seen.get(uslug, {})
        if not sub.get("manual"):
            latest = _latest_added(useen.values())
            if latest:
                latest_auto_dates.append(latest)
        kw = ", ".join(sub.get("keywords", []))
        meta = (f"キーワード: {_esc(kw)} ・ {len(useen)}本" if kw else f"{len(useen)}本")
        cards += (
            f'<div class="card"><h3><a href="{_esc(uslug)}/index.html">{_esc(display)}</a></h3>'
            f'<div class="meta">{meta}</div></div>\n'
        )
        recent.extend(_entry_with_keywords(v, sub.get("keywords", [])) for v in useen.values())
    recent.sort(key=_entry_sort_key, reverse=True)
    latest_auto_added = max(latest_auto_dates) if latest_auto_dates else ""
    ctx = {
        "cards": cards,
        "recent": _list_items(
            recent[:30],
            link_basename=False,
            highlight_added=latest_auto_added,
            root=root,
        ),
        "report_link": _latest_report_link(root),
        "generated": _today(),
    }
    out = render_template(_read(os.path.join(tpl_dir, "index.html")), ctx)
    with open(os.path.join(root, "index.html"), "w", encoding="utf-8") as f:
        f.write(out)

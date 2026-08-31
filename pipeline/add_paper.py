#!/usr/bin/env python3
"""任意の論文PDFを手動でHTML化する（日次cronとは独立）。

単一PDFだけでなく、リポジトリ配下のフォルダに置いた複数PDFを同じ分野へ
まとめて追加できる。入力PDF自体は公開せず、生成HTMLだけをリンクする。
"""
import argparse
import datetime
import os
from pathlib import Path
import re
import shlex
import sys

import yaml

from . import render
from .dedup import build_seen_aliases, load_seen, paper_is_seen, save_seen
from .fulltext import _pdf_to_text, _sections_from_pdf, fetch_sections
from .schema import Paper
from .sources import arxiv as arxiv_src
from .summarize import Summarizer
from .util import http_get, sha1, slugify
from .venue import enrich_venue

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TPL = os.path.join(ROOT, "templates")
SEEN = os.path.join(ROOT, "data", "seen.json")
DEFAULT_FIELD = "reading"


def _load_subs():
    with open(os.path.join(ROOT, "subscriptions.yml"), encoding="utf-8") as f:
        return (yaml.safe_load(f) or {}).get("subscriptions", [])


def _matched_keywords(paper, keywords):
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


def _source_quality(basis):
    return "fulltext" if str(basis or "").startswith("fulltext") else "abstract"


def _from_arxiv(raw):
    aid = raw.rsplit("/abs/", 1)[-1].replace("arxiv:", "").replace("arXiv:", "").strip()
    paper = arxiv_src.fetch_meta(aid) or Paper(source="arxiv", title=aid, arxiv_id=aid)
    paper.arxiv_id = aid
    sections, basis = fetch_sections(paper)
    return paper, sections, basis


def _clean_pdf_value(value):
    return re.sub(r"\s+", " ", str(value or "").replace("\x00", " ")).strip()


def _usable_pdf_title(value):
    title = _clean_pdf_value(value)
    lowered = title.casefold()
    return bool(
        5 <= len(title) <= 350
        and lowered not in {"untitled", "document", "paper", "title"}
        and not lowered.startswith(("microsoft word -", "acrobat distiller"))
        and not re.fullmatch(r"(?:arxiv:)?\d{4}\.\d{4,5}(?:v\d+)?", lowered)
    )


def _split_pdf_authors(value):
    raw = _clean_pdf_value(value)
    if not raw:
        return []
    if ";" in raw:
        parts = raw.split(";")
    elif re.search(r"\s+and\s+", raw, flags=re.IGNORECASE):
        parts = re.split(r"\s+and\s+", raw, flags=re.IGNORECASE)
    else:
        parts = [raw]
    return [part.strip() for part in parts if part.strip()]


def _pdf_metadata(data):
    try:
        import fitz

        doc = fitz.open(stream=data, filetype="pdf")
        metadata = doc.metadata or {}
        doc.close()
    except Exception:
        return {}, []
    return metadata, _split_pdf_authors(metadata.get("author"))


def _first_page_title(data):
    """先頭ページ上部の最大フォント行からタイトルを推定する。"""
    try:
        import fitz

        doc = fitz.open(stream=data, filetype="pdf")
        if not doc.page_count:
            doc.close()
            return ""
        page = doc[0]
        page_height = float(page.rect.height or 1)
        lines = []
        for block in page.get_text("dict").get("blocks", []):
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                text = _clean_pdf_value("".join(span.get("text", "") for span in spans))
                if not text or len(text) > 350:
                    continue
                y = min((float(span.get("bbox", [0, 0, 0, 0])[1]) for span in spans), default=0)
                size = max((float(span.get("size", 0)) for span in spans), default=0)
                if y <= page_height * 0.48 and size > 0:
                    lines.append((y, size, text))
        doc.close()
    except Exception:
        return ""
    if not lines:
        return ""
    max_size = max(size for _y, size, _text in lines)
    selected = [
        (y, text)
        for y, size, text in lines
        if size >= max_size - 0.7
        and not re.search(r"\b(?:arxiv|doi)\s*[:/]", text, flags=re.IGNORECASE)
    ]
    title = _clean_pdf_value(" ".join(text for _y, text in sorted(selected)[:4]))
    return title if _usable_pdf_title(title) else ""


def _extract_pdf_identifiers(text):
    head = (text or "")[:6000]
    doi_match = re.search(
        r"(?:\bdoi\s*:\s*|https?://(?:dx\.)?doi\.org/)"
        r"(10\.\d{4,9}/[-._;()/:A-Z0-9]+)",
        head,
        flags=re.IGNORECASE,
    )
    doi = doi_match.group(1).rstrip(".,;:)]}") if doi_match else ""
    arxiv_match = re.search(
        r"(?:arxiv\s*:\s*|arxiv\.org/(?:abs|pdf)/)"
        r"([a-z-]+(?:\.[a-z-]+)?/\d{7}|\d{4}\.\d{4,5})(?:v\d+)?",
        head,
        flags=re.IGNORECASE,
    )
    return doi, (arxiv_match.group(1) if arxiv_match else "")


def _extract_abstract(text, sections):
    for heading, body in sections:
        if "abstract" in (heading or "").casefold() and len(body.strip()) >= 80:
            return body.strip()[:5000]
    match = re.search(
        r"(?is)\babstract\b\s*[:.-]?\s*(.{80,5000}?)"
        r"(?:\n\s*(?:1(?:\.0?)?\s+)?introduction\b)",
        (text or "")[:12000],
    )
    return re.sub(r"\s+", " ", match.group(1)).strip() if match else ""


def _from_pdf_bytes(data, title="", url="", filename=""):
    if not data[:5].startswith(b"%PDF"):
        raise ValueError("PDF形式ではありません")
    text = _pdf_to_text(data)
    if not text:
        raise ValueError("PDFからテキスト抽出に失敗（画像PDFまたは破損PDF）")
    metadata, authors = _pdf_metadata(data)
    inferred_title = _clean_pdf_value(title)
    if not inferred_title:
        metadata_title = _clean_pdf_value(metadata.get("title"))
        inferred_title = metadata_title if _usable_pdf_title(metadata_title) else ""
    if not inferred_title:
        inferred_title = _first_page_title(data)
    if not inferred_title and filename:
        inferred_title = _clean_pdf_value(Path(filename).stem.replace("_", " "))
    if not _usable_pdf_title(inferred_title):
        inferred_title = _clean_pdf_value(text.split("\n", 1)[0])[:180]
    if not _usable_pdf_title(inferred_title):
        raise ValueError("論文タイトルを推定できません")

    sections = _sections_from_pdf(data, inferred_title)
    if not sections:
        raise ValueError("PDF本文をセクション分割できません")
    doi, arxiv_id = _extract_pdf_identifiers(text)
    landing_url = url
    pdf_url = ""
    if not landing_url and doi:
        landing_url = "https://doi.org/" + doi
    if arxiv_id:
        landing_url = landing_url or "https://arxiv.org/abs/" + arxiv_id
        pdf_url = "https://arxiv.org/pdf/" + arxiv_id
    paper = Paper(
        source="pdf",
        title=inferred_title,
        abstract=_extract_abstract(text, sections),
        authors=authors,
        url=landing_url,
        pdf_url=pdf_url,
        arxiv_id=arxiv_id,
        doi=doi,
    )
    return paper, sections, "fulltext(pdf)"


def _fill_arxiv_metadata(paper):
    """ローカルPDFにarXiv IDがあれば、正確な書誌情報を補完する。"""
    if not paper.arxiv_id:
        return paper
    try:
        candidate = arxiv_src.fetch_meta(paper.arxiv_id)
    except Exception as exc:
        print(f"      [warn] arXivメタデータ取得失敗: {exc!r}")
        return paper
    if not candidate:
        return paper
    if candidate.title:
        paper.title = candidate.title
    for attr in ("abstract", "published", "venue", "url", "pdf_url", "doi"):
        if not getattr(paper, attr, "") and getattr(candidate, attr, ""):
            setattr(paper, attr, getattr(candidate, attr))
    if candidate.authors:
        paper.authors = candidate.authors
    return paper


def _discover_pdf_files(folder, recursive=True, root=None):
    root_path = Path(root or ROOT).resolve()
    raw = Path(folder).expanduser()
    folder_path = (root_path / raw).resolve() if not raw.is_absolute() else raw.resolve()
    try:
        folder_path.relative_to(root_path)
    except ValueError as exc:
        raise ValueError("--folder は survey-app リポジトリ配下を指定してください") from exc
    if not folder_path.is_dir():
        raise ValueError(f"フォルダがありません: {folder_path}")
    iterator = folder_path.rglob("*") if recursive else folder_path.glob("*")
    files = []
    for path in iterator:
        if not path.is_file() or path.suffix.casefold() != ".pdf":
            continue
        try:
            path.resolve().relative_to(root_path)
        except ValueError:
            continue
        files.append(path)
    return sorted(files, key=lambda path: path.relative_to(root_path).as_posix().casefold())


def _destination(field, subs):
    uslug = slugify(field, fallback="reading")
    sub = next((s for s in subs if slugify(s.get("username", "")) == uslug), {})
    label = sub.get("label") or field
    return uslug, sub, label


def _unique_output_rel(uslug, paper):
    base = slugify(paper.title or paper.paper_id(), fallback="paper")
    rel = f"{uslug}/{base}.html"
    if not os.path.exists(os.path.join(ROOT, rel)):
        return rel
    suffix = sha1(paper.key())[:8]
    return f"{uslug}/{base[:70].rstrip('-')}-{suffix}.html"


def _seen_record(paper, summary, basis, rel, matched_keywords, added_at):
    return {
        "title": paper.title,
        "file": rel,
        "date": paper.published,
        "venue": render._venue_label(paper.venue, missing="", published=paper.published),
        "url": paper.url,
        "pdf_url": paper.pdf_url,
        "arxiv_id": paper.arxiv_id,
        "doi": paper.doi,
        "added": added_at[:10],
        "added_at": added_at,
        "authors": paper.authors,
        "tldr": summary.get("tldr", ""),
        "engine": summary.get("_engine", ""),
        "basis": summary.get("_basis", ""),
        "matched_keywords": matched_keywords,
        "selection": "manual",
        "selection_label": "手動追加",
        "citations": paper.citations,
        "relevance": len(matched_keywords),
        "source_quality": _source_quality(summary.get("_basis", basis)),
        "reading_value": summary.get("_reading_value", ""),
        "reading_value_reason": summary.get("_reading_value_reason", ""),
    }


def _add_prepared_paper(paper, sections, basis, uslug, sub, seen, summarizer):
    useen = seen.setdefault(uslug, {})
    aliases = build_seen_aliases(useen)
    if paper_is_seen(paper, aliases):
        return {"status": "skipped", "title": paper.title, "reason": "既に登録済み"}

    paper = _fill_arxiv_metadata(paper)
    paper = enrich_venue(paper)
    if paper_is_seen(paper, aliases):
        return {"status": "skipped", "title": paper.title, "reason": "既に登録済み"}

    print(f"  タイトル: {paper.title}")
    print(f"  セクション数: {len(sections)} / 根拠: {basis}")
    summary = summarizer.summarize(paper, sections=sections, basis=basis)
    matched_keywords = _matched_keywords(paper, sub.get("keywords", []))
    paper.matched_keywords = matched_keywords
    summary.update(summarizer.rate_reading_value(paper, summary, basis))
    paper.selection_type = "manual"
    paper.selection_label = "手動追加"
    paper.relevance_score = len(matched_keywords)
    paper.source_quality = _source_quality(summary.get("_basis", basis))
    paper.reading_value = summary.get("_reading_value", "")
    paper.reading_value_reason = summary.get("_reading_value_reason", "")

    rel = _unique_output_rel(uslug, paper)
    os.makedirs(os.path.join(ROOT, uslug), exist_ok=True)
    with open(os.path.join(ROOT, rel), "w", encoding="utf-8") as f:
        f.write(render.render_paper_page(TPL, paper, summary))
    added_at = datetime.datetime.now().isoformat(timespec="microseconds")
    useen[paper.key()] = _seen_record(
        paper, summary, basis, rel, matched_keywords, added_at
    )
    return {"status": "added", "title": paper.title, "file": rel}


def _render_and_save(subs, seen, uslug, sub, label):
    save_seen(SEEN, seen)
    render.render_user_index(
        TPL, ROOT, uslug, label, seen.get(uslug, {}), sub.get("keywords", [])
    )
    render.render_global_index(TPL, ROOT, subs, seen, slugify)


def _print_publish_command(files, uslug):
    targets = ["data/seen.json", "index.html", f"{uslug}/index.html", *files]
    unique = list(dict.fromkeys(targets))
    command = "git add -- " + " ".join(shlex.quote(target) for target in unique)
    print("公開（入力PDFは追加しません）:")
    print(f"  {command}")
    print("  git commit -m 'add papers from local PDFs'")
    print("  git pull --rebase origin main && git push origin main")


def _add_pdf_folder(args, uslug, sub, label, subs, seen, summarizer=None):
    try:
        files = _discover_pdf_files(args.pdf_dir, recursive=not args.no_recursive)
    except ValueError as exc:
        print(f"[error] {exc}")
        return 1
    if args.limit > 0:
        files = files[: args.limit]
    if not files:
        print("[error] 対象PDFがありません。")
        return 1

    summarizer = summarizer or Summarizer(stub=args.stub)
    print(f"対象フォルダ: {Path(args.pdf_dir)}")
    print(f"追加先: {uslug} / PDF {len(files)}件")
    print(f"要約エンジン: {summarizer.engine}")
    added, skipped, failed = [], [], []
    for index, path in enumerate(files, 1):
        relative = path.resolve().relative_to(Path(ROOT).resolve()).as_posix()
        print(f"\n=== [{index}/{len(files)}] {relative} ===")
        try:
            paper, sections, basis = _from_pdf_bytes(
                path.read_bytes(), filename=path.name
            )
            result = _add_prepared_paper(
                paper, sections, basis, uslug, sub, seen, summarizer
            )
            if result["status"] == "added":
                added.append(result)
                # 長時間処理が中断しても、完了分を次回重複処理しないよう記録する。
                save_seen(SEEN, seen)
                print(f"  + {result['file']}")
            else:
                skipped.append({"pdf": relative, **result})
                print(f"  [skip] {result['reason']}: {result['title']}")
        except Exception as exc:
            failed.append({"pdf": relative, "reason": str(exc)})
            print(f"  [error] {exc}")
            if args.fail_fast:
                break

    if added:
        _render_and_save(subs, seen, uslug, sub, label)
    print("\n=== 一括追加結果 ===")
    print(f"追加 {len(added)} / スキップ {len(skipped)} / 失敗 {len(failed)}")
    for item in failed:
        print(f"  失敗: {item['pdf']} ({item['reason']})")
    if added:
        _print_publish_command([item["file"] for item in added], uslug)
    return 1 if failed else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="手動で単一または複数の論文PDFをHTML化")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--arxiv", help="arXiv ID または URL")
    src.add_argument("--pdf", help="ローカルPDFのパス")
    src.add_argument("--url", help="PDFのURL")
    src.add_argument(
        "--pdf-dir", "--folder", dest="pdf_dir",
        help="survey-app配下のPDFフォルダ（一括・既定は再帰検索）",
    )
    ap.add_argument("--title", default="", help="単一PDFのタイトル（通常は自動取得）")
    dest = ap.add_mutually_exclusive_group()
    dest.add_argument(
        "--mapf", dest="field", action="store_const", const="mapf-mapd-warehouse",
        help="MAPF/MAPD/倉庫 分野に追加",
    )
    dest.add_argument(
        "--rag", dest="field", action="store_const", const="doc-structure-rag",
        help="文書構造解析/RAG 分野に追加",
    )
    dest.add_argument(
        "--field", default=None,
        help="任意のフィールドスラッグ（既定: reading）",
    )
    ap.add_argument("--no-recursive", action="store_true", help="フォルダ直下のPDFだけを処理")
    ap.add_argument("--limit", type=int, default=0, help="一括処理する最大件数（0は全件）")
    ap.add_argument("--fail-fast", action="store_true", help="最初の失敗で一括処理を停止")
    ap.add_argument("--stub", action="store_true", help="LLMを呼ばずスタブ要約で動作確認")
    args = ap.parse_args(argv)
    if args.pdf_dir and args.title:
        ap.error("--title は単一PDFでのみ指定できます")
    if args.limit < 0:
        ap.error("--limit は0以上を指定してください")

    field = args.field or DEFAULT_FIELD
    subs = _load_subs()
    uslug, sub, label = _destination(field, subs)
    seen = load_seen(SEEN)

    if args.pdf_dir:
        return _add_pdf_folder(args, uslug, sub, label, subs, seen)

    try:
        if args.arxiv:
            paper, sections, basis = _from_arxiv(args.arxiv)
        elif args.pdf:
            path = Path(args.pdf).expanduser()
            paper, sections, basis = _from_pdf_bytes(
                path.read_bytes(), args.title, filename=path.name
            )
        else:
            data = http_get(args.url, expect="bytes", timeout=60)
            paper, sections, basis = _from_pdf_bytes(
                data, args.title, url=args.url, filename=Path(args.url).name
            )
        summarizer = Summarizer(stub=args.stub)
        print(f"要約エンジン: {summarizer.engine}")
        result = _add_prepared_paper(
            paper, sections, basis, uslug, sub, seen, summarizer
        )
    except Exception as exc:
        print(f"[error] {exc}")
        return 1
    if result["status"] == "skipped":
        print(f"[skip] {result['reason']}: {result['title']}")
        return 0

    _render_and_save(subs, seen, uslug, sub, label)
    if not sub:
        print(
            f"  [note] '{uslug}' は subscriptions.yml に無いため、"
            "トップ一覧には出ません（ページは生成されます）。"
        )
    print(f"生成: {result['file']}")
    _print_publish_command([result["file"]], uslug)
    return 0


if __name__ == "__main__":
    sys.exit(main())

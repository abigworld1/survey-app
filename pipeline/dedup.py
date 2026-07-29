"""名寄せ（重複排除）と既出管理（seen.json）。"""
import json
import os
import re

from .schema import normalize_title


def _normalize_doi(value):
    value = str(value or "").strip().lower()
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value)
    return value.removeprefix("doi:").strip()


def _normalize_arxiv_id(value):
    value = str(value or "").strip().lower()
    value = re.sub(r"^https?://(?:www\.)?arxiv\.org/(?:abs|pdf)/", "", value)
    value = value.removeprefix("arxiv:").removesuffix(".pdf")
    return re.sub(r"v\d+$", "", value).strip()


def _doi_arxiv_id(value):
    doi = _normalize_doi(value)
    prefix = "10.48550/arxiv."
    return _normalize_arxiv_id(doi[len(prefix):]) if doi.startswith(prefix) else ""


def _as_int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _fulltext_score(paper):
    if getattr(paper, "arxiv_id", ""):
        return 3
    if getattr(paper, "pdf_url", ""):
        return 2
    if getattr(paper, "doi", ""):
        return 1
    return 0


def paper_aliases(paper):
    """論文を一意に照合するための DOI・arXiv ID・タイトル別名を返す。"""
    aliases = []
    title = normalize_title(getattr(paper, "title", ""))
    if title:
        aliases.append("title:" + title)
    doi = _normalize_doi(getattr(paper, "doi", ""))
    if doi:
        aliases.append("doi:" + doi)
    arxiv_id = _normalize_arxiv_id(getattr(paper, "arxiv_id", ""))
    if arxiv_id:
        aliases.append("arxiv:" + arxiv_id)
    doi_arxiv_id = _doi_arxiv_id(doi)
    if doi_arxiv_id:
        aliases.append("arxiv:" + doi_arxiv_id)
    return list(dict.fromkeys(aliases))


def seen_entry_aliases(key, info):
    """seen.json の1レコードから、候補照合に使う全別名を作る。"""
    aliases = []
    raw_key = str(key or "").strip()
    if raw_key.startswith("doi:"):
        doi = _normalize_doi(raw_key)
        if doi:
            aliases.append("doi:" + doi)
            doi_arxiv_id = _doi_arxiv_id(doi)
            if doi_arxiv_id:
                aliases.append("arxiv:" + doi_arxiv_id)
    elif raw_key.startswith("arxiv:"):
        arxiv_id = _normalize_arxiv_id(raw_key)
        if arxiv_id:
            aliases.append("arxiv:" + arxiv_id)
    elif raw_key.startswith("title:"):
        title = normalize_title(raw_key.removeprefix("title:"))
        if title:
            aliases.append("title:" + title)

    title = normalize_title(info.get("title", ""))
    if title:
        aliases.append("title:" + title)
    doi = _normalize_doi(info.get("doi", ""))
    if doi:
        aliases.append("doi:" + doi)
        doi_arxiv_id = _doi_arxiv_id(doi)
        if doi_arxiv_id:
            aliases.append("arxiv:" + doi_arxiv_id)
    arxiv_id = _normalize_arxiv_id(info.get("arxiv_id", ""))
    if arxiv_id:
        aliases.append("arxiv:" + arxiv_id)
    return list(dict.fromkeys(aliases))


def build_seen_aliases(seen_for_field):
    aliases = set()
    for key, info in (seen_for_field or {}).items():
        aliases.update(seen_entry_aliases(key, info or {}))
    return aliases


def paper_is_seen(paper, seen_or_aliases):
    """候補が過去登録と同じ論文なら True。識別子が変わってもタイトルで検出する。"""
    if isinstance(seen_or_aliases, set):
        aliases = seen_or_aliases
    else:
        aliases = build_seen_aliases(seen_or_aliases)
    return bool(set(paper_aliases(paper)) & aliases)


def _merge_papers(cur, paper):
    """本文取得しやすいレコードをベースにし、重要メタデータは補完する。"""
    citations = max(_as_int(getattr(cur, "citations", 0)), _as_int(getattr(paper, "citations", 0)))
    if (
        _fulltext_score(paper) > _fulltext_score(cur)
        or (
            _fulltext_score(paper) == _fulltext_score(cur)
            and not getattr(cur, "abstract", "")
            and getattr(paper, "abstract", "")
        )
    ):
        base, other = paper, cur
    else:
        base, other = cur, paper

    base.citations = citations
    for attr in ("pdf_url", "doi", "arxiv_id", "url", "venue", "published", "abstract"):
        if not getattr(base, attr, "") and getattr(other, attr, ""):
            setattr(base, attr, getattr(other, attr))
    if not getattr(base, "authors", None) and getattr(other, "authors", None):
        base.authors = other.authors
    return base


def dedup(papers):
    """同一論文（DOI/arXiv ID/正規化タイトルが一致）をまとめる。

    本文取得しやすい方（arXiv ID / PDF URL）を優先しつつ、
    被引用数など、ソース間で補完できる情報は最大/非空値を残す。
    出現順は維持。
    """
    best = {}
    alias_to_key = {}
    order = []
    for p in papers:
        aliases = paper_aliases(p)
        k = next((alias_to_key[a] for a in aliases if a in alias_to_key), None)
        if k is None:
            k = p.key()
            best[k] = p
            order.append(k)
            for alias in aliases:
                alias_to_key[alias] = k
            continue
        best[k] = _merge_papers(best[k], p)
        for alias in paper_aliases(best[k]) + aliases:
            alias_to_key[alias] = k
    return [best[k] for k in order]


def _entry_added_at(info):
    return str(info.get("added_at") or info.get("added") or "9999-99-99")


def _merge_seen_info(entries):
    """最初の追加日時を保ち、後から得たメタデータで同一論文を更新する。"""
    ordered = sorted(entries, key=lambda item: (_entry_added_at(item[1]), item[0]))
    keep_key, oldest = ordered[0]
    merged = dict(oldest)
    for _, info in ordered[1:]:
        for name, value in info.items():
            if name in {"added", "added_at", "file"}:
                continue
            if value not in (None, "", [], {}):
                merged[name] = value
    merged["added"] = oldest.get("added", "")
    merged["added_at"] = oldest.get("added_at", oldest.get("added", ""))
    merged["file"] = oldest.get("file", merged.get("file", ""))
    return keep_key, merged


def collapse_seen_duplicates(seen):
    """seen.json 内の同一論文レコードを統合し、削除したレコード一覧を返す。"""
    removed = []
    for field, entries in (seen or {}).items():
        keys = list(entries)
        if len(keys) < 2:
            continue

        parent = {key: key for key in keys}

        def find(key):
            while parent[key] != key:
                parent[key] = parent[parent[key]]
                key = parent[key]
            return key

        def union(left, right):
            left_root, right_root = find(left), find(right)
            if left_root != right_root:
                parent[right_root] = left_root

        alias_owner = {}
        file_owner = {}
        for key in keys:
            info = entries[key] or {}
            for alias in seen_entry_aliases(key, info):
                if alias in alias_owner:
                    union(key, alias_owner[alias])
                else:
                    alias_owner[alias] = key
            rel = info.get("file", "")
            if rel:
                if rel in file_owner:
                    union(key, file_owner[rel])
                else:
                    file_owner[rel] = key

        groups = {}
        for key in keys:
            groups.setdefault(find(key), []).append((key, entries[key] or {}))
        for group in groups.values():
            if len(group) < 2:
                continue
            keep_key, merged = _merge_seen_info(group)
            for key, info in group:
                if key == keep_key:
                    continue
                removed.append(
                    {"field": field, "key": key, "kept": keep_key, "file": info.get("file", "")}
                )
                entries.pop(key, None)
            entries[keep_key] = merged
    return removed


def load_seen(path):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_seen(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)

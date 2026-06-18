"""名寄せ（重複排除）と既出管理（seen.json）。"""
import json
import os

from .schema import normalize_title


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


def _aliases(paper):
    aliases = [paper.key()]
    title = normalize_title(getattr(paper, "title", ""))
    if title:
        aliases.append("title:" + title)
    if getattr(paper, "doi", ""):
        aliases.append("doi:" + paper.doi.lower())
    if getattr(paper, "arxiv_id", ""):
        aliases.append("arxiv:" + paper.arxiv_id.lower())
    return list(dict.fromkeys(aliases))


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
        aliases = _aliases(p)
        k = next((alias_to_key[a] for a in aliases if a in alias_to_key), None)
        if k is None:
            k = p.key()
            best[k] = p
            order.append(k)
            for alias in aliases:
                alias_to_key[alias] = k
            continue
        best[k] = _merge_papers(best[k], p)
        for alias in _aliases(best[k]) + aliases:
            alias_to_key[alias] = k
    return [best[k] for k in order]


def load_seen(path):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_seen(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)

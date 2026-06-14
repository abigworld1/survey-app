"""名寄せ（重複排除）と既出管理（seen.json）。"""
import json
import os


def dedup(papers):
    """同一論文（DOI/arXiv ID/正規化タイトルが一致）をまとめる。

    abstract を持つ方を優先（DBLP のように abstract 無しのものを上書き）。
    被引用数など、ソース間で補完できる情報は最大/非空値を残す。
    出現順は維持。
    """
    best = {}
    order = []
    for p in papers:
        k = p.key()
        if k not in best:
            best[k] = p
            order.append(k)
            continue
        cur = best[k]
        citations = max(int(getattr(cur, "citations", 0) or 0), int(getattr(p, "citations", 0) or 0))
        if not cur.abstract and p.abstract:
            best[k] = p
            cur = best[k]
        cur.citations = citations
        for attr in ("pdf_url", "doi", "arxiv_id", "url", "venue", "published"):
            if not getattr(cur, attr, "") and getattr(p, attr, ""):
                setattr(cur, attr, getattr(p, attr))
        if not cur.authors and p.authors:
            cur.authors = p.authors
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

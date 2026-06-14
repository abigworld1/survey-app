"""OpenAlex（鍵不要・分野横断）。abstract は inverted index を復元する。"""
import urllib.parse

from ..schema import Paper
from ..util import http_get

ENDPOINT = "https://api.openalex.org/works"


def _reconstruct_abstract(inv):
    if not inv:
        return ""
    pos = {}
    for word, idxs in inv.items():
        for i in idxs:
            pos[i] = word
    return " ".join(pos[i] for i in sorted(pos))


def search(keywords, limit=25, mode="recent"):
    sort = "cited_by_count:desc" if mode == "important" else "publication_date:desc"
    q = urllib.parse.urlencode(
        {
            "search": " ".join(keywords),
            "sort": sort,
            "per-page": min(limit, 50),
            # mailto を付けると OpenAlex の "polite pool" になり安定する
            "mailto": "hirayama.h77@gmail.com",
        }
    )
    data = http_get(ENDPOINT + "?" + q, timeout=40, min_interval=0.2)
    out = []
    for w in data.get("results", []):
        authors = [
            (a.get("author") or {}).get("display_name") for a in w.get("authorships", [])
        ]
        venue = (
            ((w.get("primary_location") or {}).get("source") or {}).get("display_name")
            or ""
        )
        # OA の直リンク（あれば本文取得の候補に使う）
        oa = w.get("open_access") or {}
        best = w.get("best_oa_location") or {}
        pdf = best.get("pdf_url") or oa.get("oa_url") or ""
        out.append(
            Paper(
                source="openalex",
                title=w.get("title") or w.get("display_name") or "",
                abstract=_reconstruct_abstract(w.get("abstract_inverted_index")),
                authors=[a for a in authors if a],
                published=(w.get("publication_date") or "")[:10],
                venue=venue,
                url=w.get("id", ""),
                pdf_url=pdf,
                doi=(w.get("doi") or "").replace("https://doi.org/", ""),
                citations=int(w.get("cited_by_count") or 0),
            )
        )
    return out

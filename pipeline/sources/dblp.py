"""DBLP 検索（会議録の網羅に強い）。

注意: DBLP は abstract を持たない。ここでは発見（タイトル/著者/会議/DOI）に使い、
abstract は dedup 後に他ソース（arXiv/S2/OpenAlex）由来のものへ寄せる想定。
"""
import urllib.parse

from ..schema import Paper
from ..util import http_get

ENDPOINT = "https://dblp.org/search/publ/api"


def search(keywords, limit=25):
    q = urllib.parse.urlencode(
        {"q": " ".join(keywords), "format": "json", "h": min(limit, 100)}
    )
    data = http_get(ENDPOINT + "?" + q, timeout=40, min_interval=1.0)
    hits = (((data or {}).get("result") or {}).get("hits") or {}).get("hit") or []
    out = []
    for h in hits:
        info = h.get("info", {})
        authors_field = (info.get("authors") or {}).get("author") or []
        if isinstance(authors_field, dict):
            authors_field = [authors_field]
        authors = [a.get("text") if isinstance(a, dict) else a for a in authors_field]
        out.append(
            Paper(
                source="dblp",
                title=info.get("title") or "",
                abstract="",
                authors=[a for a in authors if a],
                published=str(info.get("year") or ""),
                venue=info.get("venue") or "",
                url=info.get("ee") or info.get("url") or "",
                doi=info.get("doi") or "",
            )
        )
    return out

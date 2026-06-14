"""Semantic Scholar Graph API（会議録の abstract に強い）。

無鍵だとレート制限が厳しい（429 が出やすい）。失敗は呼び出し側で握りつぶす設計。
環境変数 S2_API_KEY があればヘッダに付ける。
"""
import os
import time
import urllib.error
import urllib.parse

from ..schema import Paper
from ..util import http_get

ENDPOINT = "https://api.semanticscholar.org/graph/v1/paper/search"
FIELDS = "title,abstract,year,publicationDate,venue,authors,externalIds,url,openAccessPdf,citationCount"


def search(keywords, limit=25, mode="recent"):
    q = urllib.parse.urlencode(
        {"query": " ".join(keywords), "limit": min(limit, 100), "fields": FIELDS}
    )
    headers = {}
    if os.environ.get("S2_API_KEY"):
        headers["x-api-key"] = os.environ["S2_API_KEY"]
    # S2 は一時的に 429 を返すことがある（"wait and try again"）→ 指数バックオフで数回リトライ
    data = None
    for attempt in range(3):
        try:
            data = http_get(ENDPOINT + "?" + q, headers=headers, timeout=40, min_interval=1.2)
            break
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:
                time.sleep(3 * (attempt + 1))  # 3s, 6s
            else:
                raise
    out = []
    for p in data.get("data") or []:
        ext = p.get("externalIds") or {}
        published = p.get("publicationDate") or (str(p.get("year")) if p.get("year") else "")
        out.append(
            Paper(
                source="semanticscholar",
                title=p.get("title") or "",
                abstract=p.get("abstract") or "",
                authors=[a.get("name") for a in (p.get("authors") or []) if a.get("name")],
                published=published,
                venue=p.get("venue") or "",
                url=p.get("url", ""),
                pdf_url=(p.get("openAccessPdf") or {}).get("url", "") or "",
                arxiv_id=ext.get("ArXiv", "") or "",
                doi=ext.get("DOI", "") or "",
                citations=int(p.get("citationCount") or 0),
            )
        )
    if mode == "important":
        out.sort(key=lambda p: p.citations, reverse=True)
    return out

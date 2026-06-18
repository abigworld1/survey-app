"""arXiv Atom API。作法: 説明的な User-Agent + 約3秒間隔。"""
import re
import urllib.parse
import xml.etree.ElementTree as ET

from ..schema import Paper
from ..util import http_get

ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"
ENDPOINT = "https://export.arxiv.org/api/query"


def _build_query(keywords):
    terms = ['all:"%s"' % k.replace('"', "").strip() for k in keywords if k.strip()]
    return " OR ".join(terms) if terms else "all:multi-agent path finding"


def fetch_meta(arxiv_id):
    """arXiv ID 単体のメタデータ(Paper)を取得。失敗時 None。"""
    try:
        q = urllib.parse.urlencode({"id_list": arxiv_id, "max_results": 1})
        xml = http_get(ENDPOINT + "?" + q, timeout=30, min_interval=3.0, expect="text")
        e = ET.fromstring(xml).find(ATOM + "entry")
        if e is None:
            return None
        authors = [a.findtext(ATOM + "name") for a in e.findall(ATOM + "author")]
        return Paper(
            source="arxiv",
            title=" ".join((e.findtext(ATOM + "title") or "").split()),
            abstract=(e.findtext(ATOM + "summary") or "").strip(),
            authors=[a for a in authors if a],
            published=(e.findtext(ATOM + "published") or "")[:10],
            venue=e.findtext(ARXIV + "journal_ref") or "",
            url=(e.findtext(ATOM + "id") or "").strip(),
            arxiv_id=arxiv_id,
            doi=e.findtext(ARXIV + "doi") or "",
        )
    except Exception:
        return None


def search(keywords, limit=25, mode="recent"):
    if mode == "important":
        return []
    q = urllib.parse.urlencode(
        {
            "search_query": _build_query(keywords),
            "sortBy": "submittedDate",
            "sortOrder": "descending",
            "start": 0,
            "max_results": limit,
        }
    )
    xml = http_get(ENDPOINT + "?" + q, timeout=40, min_interval=3.0, expect="text")
    root = ET.fromstring(xml)
    out = []
    for e in root.findall(ATOM + "entry"):
        title = " ".join((e.findtext(ATOM + "title") or "").split())
        summary = (e.findtext(ATOM + "summary") or "").strip()
        published = (e.findtext(ATOM + "published") or "")[:10]
        id_url = (e.findtext(ATOM + "id") or "").strip()
        arxiv_id = re.sub(r"v\d+$", "", id_url.rsplit("/abs/", 1)[-1])
        authors = [a.findtext(ATOM + "name") for a in e.findall(ATOM + "author")]
        doi = e.findtext(ARXIV + "doi") or ""
        venue = e.findtext(ARXIV + "journal_ref") or ""
        pdf = ""
        for link in e.findall(ATOM + "link"):
            if link.get("title") == "pdf":
                pdf = link.get("href", "")
        out.append(
            Paper(
                source="arxiv",
                title=title,
                abstract=summary,
                authors=[a for a in authors if a],
                published=published,
                venue=venue,
                url=id_url,
                pdf_url=pdf,
                arxiv_id=arxiv_id,
                doi=doi,
            )
        )
    return out

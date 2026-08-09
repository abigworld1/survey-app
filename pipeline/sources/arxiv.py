"""arXiv Atom API。作法: 説明的な User-Agent + 約3秒間隔。"""
import datetime
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

from ..schema import Paper
from ..util import http_get

ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"
ENDPOINT = "https://export.arxiv.org/api/query"
SEARCH_ENDPOINT = "https://arxiv.org/search/"
SEARCH_RETRY_DELAYS = (10, 30)


def _build_query(keywords):
    terms = ['all:"%s"' % k.replace('"', "").strip() for k in keywords if k.strip()]
    return " OR ".join(terms) if terms else "all:multi-agent path finding"


def _venue_from_comment(comment):
    """arXivコメント中の明示的な採択・掲載表現だけからvenueを得る。"""
    text = " ".join(str(comment or "").split())
    match = re.search(
        r"(?:accepted\s+(?:at|to|for)|to\s+appear\s+(?:at|in)|published\s+(?:at|in))"
        r"\s*[:：]?\s*(.+?)(?:[.;]|$)",
        text,
        re.I,
    )
    return match.group(1).strip(" ,") if match else ""


def fetch_meta(arxiv_id):
    """arXiv ID 単体のメタデータ(Paper)を取得。失敗時 None。"""
    try:
        q = urllib.parse.urlencode({"id_list": arxiv_id, "max_results": 1})
        xml = http_get(ENDPOINT + "?" + q, timeout=30, min_interval=3.0, expect="text")
        e = ET.fromstring(xml).find(ATOM + "entry")
        if e is None:
            return None
        authors = [a.findtext(ATOM + "name") for a in e.findall(ATOM + "author")]
        comment = e.findtext(ARXIV + "comment") or ""
        return Paper(
            source="arxiv",
            title=" ".join((e.findtext(ATOM + "title") or "").split()),
            abstract=(e.findtext(ATOM + "summary") or "").strip(),
            authors=[a for a in authors if a],
            published=(e.findtext(ATOM + "published") or "")[:10],
            venue=e.findtext(ARXIV + "journal_ref") or _venue_from_comment(comment),
            url=(e.findtext(ATOM + "id") or "").strip(),
            arxiv_id=arxiv_id,
            doi=e.findtext(ARXIV + "doi") or "",
        )
    except Exception:
        return None


def _search_once(keywords, limit, mode="recent"):
    q = urllib.parse.urlencode(
        {
            "search_query": _build_query(keywords),
            "sortBy": "relevance" if mode == "important" else "submittedDate",
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
        comment = e.findtext(ARXIV + "comment") or ""
        venue = e.findtext(ARXIV + "journal_ref") or _venue_from_comment(comment)
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


class _SearchResultParser(HTMLParser):
    """arXiv公式検索HTMLから、検索結果に含まれるメタデータだけを抽出する。"""

    def __init__(self):
        super().__init__()
        self.results = []
        self.current = None
        self.depth = 0
        self.title_depth = None
        self.authors_depth = None
        self.abstract_depth = None
        self.meta_depth = None
        self.comment_depth = None

    @staticmethod
    def _classes(attrs):
        values = dict(attrs).get("class", "")
        return set(values.split())

    def handle_starttag(self, tag, attrs):
        self.depth += 1
        classes = self._classes(attrs)
        attr_map = dict(attrs)
        if tag == "li" and "arxiv-result" in classes:
            self.current = {
                "arxiv_id": "",
                "title": [],
                "authors": [],
                "abstract": [],
                "meta": [],
                "comments": [],
            }
        if self.current is None:
            return
        if tag == "a" and not self.current["arxiv_id"]:
            href = attr_map.get("href", "")
            match = re.search(r"arxiv\.org/abs/([^?#]+)", href)
            if match:
                self.current["arxiv_id"] = re.sub(r"v\d+$", "", match.group(1))
        if tag == "p" and "title" in classes:
            self.title_depth = self.depth
        elif tag == "p" and "authors" in classes:
            self.authors_depth = self.depth
        elif tag == "span" and "abstract-full" in classes:
            self.abstract_depth = self.depth
        elif tag == "p" and "comments" in classes:
            self.comment_depth = self.depth
        elif tag == "p" and "is-size-7" in classes and self.meta_depth is None:
            self.meta_depth = self.depth

    def handle_endtag(self, tag):
        if self.current is not None:
            if self.title_depth == self.depth:
                self.title_depth = None
            if self.authors_depth == self.depth:
                self.authors_depth = None
            if self.abstract_depth == self.depth:
                self.abstract_depth = None
            if self.comment_depth == self.depth:
                self.comment_depth = None
            if self.meta_depth == self.depth:
                self.meta_depth = None
            if tag == "li":
                self.results.append(self.current)
                self.current = None
        self.depth = max(0, self.depth - 1)

    def handle_data(self, data):
        if self.current is None:
            return
        text = data.strip()
        if not text:
            return
        if self.title_depth is not None:
            self.current["title"].append(text)
        if self.authors_depth is not None:
            self.current["authors"].append(text)
        if self.abstract_depth is not None:
            self.current["abstract"].append(text)
        if self.meta_depth is not None:
            self.current["meta"].append(text)
        if self.comment_depth is not None:
            self.current["comments"].append(text)


def _parse_search_date(text):
    match = re.search(r"Submitted\s+(\d{1,2}\s+[A-Za-z]+,\s+\d{4})", text or "")
    if not match:
        return ""
    try:
        return datetime.datetime.strptime(match.group(1), "%d %B, %Y").date().isoformat()
    except ValueError:
        return ""


def _search_html_once(keywords, limit, mode="recent"):
    """Atom API障害時に、arXiv公式検索ページを代替ソースとして使う。"""
    out = []
    seen_ids = set()
    terms = [str(term).replace('"', "").strip() for term in keywords]
    terms = [term for term in terms if term] or ["multi-agent path finding"]
    target_per_query = max(1, (limit + len(terms) - 1) // len(terms))
    page_size = next(
        size for size in (25, 50, 100, 200) if size >= min(target_per_query, 200)
    )
    last_error = None
    for term in terms:
        params = {
            "query": f'"{term}"',
            "searchtype": "all",
            "abstracts": "show",
            "order": "" if mode == "important" else "-announced_date_first",
            "size": page_size,
        }
        url = SEARCH_ENDPOINT + "?" + urllib.parse.urlencode(params)
        try:
            html = http_get(url, timeout=30, min_interval=3.0, expect="text")
        except Exception as e:
            last_error = e
            continue
        parser = _SearchResultParser()
        parser.feed(html)
        for item in parser.results:
            arxiv_id = item["arxiv_id"]
            title = " ".join(item["title"])
            abstract = " ".join(item["abstract"])
            authors_text = " ".join(item["authors"])
            authors_text = re.sub(r"^Authors:\s*", "", authors_text, flags=re.I)
            authors = [part.strip() for part in authors_text.split(",") if part.strip()]
            abstract = re.sub(r"\s*[△▽]?\s*(?:More|Less)\s*$", "", abstract).strip()
            if not arxiv_id or not title or arxiv_id in seen_ids:
                continue
            seen_ids.add(arxiv_id)
            out.append(
                Paper(
                    source="arxiv",
                    title=" ".join(title.split()),
                    abstract=abstract,
                    authors=authors,
                    published=_parse_search_date(" ".join(item["meta"])),
                    venue=_venue_from_comment(" ".join(item["comments"])),
                    url=f"https://arxiv.org/abs/{arxiv_id}",
                    pdf_url=f"https://arxiv.org/pdf/{arxiv_id}",
                    arxiv_id=arxiv_id,
                )
            )
            if len(out) >= limit:
                return out
    if not out and last_error:
        raise last_error
    return out[:limit]


def search(keywords, limit=25, mode="recent", retries=True):
    last_error = None
    delays = SEARCH_RETRY_DELAYS if retries else ()
    attempts = len(delays) + 1
    for attempt in range(attempts):
        try:
            out = _search_once(keywords, limit, mode=mode)
            if out:
                if attempt:
                    print(f"    [recovered] arXiv検索: {attempt + 1}回目で取得")
                return out
            last_error = RuntimeError("arXiv API returned an empty feed")
        except Exception as e:
            last_error = e

        try:
            out = _search_html_once(keywords, limit, mode=mode)
            if out:
                print("    [fallback] arXiv公式検索HTMLから取得")
                return out
        except Exception as e:
            last_error = e

        if attempt < len(delays):
            delay = delays[attempt]
            print(
                f"    [retry] arXiv検索が空または失敗: {last_error!r}、"
                f"{delay}秒待機 ({attempt + 1}/{attempts})"
            )
            time.sleep(delay)

    raise RuntimeError(
        f"arXiv検索を{attempts}回試しましたが取得できませんでした: {last_error!r}"
    ) from last_error

"""採択会議・掲載ジャーナルを複数の書誌情報源から補完する。"""

import difflib
import re
import urllib.parse

from .schema import Paper, normalize_title
from .sources import dblp, openalex, semanticscholar
from .util import http_get


_PREPRINT_VENUES = {
    "arxiv",
    "arxivorg",
    "arxivcornelluniversity",
    "corr",
    "biorxiv",
    "medrxiv",
    "researchsquare",
    "ssrn",
}
_CONFERENCE_ACRONYMS = {
    "aaai", "aamas", "acl", "aistats", "cikm", "coling", "corl", "cpaior",
    "cvpr", "ecai", "eccv", "emnlp", "icaps", "iccv", "icdar", "iclr",
    "icml", "icra", "ijcai", "iros", "kdd", "naacl", "neurips", "rss",
    "sigir", "socs", "uai", "wsdm", "www",
}


def _usable_venue(value):
    venue = re.sub(r"\s+", " ", str(value or "")).strip()
    normalized = re.sub(r"[\s._()/-]+", "", venue).lower()
    return bool(
        venue
        and venue.lower() not in {"unknown", "n/a", "none", "-", "未取得"}
        and normalized not in _PREPRINT_VENUES
    )


def _is_conference_venue(value):
    venue = re.sub(r"\s+", " ", str(value or "")).strip()
    lowered = venue.lower()
    if re.search(r"\b(conference|symposium|workshop|congress|proceedings)\b", lowered):
        return True
    tokens = set(re.findall(r"[a-z0-9]+", lowered))
    return bool(tokens & _CONFERENCE_ACRONYMS)


def venue_with_year(value, published=""):
    """会議名に年が無い場合だけ、論文の公開年を表示用に補う。"""
    venue = re.sub(r"\s+", " ", str(value or "")).strip()
    if not _usable_venue(venue) or not _is_conference_venue(venue):
        return venue
    if re.search(r"\b(?:19|20)\d{2}\b", venue):
        return venue
    year_match = re.search(r"\b((?:19|20)\d{2})\b", str(published or ""))
    return f"{venue} {year_match.group(1)}" if year_match else venue


def _title_similarity(left, right):
    left = normalize_title(left)
    right = normalize_title(right)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    return difflib.SequenceMatcher(None, left, right).ratio()


def _crossref_lookup(paper):
    if not paper.doi:
        return None
    doi = urllib.parse.quote(str(paper.doi).strip(), safe="")
    data = http_get(
        f"https://api.crossref.org/works/{doi}", timeout=35, min_interval=0.5
    )
    item = (data or {}).get("message") or {}
    titles = item.get("title") or []
    title = titles[0] if titles else ""
    event = item.get("event") or {}
    short_container = item.get("short-container-title") or []
    container = item.get("container-title") or []
    venue = (
        event.get("name")
        or (short_container[0] if short_container else "")
        or (container[0] if container else "")
    )
    date_parts = (
        ((item.get("published") or {}).get("date-parts") or [[]])[0]
        or ((item.get("issued") or {}).get("date-parts") or [[]])[0]
    )
    published = "-".join(str(part) for part in date_parts) if date_parts else ""
    authors = []
    for author in item.get("author") or []:
        name = " ".join(
            part for part in (author.get("given", ""), author.get("family", "")) if part
        )
        if name:
            authors.append(name)
    return Paper(
        source="crossref",
        title=title,
        authors=authors,
        published=published,
        venue=venue,
        url=item.get("URL") or "",
        doi=item.get("DOI") or paper.doi,
    )


def _best_title_match(candidates, title, threshold=0.84):
    matches = [
        candidate
        for candidate in candidates or []
        if _title_similarity(candidate.title, title) >= threshold
    ]
    if not matches:
        return None
    return max(
        matches,
        key=lambda candidate: (
            _title_similarity(candidate.title, title),
            1 if _usable_venue(candidate.venue) else 0,
            int(candidate.citations or 0),
        ),
    )


def _dblp_lookup(paper):
    return _best_title_match(dblp.search([paper.title], limit=5), paper.title)


def _openalex_lookup(paper):
    return _best_title_match(openalex.search([paper.title], limit=5), paper.title)


def _semantic_scholar_lookup(paper):
    return _best_title_match(
        semanticscholar.search([paper.title], limit=5), paper.title
    )


def _merge_metadata(paper, candidate):
    if not candidate or _title_similarity(candidate.title, paper.title) < 0.84:
        return False
    if _usable_venue(candidate.venue):
        paper.venue = candidate.venue
    for attr in ("doi", "arxiv_id", "pdf_url", "url", "published", "abstract"):
        if not getattr(paper, attr, "") and getattr(candidate, attr, ""):
            setattr(paper, attr, getattr(candidate, attr))
    if not paper.authors and candidate.authors:
        paper.authors = candidate.authors
    try:
        paper.citations = max(int(paper.citations or 0), int(candidate.citations or 0))
    except (TypeError, ValueError):
        pass
    return _usable_venue(paper.venue)


def enrich_venue(paper):
    """採択先が無いPaperを照合し、得られた書誌情報をその場で補完する。"""
    if _usable_venue(paper.venue):
        paper.venue = venue_with_year(paper.venue, paper.published)
        return paper
    paper.venue = ""
    lookups = []
    if paper.doi:
        lookups.append(("Crossref", _crossref_lookup))
    lookups.extend(
        (
            ("DBLP", _dblp_lookup),
            ("OpenAlex", _openalex_lookup),
            ("Semantic Scholar", _semantic_scholar_lookup),
        )
    )
    checked = []
    for label, lookup in lookups:
        checked.append(label)
        try:
            candidate = lookup(paper)
        except Exception as exc:
            print(f"      [warn] 採択先照合失敗 {label}: {exc!r}")
            continue
        if _merge_metadata(paper, candidate):
            paper.venue = venue_with_year(paper.venue, paper.published)
            print(f"      採択先: {paper.venue} ({label}で確認)")
            return paper
    print(f"      採択先: 未取得（{' / '.join(checked)}を確認）")
    return paper

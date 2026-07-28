"""論文ソースのレジストリ。各アダプタは search(keywords, limit) -> [Paper] を実装。"""
from . import arxiv, dblp, openalex, semanticscholar

_SOURCES = {
    "arxiv": arxiv.search,
    "openalex": openalex.search,
    "semanticscholar": semanticscholar.search,
    "dblp": dblp.search,
}


def available():
    return list(_SOURCES)


def search_source(name, keywords, limit, mode="recent"):
    fn = _SOURCES.get(name)
    if not fn:
        raise ValueError(f"未知のソース: {name}")
    return fn(keywords, limit, mode=mode)

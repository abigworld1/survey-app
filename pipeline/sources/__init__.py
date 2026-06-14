"""論文ソースのレジストリ。各アダプタは search(keywords, limit) -> [Paper] を実装。

1つのソースが落ちても全体は止めない（ログを出して空リストを返す）。
"""
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
        print(f"  [warn] 未知のソース: {name}")
        return []
    try:
        return fn(keywords, limit, mode=mode)
    except Exception as e:  # ネットワーク/パース失敗は致命にしない
        print(f"  [warn] ソース '{name}' 取得失敗: {e!r}")
        return []

"""論文の共通スキーマ。各ソースのアダプタはこの Paper に正規化する。"""
import re
from dataclasses import dataclass, field

from .util import sha1


@dataclass
class Paper:
    source: str                       # 取得元（arxiv / openalex / semanticscholar / dblp）
    title: str
    abstract: str = ""
    authors: list = field(default_factory=list)
    published: str = ""               # 可能なら YYYY-MM-DD（無ければ年など）
    venue: str = ""
    url: str = ""                     # 原典ランディングURL
    pdf_url: str = ""
    arxiv_id: str = ""
    doi: str = ""

    def key(self):
        """名寄せ用の安定キー。DOI > arXiv ID > 正規化タイトル の優先順。"""
        if self.doi:
            return "doi:" + self.doi.lower()
        if self.arxiv_id:
            return "arxiv:" + self.arxiv_id.lower()
        return "title:" + normalize_title(self.title)

    def paper_id(self):
        """出力ファイル名に使う安定ID（後段で slugify する）。"""
        if self.arxiv_id:
            return self.arxiv_id
        if self.doi:
            return "doi-" + sha1(self.doi.lower())[:12]
        return "t-" + sha1(normalize_title(self.title))[:12]


def normalize_title(t):
    t = (t or "").lower()
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()

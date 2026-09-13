"""Build bounded paper evidence without losing late results/conclusions."""
import os
import re

from .fulltext import _DENY_HEADINGS, _TextExtractor, _strip_references


def _evidence_excerpt(text, limit):
    if limit <= 0:
        return ""
    value = (text or "").strip()
    if len(value) <= limit:
        return value
    head_size = max(0, int(limit * 0.40))
    tail_size = max(0, int(limit * 0.20))
    middle_budget = max(0, limit - head_size - tail_size - 30)
    middle = value[head_size:-tail_size]
    evidence_sentences = []
    evidence_pattern = re.compile(
        r"\d|%|percent|result|outperform|improv|success|runtime|latency|"
        r"limitation|failure|ablation|比較|結果|成功率|実行時間|限界|失敗",
        re.I,
    )
    used = 0
    for sentence in re.split(r"(?<=[.!?。！？])\s+", middle):
        if not evidence_pattern.search(sentence):
            continue
        sentence = sentence.strip()
        if not sentence or used + len(sentence) + 1 > middle_budget:
            continue
        evidence_sentences.append(sentence)
        used += len(sentence) + 1
    selected = " ".join(evidence_sentences)
    if not selected:
        selected = middle[:middle_budget]
    return (
        value[:head_size]
        + "\n[中間部から数値・結果・限界に関する記述を抜粋]\n"
        + selected
        + "\n[末尾部]\n"
        + value[-tail_size:]
    )[:limit]


def _priority(heading):
    if re.search(r"method|approach|algorithm|experiment|evaluat|result|conclu|limitation|discussion", heading, re.I):
        return 3
    if re.search(r"abstract|intro|related|background|problem|preliminar", heading, re.I):
        return 2
    return 1


def build_evidence(paper, sections, max_chars=None):
    budget = max(4000, min(int(max_chars or os.environ.get("COPILOT_CONTEXT_CHARS", "24000")), 32000))
    prefix = (
        f"Title: {paper.title[:1000]}\nAuthors: {', '.join(paper.authors[:12])[:1000]}\n"
        f"Source URL: {(paper.url or paper.pdf_url)[:1000]}\n"
        f"Abstract: {paper.abstract[:4000]}\n"
    )[:min(6000, budget // 3)]
    cleaned, fingerprints = [], set()
    for heading, body in sections:
        if any(word in heading.lower() for word in _DENY_HEADINGS):
            continue
        if re.search(r"<(?:p|div|nav|script)\b", body, re.I):
            parser = _TextExtractor()
            parser.feed(body)
            body = "\n".join(parser.parts)
        body = _strip_references(body)
        paragraphs = []
        for paragraph in re.split(r"\n+|(?<=[.!?])\s+", body):
            paragraph = " ".join(paragraph.split())
            fingerprint = paragraph.casefold()
            if not paragraph or fingerprint in fingerprints:
                continue
            fingerprints.add(fingerprint)
            paragraphs.append(paragraph)
        if paragraphs:
            cleaned.append((heading[:120], " ".join(paragraphs)))
    # Rank before applying a section cap so late conclusions survive. Keep the
    # final body chunk too when PDF heading detection fell back to plain chunks.
    if len(cleaned) > 24 and all(h.startswith("本文 (") for h, _ in cleaned):
        # Without usable headings, sample across the paper, not just its start.
        cleaned = [cleaned[round(i * (len(cleaned) - 1) / 23)] for i in range(24)]
    elif len(cleaned) > 24:
        tail = cleaned[-1]
        cleaned = sorted(cleaned[:-1], key=lambda item: _priority(item[0]), reverse=True)[:23] + [tail]
    overhead = sum(len(h) + 8 for h, _ in cleaned)
    remaining = max(0, budget - len(prefix) - overhead)
    weights = sum(_priority(h) for h, _ in cleaned) or 1
    chunks = [prefix]
    for heading, body in cleaned:
        allocation = int(remaining * _priority(heading) / weights)
        chunks.append(f"\n## {heading}\n{_evidence_excerpt(body, allocation)}")
    evidence = "".join(chunks)[:budget]
    # Unicode-heavy papers also need a byte cap for subprocess argv/review.
    if len(evidence.encode("utf-8")) > 60000:
        return build_evidence(paper, sections, max_chars=max(4000, budget * 60000 // len(evidence.encode("utf-8")) - 100))
    return evidence

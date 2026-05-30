from __future__ import annotations

import html
import re
from urllib.parse import urlencode
from urllib.request import Request

from .http_client import open_url
from .models import AnswerCandidate, PageQuestion
from .normalize import normalize_text


_TAG_RE = re.compile(r"<[^>]+>")


def web_answer(question: PageQuestion, timeout: float = 8.0) -> AnswerCandidate | None:
    if not question.options:
        return None
    query = f"{question.text} {' '.join(question.options)}"
    snippets = _search_snippets(query, timeout=timeout)
    if not snippets:
        return None
    corpus = normalize_text(" ".join(snippets))
    scored_options = []
    for option in question.options:
        normalized_option = normalize_text(option)
        exact_score = 0.92 if normalized_option and normalized_option in corpus else 0.0
        overlap_score = _term_overlap(normalized_option, corpus)
        scored_options.append((option, max(exact_score, overlap_score)))
    scored_options.sort(key=lambda item: item[1], reverse=True)
    if not scored_options or scored_options[0][1] < 0.45:
        return None
    return AnswerCandidate(
        answers=[scored_options[0][0]],
        confidence=min(scored_options[0][1], 0.72),
        source="web-search",
        excerpt=" ".join(snippets[:3])[:1000],
    )


def _search_snippets(query: str, timeout: float) -> list[str]:
    params = urlencode({"q": query})
    request = Request(
        f"https://html.duckduckgo.com/html/?{params}",
        headers={
            "User-Agent": "Mozilla/5.0 TestSolver/0.1",
            "Accept-Language": "ru,en;q=0.8",
        },
    )
    with open_url(request, timeout=timeout) as response:
        body = response.read().decode("utf-8", errors="ignore")
    snippets = re.findall(r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>', body, flags=re.DOTALL)
    if not snippets:
        snippets = re.findall(r'<div[^>]+class="result__snippet"[^>]*>(.*?)</div>', body, flags=re.DOTALL)
    return [_clean_html(snippet) for snippet in snippets if _clean_html(snippet)]


def _clean_html(value: str) -> str:
    without_tags = _TAG_RE.sub(" ", value)
    return re.sub(r"\s+", " ", html.unescape(without_tags)).strip()


def _term_overlap(left: str, right: str) -> float:
    left_terms = {term for term in left.split() if len(term) >= 3}
    if not left_terms:
        return 0.0
    right_terms = set(right.split())
    return len(left_terms & right_terms) / len(left_terms)

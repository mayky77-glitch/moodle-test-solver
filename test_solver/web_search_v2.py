from __future__ import annotations

import hashlib
import html
import contextlib
import io
import json
import re
import time
from dataclasses import dataclass
from io import BytesIO
from urllib.parse import parse_qs, unquote, urlencode, urlparse
from urllib.request import Request

from pypdf import PdfReader

from .http_client import open_url
from .models import AnswerCandidate, PageQuestion
from .normalize import normalize_text, question_key
from .storage import QuestionStore


_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_RESULT_LINK_RE = re.compile(r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"', re.IGNORECASE)
_HREF_RE = re.compile(r'href="([^"]+)"', re.IGNORECASE)
_DDG_RESULT_RE = re.compile(
    r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="(?P<url>[^"]+)"[^>]*>(?P<title>.*?)</a>.*?'
    r'(?:<a[^>]+class="[^"]*result__snippet[^"]*"[^>]*>|<div[^>]+class="[^"]*result__snippet[^"]*"[^>]*>)(?P<snippet>.*?)(?:</a>|</div>)',
    re.DOTALL | re.IGNORECASE,
)
_BING_RESULT_RE = re.compile(
    r'<li[^>]+class="[^"]*b_algo[^"]*"[^>]*>.*?<a[^>]+href="(?P<url>[^"]+)"[^>]*>(?P<title>.*?)</a>.*?<p[^>]*>(?P<snippet>.*?)</p>',
    re.DOTALL | re.IGNORECASE,
)
_NEGATION_TERMS = {"не", "нет", "нельзя", "невозможно", "отсутствует", "исключает", "кроме"}
_LOW_VALUE_DOMAINS = ("otvet.mail.ru", "znanija.com", "studfile.net", "referat", "allbest")
_HIGH_VALUE_DOMAINS = ("edu.", ".edu", "wikipedia.org", "docs.", "consultant.ru", "garant.ru")


@dataclass(frozen=True)
class WebDocument:
    url: str
    title: str
    text: str
    source_weight: float


def web_answer_v2(
    question: PageQuestion,
    store: QuestionStore,
    timeout: float = 8.0,
    max_pages: int = 5,
    cache_ttl: int = 86400,
    total_timeout: float | None = None,
) -> AnswerCandidate | None:
    if not question.options:
        return None

    cache_key = _cache_key(question)
    cached = store.get_web_cache(cache_key, cache_ttl) if cache_ttl > 0 else None
    if cached:
        return _candidate_from_payload(cached)

    documents = search_documents(question, timeout=timeout, max_pages=max_pages, total_timeout=total_timeout)
    candidate = answer_from_documents(question, documents)
    if candidate and candidate.answers:
        store.set_web_cache(
            cache_key,
            {
                "answers": candidate.answers,
                "confidence": candidate.confidence,
                "source": candidate.source,
                "excerpt": candidate.excerpt,
            },
        )
    return candidate


def search_documents(
    question: PageQuestion,
    timeout: float = 8.0,
    max_pages: int = 5,
    total_timeout: float | None = None,
) -> list[WebDocument]:
    deadline = time.monotonic() + total_timeout if total_timeout and total_timeout > 0 else None
    urls: list[str] = []
    documents: list[WebDocument] = []
    for query in build_queries(question):
        request_timeout = _remaining_timeout(deadline, timeout)
        try:
            search_documents_from_results, found_urls = search_result_documents(query, timeout=request_timeout)
        except TimeoutError:
            if deadline and time.monotonic() >= deadline:
                raise
            search_documents_from_results, found_urls = [], []
        except Exception:
            search_documents_from_results, found_urls = [], []
        existing_document_urls = {document.url for document in documents}
        for document in search_documents_from_results:
            if document.url not in existing_document_urls:
                documents.append(document)
                existing_document_urls.add(document.url)
            if len(documents) >= max_pages:
                return documents
        for url in found_urls:
            if url not in urls:
                urls.append(url)
            if len(urls) >= max_pages * 2:
                break
            _remaining_timeout(deadline, timeout)
        if len(urls) >= max_pages * 2:
            break
        _remaining_timeout(deadline, timeout)

    for url in urls:
        if any(document.url == url for document in documents):
            continue
        request_timeout = _remaining_timeout(deadline, timeout)
        document = fetch_document(url, timeout=request_timeout)
        if document and len(normalize_text(document.text)) > 120:
            documents.append(document)
        if len(documents) >= max_pages:
            break
        _remaining_timeout(deadline, timeout)
    return documents


def answer_from_documents(question: PageQuestion, documents: list[WebDocument]) -> AnswerCandidate | None:
    if not documents:
        return None

    evidence_chunks = _rank_evidence(question, documents, limit=8)
    if not evidence_chunks:
        return None
    boolean_candidate = _boolean_candidate(question, evidence_chunks)
    if boolean_candidate:
        return boolean_candidate

    option_scores: dict[str, float] = {option: 0.0 for option in question.options}
    option_evidence: dict[str, list[str]] = {option: [] for option in question.options}
    for chunk, weight, url in evidence_chunks:
        normalized_chunk = normalize_text(chunk)
        for option in question.options:
            normalized_option = normalize_text(option)
            if not normalized_option:
                continue
            exact_score = 1.0 if normalized_option in normalized_chunk else 0.0
            term_score = _term_overlap(normalized_option, normalized_chunk)
            phrase_score = _phrase_overlap(normalized_option, normalized_chunk)
            negation_penalty = 0.55 if _negation_conflict(normalized_option, normalized_chunk) else 1.0
            score = max(exact_score, term_score * 0.78, phrase_score * 0.9) * weight * negation_penalty
            if score > 0:
                option_scores[option] += score
                option_evidence[option].append(f"{url}\n{chunk}")

    ranked = sorted(option_scores.items(), key=lambda item: item[1], reverse=True)
    if not ranked or ranked[0][1] <= 0:
        return None

    best_answer, best_score = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else 0.0
    margin = best_score - second_score
    if best_score < 0.42 or margin < 0.08:
        return None

    confidence = min(0.86, max(0.45, best_score / 2.5 + min(margin, 0.4)))
    evidence = option_evidence[best_answer][:3]
    excerpt = "\n\n".join(evidence)[:1400]
    return AnswerCandidate(
        answers=[best_answer],
        confidence=confidence,
        source="web-search-v2",
        excerpt=excerpt,
    )


def build_queries(question: PageQuestion) -> list[str]:
    question_text = _strip_noise(question.text)
    option_text = " ".join(question.options[:4])
    terms = " ".join(_keywords(question_text, limit=10))
    queries = [
        f'"{question_text}"',
        question_text,
        f"{question_text} {option_text}",
        terms,
    ]
    for option in question.options[:3]:
        queries.append(f"{terms} {option}")
    return [query for query in dict.fromkeys(query.strip() for query in queries) if query]


def search_urls(query: str, timeout: float = 8.0) -> list[str]:
    _, urls = search_result_documents(query, timeout=timeout)
    return urls


def search_result_documents(query: str, timeout: float = 8.0) -> tuple[list[WebDocument], list[str]]:
    params = urlencode({"q": query})
    urls = []
    documents: list[WebDocument] = []
    for search_url in [
        f"https://html.duckduckgo.com/html/?{params}",
        f"https://www.bing.com/search?{params}",
    ]:
        request = Request(
            search_url,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 TestSolver/0.2",
                "Accept-Language": "ru,en;q=0.8",
            },
        )
        try:
            with open_url(request, timeout=timeout) as response:
                body = response.read().decode("utf-8", errors="ignore")
        except Exception:
            continue
        documents.extend(_documents_from_search_body(body))
        raw_urls = _RESULT_LINK_RE.findall(body)
        if not raw_urls:
            raw_urls = _HREF_RE.findall(body)
        for raw_url in raw_urls:
            url = _unwrap_search_url(html.unescape(raw_url))
            if not url.startswith(("http://", "https://")):
                continue
            domain = urlparse(url).netloc.casefold()
            if any(blocked in domain for blocked in ("duckduckgo.com", "bing.com", "microsoft.com")):
                continue
            if url not in urls:
                urls.append(url)
        if urls:
            break
    return documents[:5], urls[:10]


def fetch_document(url: str, timeout: float = 8.0) -> WebDocument | None:
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 TestSolver/0.2",
            "Accept-Language": "ru,en;q=0.8",
        },
    )
    try:
        with open_url(request, timeout=timeout) as response:
            content_type = response.headers.get("Content-Type", "")
            raw = response.read(2_000_000)
    except Exception:
        return None

    if "pdf" in content_type.lower() or url.lower().split("?")[0].endswith(".pdf"):
        text = _pdf_text(raw)
        title = url.rsplit("/", 1)[-1]
    else:
        body = raw.decode("utf-8", errors="ignore")
        title = _title_from_html(body) or urlparse(url).netloc
        text = _html_text(body)
    if not text:
        return None
    return WebDocument(url=url, title=title, text=text, source_weight=_source_weight(url))


def _rank_evidence(question: PageQuestion, documents: list[WebDocument], limit: int) -> list[tuple[str, float, str]]:
    query_text = normalize_text(" ".join([question.text, *question.options]))
    chunks: list[tuple[str, float, str]] = []
    for document in documents:
        for chunk in _chunks(document.text):
            normalized_chunk = normalize_text(chunk)
            question_overlap = _term_overlap(normalize_text(question.text), normalized_chunk)
            option_overlap = max((_term_overlap(normalize_text(option), normalized_chunk) for option in question.options), default=0.0)
            broad_overlap = _term_overlap(query_text, normalized_chunk)
            score = (question_overlap * 0.55 + option_overlap * 0.35 + broad_overlap * 0.1) * document.source_weight
            if score > 0.05:
                chunks.append((chunk, score, document.url))
    chunks.sort(key=lambda item: item[1], reverse=True)
    return chunks[:limit]


def _candidate_from_payload(payload: dict) -> AnswerCandidate | None:
    answers = [str(answer) for answer in payload.get("answers", []) if str(answer).strip()]
    if not answers:
        return None
    return AnswerCandidate(
        answers=answers,
        confidence=float(payload.get("confidence") or 0.0),
        source=str(payload.get("source") or "web-search-v2"),
        excerpt=str(payload.get("excerpt") or ""),
    )


def _cache_key(question: PageQuestion) -> str:
    payload = json.dumps(
        {
            "version": 2,
            "key": question_key(question.text, question.options),
            "kind": question.kind,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _unwrap_search_url(url: str) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    if "uddg" in query:
        return unquote(query["uddg"][0])
    if parsed.netloc.endswith("bing.com") and "u" in query:
        return unquote(query["u"][0])
    return url


def _documents_from_search_body(body: str) -> list[WebDocument]:
    documents: list[WebDocument] = []
    for pattern in (_DDG_RESULT_RE, _BING_RESULT_RE):
        for match in pattern.finditer(body):
            document = _search_result_document(
                match.group("url") or "",
                _clean(match.group("title") or ""),
                _clean(match.group("snippet") or ""),
            )
            if document and document.url not in {existing.url for existing in documents}:
                documents.append(document)
    return documents


def _search_result_document(raw_url: str, title: str, snippet: str) -> WebDocument | None:
    url = _unwrap_search_url(html.unescape(raw_url))
    if not url.startswith(("http://", "https://")):
        return None
    domain = urlparse(url).netloc.casefold()
    if any(blocked in domain for blocked in ("duckduckgo.com", "bing.com", "microsoft.com")):
        return None
    text = _clean(" ".join([title, snippet]))
    if len(normalize_text(text)) < 40:
        return None
    return WebDocument(url=url, title=title or domain, text=text, source_weight=_source_weight(url) * 0.92)


def _html_text(body: str) -> str:
    body = _SCRIPT_RE.sub(" ", body)
    text = _TAG_RE.sub(" ", body)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _title_from_html(body: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", body, flags=re.DOTALL | re.IGNORECASE)
    return _clean(match.group(1)) if match else ""


def _pdf_text(raw: bytes) -> str:
    if not raw.lstrip().startswith(b"%PDF"):
        return ""
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            reader = PdfReader(BytesIO(raw))
            return "\n".join(page.extract_text() or "" for page in reader.pages[:8])
    except Exception:
        return ""


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub(" ", value))).strip()


def _strip_noise(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip(" .")


def _keywords(value: str, limit: int) -> list[str]:
    stop_words = {
        "какой",
        "какая",
        "какие",
        "какое",
        "что",
        "где",
        "когда",
        "после",
        "перед",
        "это",
        "для",
        "при",
        "или",
        "the",
        "and",
        "with",
    }
    terms = []
    for term in normalize_text(value).split():
        if len(term) < 3 or term in stop_words:
            continue
        terms.append(term)
    return list(dict.fromkeys(terms))[:limit]


def _chunks(text: str) -> list[str]:
    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks = []
    current: list[str] = []
    current_length = 0
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        current.append(sentence)
        current_length += len(sentence)
        if current_length >= 700:
            chunks.append(" ".join(current))
            current = []
            current_length = 0
    if current:
        chunks.append(" ".join(current))
    return chunks


def _source_weight(url: str) -> float:
    domain = urlparse(url).netloc.casefold()
    weight = 1.0
    if any(marker in domain for marker in _HIGH_VALUE_DOMAINS):
        weight += 0.2
    if any(marker in domain for marker in _LOW_VALUE_DOMAINS):
        weight -= 0.25
    if url.lower().split("?")[0].endswith(".pdf"):
        weight += 0.1
    return max(0.55, min(weight, 1.3))


def _remaining_timeout(deadline: float | None, per_request_timeout: float) -> float:
    if deadline is None:
        return per_request_timeout
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("web-search-v2 total timeout exceeded")
    return max(0.1, min(per_request_timeout, remaining))


def _term_overlap(left: str, right: str) -> float:
    left_terms = {term for term in left.split() if len(term) >= 3}
    if not left_terms:
        return 0.0
    right_terms = set(right.split())
    return len(left_terms & right_terms) / len(left_terms)


def _phrase_overlap(left: str, right: str) -> float:
    left_terms = [term for term in left.split() if len(term) >= 3]
    if len(left_terms) < 2:
        return _term_overlap(left, right)
    pairs = {" ".join(left_terms[index : index + 2]) for index in range(len(left_terms) - 1)}
    if not pairs:
        return 0.0
    return sum(1 for pair in pairs if pair in right) / len(pairs)


def _negation_conflict(option: str, chunk: str) -> bool:
    option_terms = set(option.split())
    if option_terms & _NEGATION_TERMS:
        return False
    return bool(set(chunk.split()) & _NEGATION_TERMS) and _term_overlap(option, chunk) > 0.55


def _boolean_candidate(question: PageQuestion, evidence_chunks: list[tuple[str, float, str]]) -> AnswerCandidate | None:
    normalized_question = normalize_text(question.text)
    if not any(marker in normalized_question for marker in ["является ли", "являются ли", "можно ли", "входит ли", "относится ли"]):
        return None

    yes_options = [option for option in question.options if normalize_text(option).startswith("да")]
    no_options = [option for option in question.options if normalize_text(option).startswith("нет")]
    if not yes_options or not no_options:
        return None

    corpus = normalize_text(" ".join(chunk for chunk, _, _ in evidence_chunks[:3]))
    negative_markers = [
        "не является",
        "не являются",
        "не может",
        "нельзя",
        "не входит",
        "не относится",
        "не относятся",
    ]
    if any(marker in corpus for marker in negative_markers):
        excerpt = "\n\n".join(f"{url}\n{chunk}" for chunk, _, url in evidence_chunks[:2])[:1400]
        return AnswerCandidate(
            answers=[no_options[0]],
            confidence=0.74,
            source="web-search-v2",
            excerpt=excerpt,
        )
    positive_markers = ["является", "являются", "можно", "входит", "относится", "относятся"]
    if any(marker in corpus for marker in positive_markers):
        excerpt = "\n\n".join(f"{url}\n{chunk}" for chunk, _, url in evidence_chunks[:2])[:1400]
        return AnswerCandidate(
            answers=[yes_options[0]],
            confidence=0.72,
            source="web-search-v2",
            excerpt=excerpt,
        )
    return None

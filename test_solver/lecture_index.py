from __future__ import annotations

import html
import re
from pathlib import Path

from .models import LectureHit
from .normalize import normalize_text
from .storage import QuestionStore


_WORD_RE = re.compile(r"[\wа-яёА-ЯЁ]{3,}", re.UNICODE)
_SCRIPT_STYLE_RE = re.compile(r"<(script|style|noscript)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")


def extract_text(path: str | Path) -> str:
    source = Path(path)
    suffix = source.suffix.casefold()
    if suffix in {".txt", ".md", ".csv"}:
        return source.read_text(encoding="utf-8", errors="ignore")
    if suffix in {".html", ".htm"}:
        return clean_page_text(source.read_text(encoding="utf-8", errors="ignore"))
    if suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(str(source))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    if suffix == ".docx":
        from docx import Document

        document = Document(str(source))
        return "\n".join(paragraph.text for paragraph in document.paragraphs)
    raise ValueError(f"Unsupported lecture format: {source}")


def clean_page_text(raw_html: str) -> str:
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(raw_html, "html.parser")
        for element in soup(["script", "style", "noscript"]):
            element.decompose()
        return html.unescape(soup.get_text("\n", strip=True))
    except ModuleNotFoundError:
        without_scripts = _SCRIPT_STYLE_RE.sub(" ", raw_html)
        without_tags = _TAG_RE.sub(" ", without_scripts)
        return re.sub(r"\s+", " ", html.unescape(without_tags)).strip()


def ingest_paths(store: QuestionStore, paths: list[str | Path]) -> int:
    count = 0
    for item in paths:
        path = Path(item)
        candidates = [path]
        if path.is_dir():
            candidates = [
                child
                for child in path.rglob("*")
                if child.is_file() and child.suffix.casefold() in {".txt", ".md", ".csv", ".html", ".htm", ".pdf", ".docx"}
            ]
        for candidate in candidates:
            text = extract_text(candidate)
            if normalize_text(text):
                store.add_lecture(str(candidate.resolve()), candidate.name, text)
                count += 1
    return count


class LectureIndex:
    def __init__(self, store: QuestionStore) -> None:
        self.store = store

    def search(self, query: str, options: list[str] | None = None, limit: int = 5) -> list[LectureHit]:
        query_terms = set(_WORD_RE.findall(normalize_text(query)))
        option_terms = set(_WORD_RE.findall(normalize_text(" ".join(options or []))))
        terms = query_terms | option_terms
        if not terms:
            return []

        hits: list[LectureHit] = []
        for row in self.store.lectures():
            excerpt, score, matched = _referenced_excerpt(row["text"], query, terms)
            if not matched:
                excerpt, score, matched = _best_excerpt(row["text"], terms)
            if not matched:
                continue
            hits.append(LectureHit(int(row["id"]), row["title"], excerpt, score))
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[:limit]


def _best_excerpt(text: str, terms: set[str], chunk_size: int = 900, overlap: int = 250) -> tuple[str, float, set[str]]:
    compact = re.sub(r"\s+", " ", text).strip()
    if not compact:
        return "", 0.0, set()

    best_excerpt = ""
    best_score = 0.0
    best_matched: set[str] = set()
    step = max(chunk_size - overlap, 1)
    for start in range(0, len(compact), step):
        chunk = compact[start : start + chunk_size]
        normalized_chunk = normalize_text(chunk)
        matched = {term for term in terms if term in normalized_chunk}
        if not matched:
            continue
        score = len(matched) / max(len(terms), 1)
        if "вопросы для самоконтроля" in normalized_chunk:
            score *= 0.35
        if score > best_score:
            best_excerpt = chunk
            best_score = score
            best_matched = matched
    return best_excerpt, best_score, best_matched


def _referenced_excerpt(text: str, query: str, terms: set[str], radius: int = 700) -> tuple[str, float, set[str]]:
    query_text = query.strip().rstrip("?!.")
    if len(query_text) < 20:
        return "", 0.0, set()
    pattern = r"\s+".join(re.escape(part) for part in re.split(r"\s+", query_text))
    match = re.search(pattern + r"\??\s*\((\d{1,3})\)", text, flags=re.IGNORECASE)
    if not match:
        return "", 0.0, set()

    slide_number = match.group(1)
    marker = re.search(rf"Крючков\s+А\.В\.\s+{re.escape(slide_number)}\b", text, flags=re.IGNORECASE)
    if not marker:
        return "", 0.0, set()

    start = max(marker.start() - radius, 0)
    end = min(marker.end() + radius, len(text))
    excerpt = re.sub(r"\s+", " ", text[start:end]).strip()
    matched = {term for term in terms if term in normalize_text(excerpt)}
    if not matched:
        return "", 0.0, set()
    score = min((len(matched) / max(len(terms), 1)) + 0.25, 0.95)
    return excerpt, score, matched


from __future__ import annotations

import hashlib
import re
import unicodedata


_SPACE_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\sа-яёА-ЯЁ-]", re.UNICODE)


def normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "")
    normalized = normalized.replace("\u00a0", " ")
    normalized = _PUNCT_RE.sub(" ", normalized)
    normalized = _SPACE_RE.sub(" ", normalized)
    return normalized.casefold().strip()


def question_key(question: str, options: list[str] | tuple[str, ...] = ()) -> str:
    normalized_question = normalize_text(question)
    normalized_options = sorted(normalize_text(option) for option in options if option)
    payload = "\n".join([normalized_question, *normalized_options])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


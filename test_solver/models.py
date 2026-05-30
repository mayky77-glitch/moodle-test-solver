from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


QuestionKind = Literal["single_choice", "multiple_choice", "text", "unknown"]
QuestionStatus = Literal["answered", "needs_review", "diagnosed", "failed"]


@dataclass(frozen=True)
class LectureHit:
    lecture_id: int
    title: str
    excerpt: str
    score: float


@dataclass(frozen=True)
class AnswerCandidate:
    answers: list[str]
    confidence: float
    source: str
    excerpt: str = ""
    reason: str = ""
    evidence: list[str] = field(default_factory=list)


@dataclass
class PageQuestion:
    text: str
    kind: QuestionKind = "unknown"
    options: list[str] = field(default_factory=list)
    input_selector: str | None = None
    option_selectors: dict[str, str] = field(default_factory=dict)
    submit_selector: str | None = None
    next_selector: str | None = None

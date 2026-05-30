from __future__ import annotations

from .lecture_index import LectureIndex
from .models import AnswerCandidate, PageQuestion
from .normalize import normalize_text
from .storage import QuestionStore


class AnswerEngine:
    def __init__(self, store: QuestionStore, lecture_index: LectureIndex, min_confidence: float = 0.62) -> None:
        self.store = store
        self.lecture_index = lecture_index
        self.min_confidence = min_confidence

    def answer(self, question: PageQuestion) -> AnswerCandidate | None:
        known = self.store.find_known_answer(question)
        if known:
            return known

        return self.answer_from_lectures(question)

    def answer_from_lectures(self, question: PageQuestion) -> AnswerCandidate | None:
        if question.options:
            return self._answer_choice(question)
        return self._answer_text(question)

    def _answer_choice(self, question: PageQuestion) -> AnswerCandidate | None:
        hits = self.lecture_index.search(question.text, question.options, limit=3)
        if not hits:
            return None
        best_hit = hits[0]
        excerpt = normalize_text(best_hit.excerpt)
        scored_options: list[tuple[str, float]] = []
        for option in question.options:
            normalized_option = normalize_text(option)
            if not normalized_option:
                continue
            exact_score = 0.95 if normalized_option in excerpt else 0.0
            term_score = _term_overlap(normalized_option, excerpt)
            scored_options.append((option, max(exact_score, term_score * best_hit.score)))
        scored_options.sort(key=lambda item: item[1], reverse=True)
        boolean_candidate = _boolean_answer(question, best_hit.title, best_hit.excerpt, best_hit.score)
        if boolean_candidate:
            return boolean_candidate
        if not scored_options or scored_options[0][1] < self.min_confidence:
            return AnswerCandidate([], scored_options[0][1] if scored_options else 0.0, best_hit.title, best_hit.excerpt)

        if question.kind == "multiple_choice":
            selected = [option for option, score in scored_options if score >= self.min_confidence]
        else:
            selected = [scored_options[0][0]]
        return AnswerCandidate(selected, scored_options[0][1], best_hit.title, best_hit.excerpt)

    def _answer_text(self, question: PageQuestion) -> AnswerCandidate | None:
        hits = self.lecture_index.search(question.text, limit=1)
        if not hits:
            return None
        hit = hits[0]
        confidence = min(hit.score, 0.7)
        if confidence < self.min_confidence:
            return AnswerCandidate([], confidence, hit.title, hit.excerpt)
        return AnswerCandidate([hit.excerpt], confidence, hit.title, hit.excerpt)


def _term_overlap(left: str, right: str) -> float:
    left_terms = {term for term in left.split() if len(term) >= 3}
    if not left_terms:
        return 0.0
    right_terms = set(right.split())
    return len(left_terms & right_terms) / len(left_terms)


def _boolean_answer(question: PageQuestion, source: str, excerpt: str, hit_score: float) -> AnswerCandidate | None:
    normalized_question = normalize_text(question.text)
    if not any(marker in normalized_question for marker in ["является ли", "являются ли", "относятся ли", "входит ли", "включает ли"]):
        return None
    if hit_score < 0.35:
        return None

    yes_options = [option for option in question.options if normalize_text(option).startswith("да")]
    no_options = [option for option in question.options if normalize_text(option).startswith("нет")]
    if not yes_options or not no_options:
        return None

    normalized_excerpt = normalize_text(excerpt)
    negative_markers = ["не является", "не являются", "не относятся", "не входит", "не включает", "не включают"]
    if any(marker in normalized_excerpt for marker in negative_markers):
        return AnswerCandidate([no_options[0]], min(max(hit_score, 0.72), 0.9), source, excerpt)
    return AnswerCandidate([yes_options[0]], min(max(hit_score, 0.72), 0.9), source, excerpt)

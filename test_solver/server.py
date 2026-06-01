from __future__ import annotations

import json
import hashlib
import re
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .answer_engine import AnswerEngine
from .lecture_index import LectureIndex
from .models import AnswerCandidate, PageQuestion
from .normalize import normalize_text, question_key
from .storage import QuestionStore
from .web_search import web_answer
from .web_search_v2 import web_answer_v2


BACKEND_VERSION = "0.1.12-multidb"


class SolverServer:
    def __init__(
        self,
        db_path: str,
        csv_path: str,
        min_confidence: float,
        web_search_enabled: bool = True,
        web_timeout: float = 8.0,
        web_max_pages: int = 5,
        web_cache_ttl: int = 86400,
        web_total_timeout: float = 6.0,
        web_negative_cache_ttl: int = 600,
        lecture_db_path: str | None = None,
    ) -> None:
        self.db_path = db_path
        self.csv_path = csv_path
        self.lecture_db_path = lecture_db_path or db_path
        self.min_confidence = min_confidence
        self.web_search_enabled = web_search_enabled
        self.web_timeout = web_timeout
        self.web_max_pages = web_max_pages
        self.web_cache_ttl = web_cache_ttl
        self.web_total_timeout = web_total_timeout
        self.web_negative_cache_ttl = web_negative_cache_ttl

    def serve(self, host: str = "127.0.0.1", port: int = 8765) -> None:
        server_state = self

        class Handler(BaseHTTPRequestHandler):
            def do_OPTIONS(self) -> None:
                self._send_json({"ok": True})

            def do_GET(self) -> None:
                if self.path == "/health":
                    self._send_json({"ok": True, "backendVersion": BACKEND_VERSION})
                    return
                if self.path == "/stats":
                    self._send_json(server_state.stats())
                    return
                self._send_json({"error": "not_found"}, status=404)

            def do_POST(self) -> None:
                if self.path not in {"/answer", "/correct", "/stats"}:
                    self._send_json({"error": "not_found"}, status=404)
                    return
                try:
                    payload = self._read_json()
                    if self.path == "/correct":
                        response = server_state.correct(payload)
                    elif self.path == "/stats":
                        response = server_state.stats(payload)
                    else:
                        response = server_state.answer(payload)
                    self._send_json(response)
                except Exception as error:
                    traceback.print_exc()
                    self._send_json({"error": str(error)}, status=500)

            def log_message(self, format: str, *args: Any) -> None:
                return

            def _read_json(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length).decode("utf-8")
                return json.loads(raw or "{}")

            def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Headers", "content-type")
                self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
                self.end_headers()
                self.wfile.write(body)

        httpd = ThreadingHTTPServer((host, port), Handler)
        print(f"Solver backend listening on http://{host}:{port}")
        print("Keep this terminal open while using the Chrome extension.")
        httpd.serve_forever()

    def answer(self, payload: dict[str, Any]) -> dict[str, Any]:
        db_path, csv_path, quiz_context = self._paths_for_payload(payload)
        store = QuestionStore(db_path)
        lecture_store = QuestionStore(self.lecture_db_path)
        try:
            index = LectureIndex(lecture_store)
            engine = AnswerEngine(store, index, min_confidence=self.min_confidence)
            results = []
            for item in payload.get("questions", []):
                question = PageQuestion(
                    text=str(item.get("text") or ""),
                    kind=item.get("kind") or "unknown",
                    options=_clean_options(item.get("options", [])),
                )
                known = store.find_known_answer(question)
                web_attempted = False
                web_candidate = None
                web_trace = _web_trace("skipped")
                pipeline: list[str] = []
                if known:
                    candidate = known
                    lecture_candidate = None
                    answer_origin = "db"
                    pipeline.append("db")
                else:
                    answer_origin = "computed"
                    wrong_candidate = self._wrong_answer_elimination(question, store)
                    resolution_question = _question_for_candidate_options(question, wrong_candidate)
                    if wrong_candidate and wrong_candidate.answers:
                        pipeline.append("wrong-elimination")
                    lecture_candidate = engine.answer_from_lectures(resolution_question)
                    if lecture_candidate and lecture_candidate.answers:
                        pipeline.append("lecture")
                    if self.web_search_enabled and resolution_question.options:
                        web_attempted = True
                        web_candidate, web_trace = self._web_answer(resolution_question, store)
                        pipeline.extend(web_trace["pipeline"])
                    candidate = self._choose_candidate(lecture_candidate, web_candidate, wrong_candidate)
                    if question.kind == "single_choice" and candidate and len(candidate.answers) > 1:
                        candidate = self._best_effort_answer(resolution_question, candidate)
                        if candidate and candidate.answers:
                            pipeline.append("single-choice-collapse")
                    if not candidate or not candidate.answers:
                        candidate = self._best_effort_answer(resolution_question, lecture_candidate or web_candidate or wrong_candidate)
                        if candidate and candidate.answers:
                            pipeline.append("best-effort")
                candidate = self._sanitize_choice_candidate(question, candidate)
                status = self._status_for(candidate)
                question_id = store.upsert_question(question, candidate, status)
                store.record_attempt(question_id, question, "extension", status)
                results.append(
                    {
                        "questionNumber": item.get("questionNumber"),
                        "text": question.text,
                        "kind": question.kind,
                        "options": question.options,
                        "answers": candidate.answers if candidate else [],
                        "confidence": candidate.confidence if candidate else 0.0,
                        "status": status,
                        "source": candidate.source if candidate else "",
                        "excerpt": candidate.excerpt if candidate else "",
                        "answerIndexes": _answer_indexes(question, candidate),
                        "autoSelectable": bool(candidate and candidate.answers and question.kind in {"single_choice", "multiple_choice"}),
                        "webAttempted": web_attempted,
                        "webStatus": web_trace["status"],
                        "webError": web_trace["error"],
                        "webDurationMs": web_trace["duration_ms"],
                        "webSource": web_candidate.source if web_candidate else web_trace["source"],
                        "webConfidence": web_candidate.confidence if web_candidate else 0.0,
                        "webCached": bool(web_trace.get("cached")),
                        "pipeline": pipeline,
                        "answerOrigin": answer_origin,
                        "fromDatabase": answer_origin == "db",
                        "backendVersion": BACKEND_VERSION,
                    }
                )
            store.export_csv(csv_path)
            return {
                "ok": True,
                "results": results,
                "csv": str(csv_path.resolve()),
                "db": str(db_path.resolve()),
                "quizContext": quiz_context,
                "backendVersion": BACKEND_VERSION,
            }
        finally:
            store.close()
            lecture_store.close()

    def correct(self, payload: dict[str, Any]) -> dict[str, Any]:
        db_path, csv_path, quiz_context = self._paths_for_payload(payload)
        store = QuestionStore(db_path)
        try:
            results = []
            saved = 0
            errors = 0
            correct = 0
            incorrect = 0
            partial = 0
            for raw_item in payload.get("questions", []):
                try:
                    item = raw_item if isinstance(raw_item, dict) else {}
                    answers = [
                        str(answer)
                        for answer in item.get("correctAnswers", [])
                        if str(answer).strip() and not _is_non_answer_option(str(answer))
                    ]
                    review_status = str(item.get("reviewStatus") or "unknown")
                    if review_status == "correct":
                        correct += 1
                    elif review_status == "incorrect":
                        incorrect += 1
                    elif review_status == "partial":
                        partial += 1

                    question = PageQuestion(
                        text=str(item.get("text") or ""),
                        kind=item.get("kind") or "unknown",
                        options=_clean_options(item.get("options", [])),
                    )
                    selected_answers = [
                        str(answer)
                        for answer in item.get("selectedAnswers", [])
                        if str(answer).strip() and not _is_non_answer_option(str(answer))
                    ]
                    if review_status == "incorrect" and selected_answers:
                        store.add_wrong_answers(question, selected_answers)
                    if not answers:
                        errors += 1
                        results.append(
                            {
                                "questionNumber": item.get("questionNumber"),
                                "reviewStatus": review_status,
                                "text": question.text,
                                "kind": question.kind,
                                "options": question.options,
                                "answers": [],
                                "confidence": 0.0,
                                "status": "needs_review",
                                "source": "moodle-review",
                                "excerpt": str(item.get("feedback") or ""),
                                "wrongAnswersSaved": len(selected_answers) if review_status == "incorrect" else 0,
                            }
                        )
                        continue
                    question_id = store.upsert_correct_question(
                        question,
                        answers,
                        source="moodle-review",
                        excerpt=str(item.get("feedback") or ""),
                    )
                    saved += 1
                    store.record_attempt(question_id, question, "extension_correct", "saved")
                    results.append(
                        {
                            "questionNumber": item.get("questionNumber"),
                            "reviewStatus": review_status,
                            "text": question.text,
                            "kind": question.kind,
                            "options": question.options,
                            "answers": answers,
                            "confidence": 1.0,
                            "status": "answered",
                            "source": "moodle-review",
                            "excerpt": str(item.get("feedback") or ""),
                            "wrongAnswersSaved": len(selected_answers) if review_status == "incorrect" else 0,
                        }
                    )
                except Exception as error:
                    errors += 1
                    results.append(
                        {
                            "questionNumber": raw_item.get("questionNumber") if isinstance(raw_item, dict) else None,
                            "reviewStatus": raw_item.get("reviewStatus") if isinstance(raw_item, dict) else "unknown",
                            "text": str(raw_item.get("text") or "") if isinstance(raw_item, dict) else "",
                            "kind": raw_item.get("kind") if isinstance(raw_item, dict) else "unknown",
                            "options": raw_item.get("options", []) if isinstance(raw_item, dict) else [],
                            "answers": [],
                            "confidence": 0.0,
                            "status": "needs_review",
                            "source": "moodle-review",
                            "excerpt": f"save error: {error}",
                        }
                    )
            store.export_csv(csv_path)
            return {
                "ok": True,
                "saved": saved,
                "total": len(payload.get("questions", [])),
                "errors": errors,
                "correct": correct,
                "incorrect": incorrect,
                "partial": partial,
                "results": results,
                "csv": str(csv_path.resolve()),
                "db": str(db_path.resolve()),
                "quizContext": quiz_context,
                "backendVersion": BACKEND_VERSION,
            }
        finally:
            store.close()

    def stats(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        db_path, csv_path, quiz_context = self._paths_for_payload(payload or {})
        store = QuestionStore(db_path)
        try:
            return {
                "ok": True,
                "stats": store.stats(),
                "csv": str(csv_path.resolve()),
                "db": str(db_path.resolve()),
                "quizContext": quiz_context,
                "backendVersion": BACKEND_VERSION,
            }
        finally:
            store.close()

    def _paths_for_payload(self, payload: dict[str, Any]) -> tuple[Path, Path, dict[str, Any]]:
        context = _normalize_quiz_context(payload.get("quizContext"))
        if not context.get("quizKey"):
            return Path(self.db_path), Path(self.csv_path), context

        quiz_key = str(context["quizKey"])
        base_dir = Path(self.db_path).parent / "quizzes" / quiz_key
        return base_dir / "questions.sqlite", base_dir / "questions.csv", context

    def _status_for(self, candidate: AnswerCandidate | None) -> str:
        if not candidate or not candidate.answers:
            return "needs_review"
        if candidate.confidence < self.min_confidence:
            return "needs_review"
        return "answered"

    def _web_answer(self, question: PageQuestion, store: QuestionStore) -> tuple[AnswerCandidate | None, dict[str, Any]]:
        started = time.monotonic()
        trace = _web_trace("not_found")
        candidate = None
        negative_cache_key = _web_negative_cache_key(question)
        cached = store.get_web_cache(negative_cache_key, self.web_negative_cache_ttl) if self.web_negative_cache_ttl > 0 else None
        if cached and cached.get("status") in {"not_found", "timeout", "error"}:
            cached_trace = _web_trace(
                str(cached.get("status") or "not_found"),
                error=str(cached.get("error") or ""),
                duration_ms=0.0,
                source=str(cached.get("source") or ""),
                cached=True,
            )
            cached_trace["pipeline"] = [str(step) for step in cached.get("pipeline", [])]
            return None, cached_trace

        try:
            candidate = web_answer_v2(
                question,
                store,
                timeout=self.web_timeout,
                max_pages=self.web_max_pages,
                cache_ttl=self.web_cache_ttl,
                total_timeout=self.web_total_timeout,
            )
            trace["pipeline"].append("web-v2")
        except Exception as error:
            trace["pipeline"].append("web-v2")
            trace["status"] = _web_error_status(error)
            trace["error"] = str(error)
        trace["duration_ms"] = round((time.monotonic() - started) * 1000)
        if self.web_total_timeout > 0 and trace["duration_ms"] >= self.web_total_timeout * 1000:
            trace["status"] = "timeout"
            trace["error"] = trace["error"] or f"web-v2 exceeded total budget {self.web_total_timeout}s"
        if candidate and candidate.answers:
            trace["status"] = "ok"
            trace["source"] = candidate.source
            return candidate, trace
        v2_error = trace["error"] if trace["status"] == "error" else ""
        if trace["status"] == "timeout":
            self._cache_negative_web_result(store, negative_cache_key, trace)
            return None, trace

        try:
            elapsed = time.monotonic() - started
            remaining_budget = self.web_total_timeout - elapsed if self.web_total_timeout > 0 else self.web_timeout
            if remaining_budget <= 0:
                trace["status"] = "timeout"
                trace["error"] = trace["error"] or f"web total timeout exceeded {self.web_total_timeout}s"
                self._cache_negative_web_result(store, negative_cache_key, trace)
                return None, trace
            candidate = web_answer(question, timeout=min(self.web_timeout, remaining_budget))
            trace["pipeline"].append("web-legacy")
        except Exception as error:
            trace["pipeline"].append("web-legacy")
            trace["status"] = _web_error_status(error)
            trace["error"] = str(error)
            trace["duration_ms"] = round((time.monotonic() - started) * 1000)
            self._cache_negative_web_result(store, negative_cache_key, trace)
            return None, trace
        trace["duration_ms"] = round((time.monotonic() - started) * 1000)
        if self.web_total_timeout > 0 and trace["duration_ms"] >= self.web_total_timeout * 1000:
            trace["status"] = "timeout"
            trace["error"] = trace["error"] or f"web total timeout exceeded {self.web_total_timeout}s"
            self._cache_negative_web_result(store, negative_cache_key, trace)
            return None, trace
        if candidate and candidate.answers:
            trace["status"] = "ok"
            trace["source"] = candidate.source
            return candidate, trace
        if v2_error:
            trace["status"] = "error"
            trace["error"] = v2_error
            self._cache_negative_web_result(store, negative_cache_key, trace)
            return None, trace
        trace["status"] = "not_found"
        self._cache_negative_web_result(store, negative_cache_key, trace)
        return None, trace

    def _cache_negative_web_result(self, store: QuestionStore, key: str, trace: dict[str, Any]) -> None:
        if self.web_negative_cache_ttl <= 0 or trace.get("status") not in {"not_found", "timeout", "error"}:
            return
        store.set_web_cache(
            key,
            {
                "status": trace.get("status") or "not_found",
                "error": trace.get("error") or "",
                "source": trace.get("source") or "",
                "pipeline": trace.get("pipeline") or [],
            },
        )

    def _choose_candidate(
        self,
        lecture_candidate: AnswerCandidate | None,
        web_candidate: AnswerCandidate | None,
        wrong_candidate: AnswerCandidate | None = None,
    ) -> AnswerCandidate | None:
        lecture_has_answer = bool(lecture_candidate and lecture_candidate.answers)
        web_has_answer = bool(web_candidate and web_candidate.answers)
        wrong_has_answer = bool(wrong_candidate and wrong_candidate.answers)
        if web_has_answer and not lecture_has_answer:
            return web_candidate
        if lecture_has_answer and not web_has_answer:
            if lecture_candidate.confidence >= self.min_confidence or not wrong_has_answer:
                return lecture_candidate
            return wrong_candidate
        if not lecture_has_answer and not web_has_answer:
            return wrong_candidate or lecture_candidate or web_candidate

        assert lecture_candidate is not None
        assert web_candidate is not None
        if web_candidate.confidence >= self.min_confidence:
            return web_candidate
        if lecture_candidate.confidence >= self.min_confidence:
            return lecture_candidate
        if wrong_has_answer:
            return wrong_candidate
        if web_candidate.confidence >= lecture_candidate.confidence - 0.03:
            return web_candidate
        return lecture_candidate

    def _wrong_answer_elimination(self, question: PageQuestion, store: QuestionStore) -> AnswerCandidate | None:
        if not question.options:
            return None
        wrong_answers = set(store.find_wrong_answers(question))
        if not wrong_answers:
            return None
        remaining = [option for option in question.options if option not in wrong_answers]
        if not remaining or len(remaining) == len(question.options):
            return None
        if question.kind == "single_choice" and len(remaining) != 1:
            return AnswerCandidate(
                answers=remaining,
                confidence=0.35,
                source="wrong-answer-elimination",
                excerpt=f"Исключены ранее неверные варианты: {', '.join(sorted(wrong_answers))}",
            )
        confidence = 0.7 if len(remaining) == 1 else 0.45
        return AnswerCandidate(
            answers=remaining,
            confidence=confidence,
            source="wrong-answer-elimination",
            excerpt=f"Исключены ранее неверные варианты: {', '.join(sorted(wrong_answers))}",
        )

    def _best_effort_answer(
        self,
        question: PageQuestion,
        evidence: AnswerCandidate | None = None,
    ) -> AnswerCandidate | None:
        if not question.options:
            if evidence and evidence.excerpt:
                return AnswerCandidate(
                    answers=[evidence.excerpt],
                    confidence=min(max(evidence.confidence * 0.5, 0.2), self.min_confidence - 0.01),
                    source="best-effort",
                    excerpt=evidence.excerpt,
                )
            return None

        evidence_text = " ".join([question.text, evidence.excerpt if evidence else ""])
        normalized_evidence = normalize_text(evidence_text)
        scored_options: list[tuple[str, float]] = []
        for option in question.options:
            normalized_option = normalize_text(option)
            if not normalized_option:
                continue
            exact_score = 0.58 if normalized_option and normalized_option in normalized_evidence else 0.0
            overlap_score = _term_overlap(normalized_option, normalized_evidence) * 0.55
            scored_options.append((option, max(exact_score, overlap_score)))

        scored_options.sort(key=lambda item: item[1], reverse=True)
        if scored_options and scored_options[0][1] > 0:
            answer = scored_options[0][0]
            confidence = min(max(scored_options[0][1], evidence.confidence * 0.7 if evidence else 0.0, 0.25), self.min_confidence - 0.01)
        else:
            answer = question.options[0]
            confidence = 0.12

        return AnswerCandidate(
            answers=[answer],
            confidence=confidence,
            source="best-effort",
            excerpt=evidence.excerpt if evidence else "No confirmed source found.",
        )

    def _sanitize_choice_candidate(
        self,
        question: PageQuestion,
        candidate: AnswerCandidate | None,
    ) -> AnswerCandidate | None:
        if not candidate or not candidate.answers or question.kind not in {"single_choice", "multiple_choice"} or not question.options:
            return candidate

        valid_answers = [option for option in question.options if option in candidate.answers]
        if question.kind == "single_choice":
            if len(valid_answers) == 1:
                return AnswerCandidate(
                    answers=valid_answers,
                    confidence=candidate.confidence,
                    source=candidate.source,
                    excerpt=candidate.excerpt,
                )
            return self._best_effort_answer(question, candidate)

        if valid_answers:
            return AnswerCandidate(
                answers=valid_answers,
                confidence=candidate.confidence,
                source=candidate.source,
                excerpt=candidate.excerpt,
            )
        return self._best_effort_answer(question, candidate)

def _term_overlap(left: str, right: str) -> float:
    left_terms = {term for term in left.split() if len(term) >= 3}
    if not left_terms:
        return 0.0
    right_terms = set(right.split())
    return len(left_terms & right_terms) / len(left_terms)


def _web_trace(
    status: str,
    error: str = "",
    duration_ms: float = 0.0,
    source: str = "",
    cached: bool = False,
) -> dict[str, Any]:
    return {
        "status": status,
        "error": error,
        "duration_ms": duration_ms,
        "source": source,
        "cached": cached,
        "pipeline": [],
    }


def _web_error_status(error: Exception) -> str:
    name = type(error).__name__.lower()
    message = str(error).lower()
    if "timeout" in name or "timeout" in message or "timed out" in message:
        return "timeout"
    return "error"


def _answer_indexes(question: PageQuestion, candidate: AnswerCandidate | None) -> list[int]:
    if not candidate:
        return []
    return [index for index, option in enumerate(question.options) if option in candidate.answers]


def _is_non_answer_option(value: str) -> bool:
    raw = str(value or "").strip()
    numbered = re.match(r"^\s*\d+[.)]\s+(.+)$", raw)
    normalized = normalize_text(numbered.group(1) if numbered else raw)
    return normalized in {
        "",
        "очистить мой выбор",
        "сбросить мой выбор",
        "очистить выбор",
        "сбросить выбор",
        "clear my choice",
        "clear choice",
        "remove my choice",
        "пока нет ответа",
        "ответ сохранен",
        "отметить вопрос",
    }


def _clean_options(options: Any) -> list[str]:
    cleaned: list[str] = []
    for option in options or []:
        text = str(option).strip()
        if _is_non_answer_option(text):
            continue
        if text not in cleaned:
            cleaned.append(text)
    return cleaned


def _question_for_candidate_options(question: PageQuestion, candidate: AnswerCandidate | None) -> PageQuestion:
    if not candidate or not candidate.answers:
        return question
    remaining_options = [option for option in question.options if option in candidate.answers]
    if not remaining_options or len(remaining_options) == len(question.options):
        return question
    return PageQuestion(
        text=question.text,
        kind=question.kind,
        options=remaining_options,
        input_selector=question.input_selector,
        option_selectors=question.option_selectors,
        submit_selector=question.submit_selector,
        next_selector=question.next_selector,
    )


def _web_negative_cache_key(question: PageQuestion) -> str:
    payload = json.dumps(
        {
            "version": 2,
            "key": question_key(question.text, question.options),
            "kind": question.kind,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return "negative:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normalize_quiz_context(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}

    cmid = _clean_context_value(value.get("cmid"))
    quiz_title = _clean_context_value(value.get("quizTitle"))
    course_title = _clean_context_value(value.get("courseTitle"))
    url = _clean_context_value(value.get("url"))
    quiz_key = _clean_context_value(value.get("quizKey"))
    if not quiz_key and (cmid or quiz_title):
        quiz_key = _quiz_key(cmid, quiz_title)
    if quiz_key:
        quiz_key = _safe_quiz_key(quiz_key)

    return {
        "cmid": cmid,
        "quizTitle": quiz_title,
        "courseTitle": course_title,
        "url": url,
        "quizKey": quiz_key,
    }


def _clean_context_value(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _quiz_key(cmid: str, quiz_title: str) -> str:
    identity = f"{cmid}|{quiz_title}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:10]
    slug_source = "-".join(part for part in [cmid, quiz_title] if part)
    slug = re.sub(r"[^a-zA-Z0-9а-яА-ЯёЁ]+", "-", slug_source).strip("-").lower()
    slug = slug[:64] or "quiz"
    return _safe_quiz_key(f"{slug}-{digest}")


def _safe_quiz_key(value: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip(".-")
    return safe[:96] or "quiz"

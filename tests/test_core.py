from __future__ import annotations

from pathlib import Path

from test_solver.answer_engine import AnswerEngine
from test_solver.lecture_index import LectureIndex, ingest_paths
from test_solver.models import AnswerCandidate, PageQuestion
from test_solver.normalize import question_key
from test_solver.server import SolverServer
from test_solver.storage import QuestionStore
from test_solver import server as server_module
from test_solver import http_client
from test_solver import web_search_v2 as web_search_v2_module
from test_solver.web_search_v2 import WebDocument, answer_from_documents


def test_question_key_ignores_spacing_and_option_order() -> None:
    left = question_key("  Что   такое HTTP? ", ["Протокол", "Сервер"])
    right = question_key("что такое http", [" сервер ", "протокол"])
    assert left == right


def test_ingest_and_answer_choice(tmp_path: Path) -> None:
    lecture = tmp_path / "lecture.txt"
    lecture.write_text("HTTP — это протокол передачи гипертекста между клиентом и сервером.", encoding="utf-8")
    store = QuestionStore(tmp_path / "questions.sqlite")
    try:
        assert ingest_paths(store, [lecture]) == 1
        question = PageQuestion(
            text="Что такое HTTP?",
            kind="single_choice",
            options=["Язык программирования", "Протокол передачи гипертекста", "Операционная система"],
        )
        engine = AnswerEngine(store, LectureIndex(store), min_confidence=0.2)
        answer = engine.answer(question)
        assert answer is not None
        assert answer.answers == ["Протокол передачи гипертекста"]
    finally:
        store.close()


def test_store_deduplicates_questions(tmp_path: Path) -> None:
    store = QuestionStore(tmp_path / "questions.sqlite")
    try:
        first = PageQuestion(text="Что такое HTTP?", kind="single_choice", options=["A", "B"])
        second = PageQuestion(text="Что   такое HTTP", kind="single_choice", options=["B", "A"])
        first_id = store.upsert_question(first, None, "diagnosed")
        second_id = store.upsert_question(second, None, "needs_review")
        assert first_id == second_id
    finally:
        store.close()


def test_solver_server_answers_question(tmp_path: Path) -> None:
    lecture = tmp_path / "lecture.txt"
    lecture.write_text("HTTP — это протокол передачи гипертекста между клиентом и сервером.", encoding="utf-8")
    store = QuestionStore(tmp_path / "questions.sqlite")
    try:
        ingest_paths(store, [lecture])
    finally:
        store.close()

    server = SolverServer(
        str(tmp_path / "questions.sqlite"),
        str(tmp_path / "questions.csv"),
        min_confidence=0.2,
        web_search_enabled=False,
    )
    response = server.answer(
        {
            "questions": [
                {
                    "questionNumber": 7,
                    "text": "Что такое HTTP?",
                    "kind": "single_choice",
                    "options": [
                        "Язык программирования",
                        "Протокол передачи гипертекста",
                        "Операционная система",
                    ],
                }
            ]
        }
    )
    assert response["ok"] is True
    assert response["results"][0]["questionNumber"] == 7
    assert response["results"][0]["answers"] == ["Протокол передачи гипертекста"]


def test_solver_server_filters_clear_choice_option(tmp_path: Path) -> None:
    server = SolverServer(
        str(tmp_path / "questions.sqlite"),
        str(tmp_path / "questions.csv"),
        min_confidence=0.2,
        web_search_enabled=False,
    )
    response = server.answer(
        {
            "questions": [
                {
                    "questionNumber": 8,
                    "text": "Сколько этапов принято включать?",
                    "kind": "single_choice",
                    "options": ["6", "7", "8", "Очистить мой выбор"],
                }
            ]
        }
    )
    result = response["results"][0]
    assert "Очистить мой выбор" not in result["options"]
    assert "Очистить мой выбор" not in result["answers"]


def test_solver_server_stats(tmp_path: Path) -> None:
    server = SolverServer(
        str(tmp_path / "questions.sqlite"),
        str(tmp_path / "questions.csv"),
        min_confidence=0.2,
        web_search_enabled=False,
    )
    server.answer(
        {
            "questions": [
                {
                    "text": "Что такое HTTP?",
                    "kind": "single_choice",
                    "options": ["A", "B"],
                }
            ]
        }
    )
    response = server.stats()
    assert response["ok"] is True
    assert response["backendVersion"]
    assert response["stats"]["total"] == 1


def test_self_control_reference_points_to_answer_slide(tmp_path: Path) -> None:
    lecture = tmp_path / "lecture.txt"
    lecture.write_text(
        """
        Крючков А.В. 37
        Структура информационного обеспечения:
        1. Исходные данные.
        2. Промежуточные данные.
        3. Выходные данные.
        4. Формы документов.
        5. Система классификаторов.

        Вопросы для самоконтроля
        36. Являются ли формы документов организации составной структурной частью информационного обеспечения её КИС? (37)
        """,
        encoding="utf-8",
    )
    store = QuestionStore(tmp_path / "questions.sqlite")
    try:
        ingest_paths(store, [lecture])
        question = PageQuestion(
            text="Являются ли формы документов организации составной структурной частью информационного обеспечения её КИС?",
            kind="single_choice",
            options=["Да, являются.", "Нет, не являются."],
        )
        answer = AnswerEngine(store, LectureIndex(store), min_confidence=0.62).answer(question)
        assert answer is not None
        assert answer.answers == ["Да, являются."]
    finally:
        store.close()


def test_solver_server_saves_correct_review_answers(tmp_path: Path) -> None:
    server = SolverServer(
        str(tmp_path / "questions.sqlite"),
        str(tmp_path / "questions.csv"),
        min_confidence=0.62,
    )
    response = server.correct(
        {
            "questions": [
                {
                    "questionNumber": 12,
                    "reviewStatus": "correct",
                    "text": "Являются ли формы документов частью информационного обеспечения?",
                    "kind": "single_choice",
                    "options": ["Да, являются.", "Нет, не являются."],
                    "correctAnswers": ["Да, являются."],
                    "feedback": "Правильный ответ: Да, являются.",
                }
            ]
        }
    )
    assert response["ok"] is True
    assert response["saved"] == 1
    assert response["total"] == 1
    assert response["errors"] == 0
    assert response["correct"] == 1
    assert response["incorrect"] == 0
    assert response["partial"] == 0
    assert response["results"][0]["questionNumber"] == 12
    assert response["results"][0]["reviewStatus"] == "correct"

    store = QuestionStore(tmp_path / "questions.sqlite")
    try:
        answer = store.find_known_answer(
            PageQuestion(
                text="Являются ли формы документов частью информационного обеспечения?",
                kind="single_choice",
                options=["Да, являются.", "Нет, не являются."],
            )
        )
        assert answer is not None
        assert answer.answers == ["Да, являются."]
        assert answer.source == "moodle-review"
    finally:
        store.close()


def test_solver_server_reports_incorrect_review_questions(tmp_path: Path) -> None:
    server = SolverServer(
        str(tmp_path / "questions.sqlite"),
        str(tmp_path / "questions.csv"),
        min_confidence=0.62,
    )
    response = server.correct(
        {
            "questions": [
                {
                    "questionNumber": 18,
                    "reviewStatus": "incorrect",
                    "text": "Вопрос с неверным ответом?",
                    "kind": "single_choice",
                    "options": ["A", "B"],
                    "correctAnswers": [],
                    "selectedAnswers": ["A"],
                    "feedback": "Ваш ответ неверный.",
                }
            ]
        }
    )
    assert response["ok"] is True
    assert response["saved"] == 0
    assert response["total"] == 1
    assert response["errors"] == 1
    assert response["incorrect"] == 1
    assert response["results"][0]["reviewStatus"] == "incorrect"
    assert response["results"][0]["status"] == "needs_review"
    assert response["results"][0]["wrongAnswersSaved"] == 1


def test_solver_server_eliminates_saved_wrong_answers(tmp_path: Path, monkeypatch) -> None:
    question_payload = {
        "questionNumber": 4,
        "text": "Какой вариант правильный?",
        "kind": "single_choice",
        "options": ["A", "B"],
    }
    server = SolverServer(
        str(tmp_path / "questions.sqlite"),
        str(tmp_path / "questions.csv"),
        min_confidence=0.62,
    )
    server.correct(
        {
            "questions": [
                {
                    **question_payload,
                    "reviewStatus": "incorrect",
                    "correctAnswers": [],
                    "selectedAnswers": ["A"],
                    "feedback": "Ваш ответ неверный.",
                }
            ]
        }
    )
    monkeypatch.setattr(server_module, "web_answer", lambda question, timeout=8.0: None)
    monkeypatch.setattr(server_module, "web_answer_v2", lambda *args, **kwargs: None)

    response = server.answer({"questions": [question_payload]})
    result = response["results"][0]
    assert result["answers"] == ["B"]
    assert result["source"] == "wrong-answer-elimination"
    assert result["status"] == "answered"


def test_solver_server_does_not_return_multiple_answers_for_single_choice(tmp_path: Path, monkeypatch) -> None:
    question_payload = {
        "questionNumber": 6,
        "text": "Какой орган утверждает итоги работ?",
        "kind": "single_choice",
        "options": ["A", "B", "C"],
    }
    server = SolverServer(
        str(tmp_path / "questions.sqlite"),
        str(tmp_path / "questions.csv"),
        min_confidence=0.62,
    )
    server.correct(
        {
            "questions": [
                {
                    **question_payload,
                    "reviewStatus": "incorrect",
                    "correctAnswers": [],
                    "selectedAnswers": ["A"],
                    "feedback": "Ваш ответ неверный.",
                }
            ]
        }
    )
    monkeypatch.setattr(server_module, "web_answer", lambda question, timeout=8.0: None)
    monkeypatch.setattr(server_module, "web_answer_v2", lambda *args, **kwargs: None)

    response = server.answer({"questions": [question_payload]})
    result = response["results"][0]
    assert len(result["answers"]) == 1
    assert result["answers"][0] in {"B", "C"}
    assert "single-choice-collapse" in result["pipeline"]


def test_solver_server_does_not_return_lecture_excerpt_as_choice_answer(tmp_path: Path, monkeypatch) -> None:
    long_excerpt = "57. Как поступать с внедрённым решением? 63. Сколько составных элементов включают специалисты?"

    def fake_lecture_answer(self, question):
        return AnswerCandidate(
            answers=[long_excerpt],
            confidence=0.7,
            source="lecture.txt",
            excerpt=f"В лекции сказано: {long_excerpt}. Ответ 6.",
        )

    monkeypatch.setattr(server_module.AnswerEngine, "answer_from_lectures", fake_lecture_answer)
    server = SolverServer(
        str(tmp_path / "questions.sqlite"),
        str(tmp_path / "questions.csv"),
        min_confidence=0.62,
        web_search_enabled=False,
    )
    response = server.answer(
        {
            "questions": [
                {
                    "questionNumber": 30,
                    "text": "Сколько составных элементов включают специалисты в понятие Индустрии 4.0?",
                    "kind": "single_choice",
                    "options": ["6", "11", "8"],
                }
            ]
        }
    )
    result = response["results"][0]
    assert result["answers"] == ["6"]
    assert long_excerpt not in result["answers"]
    assert result["answerIndexes"] == [0]


def test_solver_server_uses_web_fallback(tmp_path: Path, monkeypatch) -> None:
    def fake_web_answer(question, timeout=8.0):
        from test_solver.models import AnswerCandidate

        return AnswerCandidate(
            answers=["Верный вариант из сети"],
            confidence=0.7,
            source="web-search",
            excerpt="snippet",
        )

    monkeypatch.setattr(server_module, "web_answer", fake_web_answer)
    monkeypatch.setattr(server_module, "web_answer_v2", lambda *args, **kwargs: None)
    server = SolverServer(
        str(tmp_path / "questions.sqlite"),
        str(tmp_path / "questions.csv"),
        min_confidence=0.62,
    )
    response = server.answer(
        {
            "questions": [
                {
                    "questionNumber": 3,
                    "text": "Вопрос без локального ответа?",
                    "kind": "single_choice",
                    "options": ["Неверный", "Верный вариант из сети"],
                }
            ]
        }
    )
    assert response["ok"] is True
    assert response["results"][0]["answers"] == ["Верный вариант из сети"]
    assert response["results"][0]["source"] == "web-search"


def test_solver_server_prioritizes_database_without_web(tmp_path: Path, monkeypatch) -> None:
    question_payload = {
        "questionNumber": 5,
        "text": "Вопрос уже есть в БД?",
        "kind": "single_choice",
        "options": ["A", "B"],
    }
    server = SolverServer(
        str(tmp_path / "questions.sqlite"),
        str(tmp_path / "questions.csv"),
        min_confidence=0.62,
    )
    server.correct(
        {
            "questions": [
                {
                    **question_payload,
                    "reviewStatus": "correct",
                    "correctAnswers": ["B"],
                    "feedback": "Правильный ответ: B.",
                }
            ]
        }
    )

    def fail_web(*args, **kwargs):
        raise AssertionError("web should not be called for known DB answer")

    monkeypatch.setattr(server_module, "web_answer_v2", fail_web)
    monkeypatch.setattr(server_module, "web_answer", fail_web)
    response = server.answer({"questions": [question_payload]})
    result = response["results"][0]

    assert result["answers"] == ["B"]
    assert result["source"] == "moodle-review"
    assert result["fromDatabase"] is True
    assert result["answerOrigin"] == "db"
    assert result["pipeline"] == ["db"]


def test_solver_server_checks_only_not_wrong_options_in_web(tmp_path: Path, monkeypatch) -> None:
    seen_options = []
    question_payload = {
        "questionNumber": 6,
        "text": "Какой вариант остаётся после исключения неверного?",
        "kind": "single_choice",
        "options": ["A", "B", "C"],
    }
    server = SolverServer(
        str(tmp_path / "questions.sqlite"),
        str(tmp_path / "questions.csv"),
        min_confidence=0.62,
    )
    server.correct(
        {
            "questions": [
                {
                    **question_payload,
                    "reviewStatus": "incorrect",
                    "correctAnswers": [],
                    "selectedAnswers": ["A", "C"],
                    "feedback": "Ваш ответ неверный.",
                }
            ]
        }
    )

    def fake_web_v2(question, *args, **kwargs):
        seen_options.append(question.options)
        return AnswerCandidate(
            answers=["B"],
            confidence=0.8,
            source="web-search-v2",
            excerpt="B подтверждён источником.",
        )

    monkeypatch.setattr(server_module, "web_answer_v2", fake_web_v2)
    monkeypatch.setattr(server_module, "web_answer", lambda question, timeout=8.0: None)
    response = server.answer({"questions": [question_payload]})
    result = response["results"][0]

    assert seen_options == [["B"]]
    assert result["answers"] == ["B"]
    assert result["source"] == "web-search-v2"
    assert result["pipeline"][:2] == ["wrong-elimination", "web-v2"]


def test_solver_server_checks_web_after_lecture_for_new_question(tmp_path: Path, monkeypatch) -> None:
    lecture = tmp_path / "lecture.txt"
    lecture.write_text("HTTP — это язык программирования для серверов.", encoding="utf-8")
    store = QuestionStore(tmp_path / "questions.sqlite")
    try:
        ingest_paths(store, [lecture])
    finally:
        store.close()

    def fake_web_answer_v2(*args, **kwargs):
        return AnswerCandidate(
            answers=["Протокол передачи гипертекста"],
            confidence=0.8,
            source="web-search-v2",
            excerpt="https://example.edu/http\nHTTP — протокол передачи гипертекста.",
        )

    monkeypatch.setattr(server_module, "web_answer_v2", fake_web_answer_v2)
    monkeypatch.setattr(server_module, "web_answer", lambda question, timeout=8.0: None)
    server = SolverServer(
        str(tmp_path / "questions.sqlite"),
        str(tmp_path / "questions.csv"),
        min_confidence=0.2,
    )
    response = server.answer(
        {
            "questions": [
                {
                    "questionNumber": 9,
                    "text": "Что такое HTTP?",
                    "kind": "single_choice",
                    "options": [
                        "Язык программирования",
                        "Протокол передачи гипертекста",
                        "Операционная система",
                    ],
                }
            ]
        }
    )
    result = response["results"][0]
    assert result["answers"] == ["Протокол передачи гипертекста"]
    assert result["source"] == "web-search-v2"
    assert result["webAttempted"] is True
    assert result["webStatus"] == "ok"
    assert "web-v2" in result["pipeline"]


def test_solver_server_reports_web_error_without_500(tmp_path: Path, monkeypatch) -> None:
    def failing_web_v2(*args, **kwargs):
        raise RuntimeError("web failed")

    monkeypatch.setattr(server_module, "web_answer_v2", failing_web_v2)
    monkeypatch.setattr(server_module, "web_answer", lambda question, timeout=8.0: None)
    server = SolverServer(
        str(tmp_path / "questions.sqlite"),
        str(tmp_path / "questions.csv"),
        min_confidence=0.62,
    )
    response = server.answer(
        {
            "questions": [
                {
                    "questionNumber": 10,
                    "text": "Вопрос с ошибкой web?",
                    "kind": "single_choice",
                    "options": ["A", "B"],
                }
            ]
        }
    )
    result = response["results"][0]
    assert response["ok"] is True
    assert result["source"] == "best-effort"
    assert result["webAttempted"] is True
    assert result["webStatus"] == "error"
    assert "web failed" in result["webError"]
    assert "web-v2" in result["pipeline"]
    assert "web-legacy" in result["pipeline"]


def test_solver_server_reports_web_timeout_without_empty_result(tmp_path: Path, monkeypatch) -> None:
    captured_total_timeout = None

    def timing_out_web_v2(*args, **kwargs):
        nonlocal captured_total_timeout
        captured_total_timeout = kwargs.get("total_timeout")
        raise TimeoutError("timed out")

    monkeypatch.setattr(server_module, "web_answer_v2", timing_out_web_v2)
    monkeypatch.setattr(server_module, "web_answer", lambda question, timeout=8.0: None)
    server = SolverServer(
        str(tmp_path / "questions.sqlite"),
        str(tmp_path / "questions.csv"),
        min_confidence=0.62,
        web_total_timeout=0.25,
    )
    response = server.answer(
        {
            "questions": [
                {
                    "questionNumber": 11,
                    "text": "Вопрос с timeout web?",
                    "kind": "single_choice",
                    "options": ["A", "B"],
                }
            ]
        }
    )
    result = response["results"][0]
    assert response["ok"] is True
    assert result["answers"]
    assert result["source"] == "best-effort"
    assert result["webStatus"] == "timeout"
    assert "timed out" in result["webError"]
    assert captured_total_timeout == 0.25


def test_solver_server_caches_negative_web_timeout(tmp_path: Path, monkeypatch) -> None:
    calls = 0

    def timing_out_web_v2(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise TimeoutError("timed out")

    monkeypatch.setattr(server_module, "web_answer_v2", timing_out_web_v2)
    monkeypatch.setattr(server_module, "web_answer", lambda question, timeout=8.0: None)
    server = SolverServer(
        str(tmp_path / "questions.sqlite"),
        str(tmp_path / "questions.csv"),
        min_confidence=0.62,
        web_total_timeout=0.25,
        web_negative_cache_ttl=600,
    )
    payload = {
        "questions": [
            {
                "questionNumber": 11,
                "text": "Одинаковый вопрос с timeout web?",
                "kind": "single_choice",
                "options": ["A", "B"],
            }
        ]
    }

    first = server.answer(payload)["results"][0]
    second = server.answer(payload)["results"][0]

    assert calls == 1
    assert first["webStatus"] == "timeout"
    assert first["webCached"] is False
    assert second["webStatus"] == "timeout"
    assert second["webCached"] is True
    assert second["webDurationMs"] == 0.0


def test_solver_server_does_not_timeout_web_v2_by_per_request_limit(tmp_path: Path, monkeypatch) -> None:
    def slow_empty_web_v2(*args, **kwargs):
        import time

        time.sleep(0.03)
        return None

    def fake_legacy_web(question, timeout=8.0):
        return AnswerCandidate(
            answers=["B"],
            confidence=0.7,
            source="web-search",
            excerpt="legacy result",
        )

    monkeypatch.setattr(server_module, "web_answer_v2", slow_empty_web_v2)
    monkeypatch.setattr(server_module, "web_answer", fake_legacy_web)
    server = SolverServer(
        str(tmp_path / "questions.sqlite"),
        str(tmp_path / "questions.csv"),
        min_confidence=0.62,
        web_timeout=0.01,
        web_total_timeout=1.0,
    )
    response = server.answer(
        {
            "questions": [
                {
                    "questionNumber": 14,
                    "text": "Вопрос, где web-v2 дольше одного запроса, но короче общего бюджета?",
                    "kind": "single_choice",
                    "options": ["A", "B"],
                }
            ]
        }
    )
    result = response["results"][0]
    assert result["source"] == "web-search"
    assert result["webStatus"] == "ok"
    assert result["answers"] == ["B"]
    assert result["pipeline"] == ["web-v2", "web-legacy"]


def test_web_search_v2_skips_failed_query_before_deadline(monkeypatch) -> None:
    calls = 0

    def flaky_search_result_documents(query, timeout=8.0):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError("single request timed out")
        return [], ["https://example.edu/page"]

    def fake_fetch_document(url, timeout=8.0):
        return WebDocument(
            url=url,
            title="Example",
            text=(
                "HTTP — это протокол передачи гипертекста между клиентом и сервером. "
                "Он используется для обмена документами и ресурсами в сети Интернет. "
                "Этот фрагмент достаточно длинный для фильтра web_search_v2."
            ),
            source_weight=1.2,
        )

    monkeypatch.setattr(web_search_v2_module, "search_result_documents", flaky_search_result_documents)
    monkeypatch.setattr(web_search_v2_module, "fetch_document", fake_fetch_document)
    question = PageQuestion(
        text="Что такое HTTP?",
        kind="single_choice",
        options=["Язык программирования", "Протокол передачи гипертекста"],
    )
    documents = web_search_v2_module.search_documents(question, timeout=0.01, max_pages=1, total_timeout=1.0)

    assert calls >= 2
    assert len(documents) == 1


def test_web_search_v2_uses_search_snippets_before_fetching_pages(monkeypatch) -> None:
    body = """
    <html><body>
      <a class="result__a" href="https://example.edu/http">HTTP reference</a>
      <a class="result__snippet">HTTP — это протокол передачи гипертекста между клиентом и сервером.</a>
    </body></html>
    """

    class FakeResponse:
        headers = {"Content-Type": "text/html"}
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, limit=-1):
            return body.encode("utf-8")

    fetch_calls = 0

    def fake_open_url(request, timeout=8.0):
        return FakeResponse()

    def fake_fetch_document(url, timeout=8.0):
        nonlocal fetch_calls
        fetch_calls += 1
        return None

    monkeypatch.setattr(web_search_v2_module, "open_url", fake_open_url)
    monkeypatch.setattr(web_search_v2_module, "fetch_document", fake_fetch_document)
    question = PageQuestion(
        text="Что такое HTTP?",
        kind="single_choice",
        options=["Язык программирования", "Протокол передачи гипертекста"],
    )

    documents = web_search_v2_module.search_documents(question, timeout=0.1, max_pages=1, total_timeout=1.0)
    candidate = answer_from_documents(question, documents)

    assert fetch_calls == 0
    assert candidate is not None
    assert candidate.answers == ["Протокол передачи гипертекста"]


def test_web_search_v2_ignores_invalid_pdf_without_stderr(capsys) -> None:
    assert web_search_v2_module._pdf_text(b"<html>not a pdf</html>") == ""
    captured = capsys.readouterr()
    assert "EOF marker not found" not in captured.err


def test_solver_server_uses_threading_http_server(tmp_path: Path, monkeypatch) -> None:
    captured = {}

    class FakeThreadingHTTPServer:
        def __init__(self, address, handler):
            captured["address"] = address
            captured["handler"] = handler

        def serve_forever(self):
            captured["served"] = True
            raise KeyboardInterrupt

    monkeypatch.setattr(server_module, "ThreadingHTTPServer", FakeThreadingHTTPServer)
    server = SolverServer(
        str(tmp_path / "questions.sqlite"),
        str(tmp_path / "questions.csv"),
        min_confidence=0.62,
    )

    try:
        server.serve("127.0.0.1", 9999)
    except KeyboardInterrupt:
        pass

    assert captured["address"] == ("127.0.0.1", 9999)
    assert captured["handler"].__name__ == "Handler"
    assert captured["served"] is True


def test_solver_server_returns_best_effort_when_no_source(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(server_module, "web_answer", lambda question: None)
    monkeypatch.setattr(server_module, "web_answer_v2", lambda *args, **kwargs: None)
    server = SolverServer(
        str(tmp_path / "questions.sqlite"),
        str(tmp_path / "questions.csv"),
        min_confidence=0.62,
    )
    response = server.answer(
        {
            "questions": [
                {
                    "questionNumber": 12,
                    "text": "Вопрос без найденного источника?",
                    "kind": "single_choice",
                    "options": ["Первый вариант", "Второй вариант"],
                }
            ]
        }
    )
    result = response["results"][0]
    assert response["ok"] is True
    assert result["answers"] == ["Первый вариант"]
    assert result["source"] == "best-effort"
    assert result["status"] == "needs_review"


def test_web_search_v2_scores_answer_from_documents() -> None:
    question = PageQuestion(
        text="Какой документ создаётся после проведения фазы E метода ADM TOGAF?",
        kind="single_choice",
        options=[
            "Архитектурное решение",
            "Стратегия реализации архитектуры",
            "Архитектурное задание",
        ],
    )
    candidate = answer_from_documents(
        question,
        [
            WebDocument(
                url="https://example.edu/togaf",
                title="TOGAF ADM",
                text=(
                    "Фаза E метода ADM TOGAF описывает возможности и решения. "
                    "После этой фазы формируется стратегия реализации архитектуры и план перехода."
                ),
                source_weight=1.2,
            )
        ],
    )
    assert candidate is not None
    assert candidate.answers == ["Стратегия реализации архитектуры"]
    assert candidate.source == "web-search-v2"


def test_web_search_v2_handles_negation() -> None:
    question = PageQuestion(
        text="Является ли HTML языком программирования?",
        kind="single_choice",
        options=["Да, является.", "Нет, не является."],
    )
    candidate = answer_from_documents(
        question,
        [
            WebDocument(
                url="https://example.edu/html",
                title="HTML",
                text="HTML не является языком программирования. HTML является языком разметки документов.",
                source_weight=1.2,
            )
        ],
    )
    assert candidate is not None
    assert candidate.answers == ["Нет, не является."]


def test_solver_server_uses_web_search_v2_before_legacy_web(tmp_path: Path, monkeypatch) -> None:
    def fake_web_answer_v2(*args, **kwargs):
        from test_solver.models import AnswerCandidate

        return AnswerCandidate(
            answers=["Ответ из v2"],
            confidence=0.75,
            source="web-search-v2",
            excerpt="https://example.edu\nfragment",
        )

    monkeypatch.setattr(server_module, "web_answer_v2", fake_web_answer_v2)
    monkeypatch.setattr(server_module, "web_answer", lambda question, timeout=8.0: None)
    server = SolverServer(
        str(tmp_path / "questions.sqlite"),
        str(tmp_path / "questions.csv"),
        min_confidence=0.62,
    )
    response = server.answer(
        {
            "questions": [
                {
                    "questionNumber": 5,
                    "text": "Вопрос только для v2?",
                    "kind": "single_choice",
                    "options": ["Ответ из v2", "Другой ответ"],
                }
            ]
        }
    )
    result = response["results"][0]
    assert result["answers"] == ["Ответ из v2"]
    assert result["source"] == "web-search-v2"
    assert result["status"] == "answered"


def test_web_cache_roundtrip(tmp_path: Path) -> None:
    store = QuestionStore(tmp_path / "questions.sqlite")
    try:
        store.set_web_cache("cache-key", {"answers": ["A"], "confidence": 0.7})
        assert store.get_web_cache("cache-key", ttl_seconds=60) == {"answers": ["A"], "confidence": 0.7}
        assert store.get_web_cache("cache-key", ttl_seconds=-1) is None
    finally:
        store.close()


def test_http_client_uses_certifi_context(monkeypatch) -> None:
    captured = {}

    def fake_create_default_context(*, cafile):
        captured["cafile"] = cafile
        return "context"

    def fake_urlopen(request, timeout, context):
        captured["request"] = request
        captured["timeout"] = timeout
        captured["context"] = context
        return "response"

    monkeypatch.setattr(http_client.ssl, "create_default_context", fake_create_default_context)
    monkeypatch.setattr(http_client, "urlopen", fake_urlopen)

    assert http_client.open_url("https://example.com", timeout=3) == "response"
    assert captured["cafile"] == http_client.certifi.where()
    assert captured["timeout"] == 3
    assert captured["context"] == "context"

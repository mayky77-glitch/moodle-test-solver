from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

from .models import AnswerCandidate, PageQuestion, QuestionStatus
from .normalize import question_key


SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS lectures (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL,
  text TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS questions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key TEXT NOT NULL UNIQUE,
  question TEXT NOT NULL,
  kind TEXT NOT NULL,
  options_json TEXT NOT NULL,
  answer_json TEXT NOT NULL DEFAULT '[]',
  confidence REAL NOT NULL DEFAULT 0,
  status TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT '',
  excerpt TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  question_id INTEGER,
  question_key TEXT NOT NULL,
  action TEXT NOT NULL,
  result TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(question_id) REFERENCES questions(id)
);

CREATE TABLE IF NOT EXISTS web_cache (
  key TEXT PRIMARY KEY,
  payload_json TEXT NOT NULL,
  created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
);

CREATE TABLE IF NOT EXISTS wrong_answers (
  question_key TEXT NOT NULL,
  answer TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(question_key, answer)
);

"""


class QuestionStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)

    def close(self) -> None:
        self.connection.close()

    def add_lecture(self, source: str, title: str, text: str) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO lectures(source, title, text)
            VALUES (?, ?, ?)
            ON CONFLICT(source) DO UPDATE SET
              title = excluded.title,
              text = excluded.text
            RETURNING id
            """,
            (source, title, text),
        )
        row = cursor.fetchone()
        self.connection.commit()
        return int(row["id"])

    def lectures(self) -> list[sqlite3.Row]:
        return list(self.connection.execute("SELECT * FROM lectures ORDER BY id"))

    def upsert_question(
        self,
        page_question: PageQuestion,
        candidate: AnswerCandidate | None,
        status: QuestionStatus,
    ) -> int:
        key = question_key(page_question.text, page_question.options)
        answers = candidate.answers if candidate else []
        confidence = candidate.confidence if candidate else 0.0
        source = candidate.source if candidate else ""
        excerpt = candidate.excerpt if candidate else ""
        cursor = self.connection.execute(
            """
            INSERT INTO questions(
              key, question, kind, options_json, answer_json,
              confidence, status, source, excerpt
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
              question = excluded.question,
              kind = excluded.kind,
              options_json = excluded.options_json,
              answer_json = excluded.answer_json,
              confidence = excluded.confidence,
              status = excluded.status,
              source = excluded.source,
              excerpt = excluded.excerpt,
              updated_at = CURRENT_TIMESTAMP
            RETURNING id
            """,
            (
                key,
                page_question.text,
                page_question.kind,
                json.dumps(page_question.options, ensure_ascii=False),
                json.dumps(answers, ensure_ascii=False),
                confidence,
                status,
                source,
                excerpt,
            ),
        )
        row = cursor.fetchone()
        self.connection.commit()
        return int(row["id"])

    def upsert_correct_question(
        self,
        page_question: PageQuestion,
        answers: list[str],
        source: str = "moodle-review",
        excerpt: str = "",
    ) -> int:
        return self.upsert_question(
            page_question,
            AnswerCandidate(answers=answers, confidence=1.0, source=source, excerpt=excerpt),
            "answered",
        )

    def find_known_answer(self, page_question: PageQuestion) -> AnswerCandidate | None:
        key = question_key(page_question.text, page_question.options)
        row = self.connection.execute(
            """
            SELECT answer_json, confidence, source, excerpt
            FROM questions
            WHERE key = ? AND status = 'answered'
            """,
            (key,),
        ).fetchone()
        if not row:
            return None
        answers = json.loads(row["answer_json"])
        if not answers:
            return None
        return AnswerCandidate(
            answers=answers,
            confidence=float(row["confidence"]),
            source=row["source"],
            excerpt=row["excerpt"],
        )

    def add_wrong_answers(self, page_question: PageQuestion, answers: list[str]) -> None:
        key = question_key(page_question.text, page_question.options)
        rows = [(key, answer) for answer in dict.fromkeys(answers) if answer.strip()]
        if not rows:
            return
        self.connection.executemany(
            """
            INSERT OR IGNORE INTO wrong_answers(question_key, answer)
            VALUES (?, ?)
            """,
            rows,
        )
        self.connection.commit()

    def find_wrong_answers(self, page_question: PageQuestion) -> list[str]:
        key = question_key(page_question.text, page_question.options)
        rows = self.connection.execute(
            """
            SELECT answer
            FROM wrong_answers
            WHERE question_key = ?
            ORDER BY created_at DESC
            """,
            (key,),
        )
        return [str(row["answer"]) for row in rows]

    def record_attempt(self, question_id: int | None, page_question: PageQuestion, action: str, result: str) -> None:
        self.connection.execute(
            """
            INSERT INTO attempts(question_id, question_key, action, result)
            VALUES (?, ?, ?, ?)
            """,
            (question_id, question_key(page_question.text, page_question.options), action, result),
        )
        self.connection.commit()

    def get_web_cache(self, key: str, ttl_seconds: int) -> dict | None:
        row = self.connection.execute(
            """
            SELECT payload_json
            FROM web_cache
            WHERE key = ? AND created_at >= strftime('%s', 'now') - ?
            """,
            (key, ttl_seconds),
        ).fetchone()
        if not row:
            return None
        return json.loads(row["payload_json"])

    def set_web_cache(self, key: str, payload: dict) -> None:
        self.connection.execute(
            """
            INSERT INTO web_cache(key, payload_json, created_at)
            VALUES (?, ?, strftime('%s', 'now'))
            ON CONFLICT(key) DO UPDATE SET
              payload_json = excluded.payload_json,
              created_at = excluded.created_at
            """,
            (key, json.dumps(payload, ensure_ascii=False)),
        )
        self.connection.commit()

    def stats(self) -> dict[str, int]:
        row = self.connection.execute(
            """
            SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN status = 'answered' THEN 1 ELSE 0 END) AS answered,
              SUM(CASE WHEN status = 'needs_review' THEN 1 ELSE 0 END) AS needs_review,
              SUM(CASE WHEN source = 'moodle-review' THEN 1 ELSE 0 END) AS moodle_review,
              SUM(CASE WHEN source = 'best-effort' THEN 1 ELSE 0 END) AS best_effort
            FROM questions
            """
        ).fetchone()
        wrong_row = self.connection.execute("SELECT COUNT(*) AS total FROM wrong_answers").fetchone()
        attempts_row = self.connection.execute("SELECT COUNT(*) AS total FROM attempts").fetchone()
        return {
            "total": int(row["total"] or 0),
            "answered": int(row["answered"] or 0),
            "needsReview": int(row["needs_review"] or 0),
            "moodleReview": int(row["moodle_review"] or 0),
            "bestEffort": int(row["best_effort"] or 0),
            "wrongAnswers": int(wrong_row["total"] or 0),
            "attempts": int(attempts_row["total"] or 0),
        }

    def export_csv(self, csv_path: str | Path) -> None:
        target = Path(csv_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        rows = self.connection.execute(
            """
            SELECT key, question, kind, options_json, answer_json,
                   confidence, status, source, excerpt, updated_at
            FROM questions
            ORDER BY updated_at DESC, id DESC
            """
        )
        with target.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                [
                    "key",
                    "question",
                    "kind",
                    "options",
                    "answers",
                    "confidence",
                    "status",
                    "source",
                    "excerpt",
                    "updated_at",
                ]
            )
            for row in rows:
                writer.writerow([row[column] for column in row.keys()])

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Callable
from urllib.request import urlopen

from .adapter import read_question, read_questions
from .answer_engine import AnswerEngine
from .lecture_index import clean_page_text
from .models import AnswerCandidate, PageQuestion
from .normalize import question_key
from .storage import QuestionStore


class BrowserRunner:
    def __init__(
        self,
        remote_url: str,
        store: QuestionStore,
        engine: AnswerEngine,
        allow_clicks: bool = False,
        max_steps: int = 1,
        page_url: str | None = None,
    ) -> None:
        self.remote_url = remote_url
        self.store = store
        self.engine = engine
        self.allow_clicks = allow_clicks
        self.max_steps = max_steps
        self.page_url = page_url

    async def run(self, diagnose_only: bool = False) -> list[dict[str, object]]:
        from playwright.async_api import async_playwright

        results: list[dict[str, object]] = []
        async with async_playwright() as playwright:
            browser = await playwright.chromium.connect_over_cdp(self.remote_url)
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = await self._select_page(context)
            for _ in range(self.max_steps):
                question = await read_question(page)
                await self._store_page_lecture(page)
                result, question_id, candidate = self._process_question(page.url, question, diagnose_only)
                results.append(result)
                if diagnose_only:
                    self.store.record_attempt(question_id, question, "diagnose", "saved")
                    break
                if not self.allow_clicks:
                    self.store.record_attempt(question_id, question, "dry_run", status)
                    break
                result = await self._apply_answer(page, question, candidate)
                self.store.record_attempt(question_id, question, "click", result)
                if question.next_selector:
                    await page.click(question.next_selector)
                    await page.wait_for_load_state("domcontentloaded")
                else:
                    break
            await browser.close()
        return results

    async def watch(
        self,
        interval: float = 1.0,
        max_seconds: float = 0,
        on_result: Callable[[dict[str, object]], None] | None = None,
    ) -> list[dict[str, object]]:
        from playwright.async_api import async_playwright

        started_at = time.monotonic()
        seen_keys: set[str] = set()
        results: list[dict[str, object]] = []
        async with async_playwright() as playwright:
            browser = await playwright.chromium.connect_over_cdp(self.remote_url)
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            while True:
                page = await self._select_page(context)
                await self._store_page_lecture(page)
                questions = await read_questions(page)
                for question in questions:
                    key = f"{page.url}:{question_key(question.text, question.options)}"
                    if key in seen_keys:
                        continue
                    result, question_id, candidate = self._process_question(page.url, question, diagnose_only=False)
                    self.store.record_attempt(question_id, question, "watch", result["status"])
                    results.append(result)
                    if on_result:
                        on_result(result)
                    seen_keys.add(key)
                    if self.allow_clicks:
                        click_result = await self._apply_answer(page, question, candidate)
                        self.store.record_attempt(question_id, question, "watch_click", click_result)
                if max_seconds and time.monotonic() - started_at >= max_seconds:
                    break
                await asyncio.sleep(interval)
            await browser.close()
        return results

    async def list_pages(self) -> list[dict[str, str]]:
        endpoint = self.remote_url.rstrip("/") + "/json/list"
        with urlopen(endpoint, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return [
            {"index": str(index), "title": item.get("title", ""), "url": item.get("url", "")}
            for index, item in enumerate(payload, start=1)
            if item.get("type") == "page"
        ]

    async def _select_page(self, context):
        if not context.pages:
            return await context.new_page()
        if self.page_url:
            for page in reversed(context.pages):
                if self.page_url in page.url:
                    return page
        return context.pages[-1]

    def _process_question(
        self,
        url: str,
        question: PageQuestion,
        diagnose_only: bool,
    ) -> tuple[dict[str, object], int, AnswerCandidate | None]:
        candidate = self.engine.answer(question)
        status = "diagnosed" if diagnose_only else self._status_for(candidate)
        question_id = self.store.upsert_question(question, candidate, status)
        return (
            {
                "url": url,
                "question": question.text,
                "kind": question.kind,
                "options": question.options,
                "answers": candidate.answers if candidate else [],
                "confidence": candidate.confidence if candidate else 0.0,
                "status": status,
                "source": candidate.source if candidate else "",
            },
            question_id,
            candidate,
        )

    async def _store_page_lecture(self, page) -> None:
        title = await page.title()
        content = await page.content()
        text = clean_page_text(content)
        if len(text) > 200:
            self.store.add_lecture(f"page:{page.url}", title or page.url, text)

    def _status_for(self, candidate: AnswerCandidate | None) -> str:
        if not candidate or not candidate.answers:
            return "needs_review"
        if candidate.confidence < self.engine.min_confidence:
            return "needs_review"
        return "answered"

    async def _apply_answer(self, page, question: PageQuestion, candidate: AnswerCandidate | None) -> str:
        if not candidate or not candidate.answers or candidate.confidence < self.engine.min_confidence:
            return "skipped_low_confidence"
        if question.kind in {"single_choice", "multiple_choice"}:
            clicked = 0
            for answer in candidate.answers:
                selector = question.option_selectors.get(answer)
                if selector:
                    await page.click(selector)
                    clicked += 1
            if not clicked:
                return "skipped_no_selector"
        elif question.kind == "text" and question.input_selector:
            await page.fill(question.input_selector, candidate.answers[0])
        else:
            return "skipped_unknown_type"

        if question.submit_selector:
            await page.click(question.submit_selector)
            await page.wait_for_load_state("domcontentloaded")
            return "submitted"
        return "selected_without_submit"


def ensure_parent(path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from .answer_engine import AnswerEngine
from .browser import BrowserRunner
from .chrome import launch_chrome
from .lecture_index import LectureIndex, ingest_paths
from .server import SolverServer
from .storage import QuestionStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="test-solver")
    parser.add_argument("--db", default="data/questions.sqlite", help="Path to SQLite database")
    parser.add_argument("--csv", default="data/questions.csv", help="Path to CSV export")
    parser.add_argument("--lecture-db", default="data/lectures.sqlite", help="Shared SQLite database for lecture imports")
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest = subparsers.add_parser("ingest", help="Import lecture files")
    ingest.add_argument("paths", nargs="+", help="Lecture files or directories")

    chrome = subparsers.add_parser("chrome", help="Launch Chrome with remote debugging")
    chrome.add_argument("--profile", default=".chrome-profile", help="Chrome profile directory")
    chrome.add_argument("--port", type=int, default=9222, help="Remote debugging port")
    chrome.add_argument(
        "--default-profile",
        action="store_true",
        help="Use the normal Chrome profile instead of an isolated profile; Chrome must be fully closed first",
    )

    diagnose = subparsers.add_parser("diagnose", help="Read current page without clicking")
    diagnose.add_argument("--url", default="http://127.0.0.1:9222", help="Chrome CDP URL")
    diagnose.add_argument("--page-url", help="Use tab whose URL contains this text")

    run = subparsers.add_parser("run", help="Answer recognized questions")
    run.add_argument("--url", default="http://127.0.0.1:9222", help="Chrome CDP URL")
    run.add_argument("--page-url", help="Use tab whose URL contains this text")
    run.add_argument("--allow-clicks", action="store_true", help="Actually click/fill answers")
    run.add_argument("--max-steps", type=int, default=1, help="Maximum questions to process")
    run.add_argument("--min-confidence", type=float, default=0.62, help="Minimum confidence for answering")

    watch = subparsers.add_parser("watch", help="Watch current Chrome tab in realtime")
    watch.add_argument("--url", default="http://127.0.0.1:9222", help="Chrome CDP URL")
    watch.add_argument("--page-url", help="Use tab whose URL contains this text")
    watch.add_argument("--allow-clicks", action="store_true", help="Actually click/fill answers")
    watch.add_argument("--interval", type=float, default=1.0, help="Polling interval in seconds")
    watch.add_argument("--max-seconds", type=float, default=0, help="Stop after N seconds; 0 means run until Ctrl+C")
    watch.add_argument("--min-confidence", type=float, default=0.62, help="Minimum confidence for answering")

    pages = subparsers.add_parser("pages", help="List Chrome tabs visible over CDP")
    pages.add_argument("--url", default="http://127.0.0.1:9222", help="Chrome CDP URL")

    serve = subparsers.add_parser("serve", help="Run local backend for Chrome extension")
    serve.add_argument("--host", default="127.0.0.1", help="HTTP host")
    serve.add_argument("--port", type=int, default=8765, help="HTTP port")
    serve.add_argument("--min-confidence", type=float, default=0.62, help="Minimum confidence for answering")
    serve.add_argument("--web-search", action=argparse.BooleanOptionalAction, default=True, help="Enable web search fallback")
    serve.add_argument("--web-timeout", type=float, default=3.0, help="Timeout per web request in seconds")
    serve.add_argument("--web-max-pages", type=int, default=2, help="Maximum web pages to inspect per question")
    serve.add_argument("--web-total-timeout", type=float, default=6.0, help="Total web search budget per question in seconds")
    serve.add_argument("--web-cache-ttl", type=int, default=86400, help="Web answer cache TTL in seconds")
    serve.add_argument("--web-negative-cache-ttl", type=int, default=600, help="Negative web result cache TTL in seconds")

    subparsers.add_parser("export", help="Export questions to CSV")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "chrome":
        process = launch_chrome(args.profile, args.port, use_default_profile=args.default_profile)
        if args.default_profile:
            print(f"Chrome started with PID {process.pid} using the default profile.")
            print("If tabs do not appear, quit Chrome completely and run this command again.")
        else:
            print(f"Chrome started with PID {process.pid}. Open the test in that window.")
        return 0
    if args.command == "serve":
        SolverServer(
            args.db,
            args.csv,
            args.min_confidence,
            web_search_enabled=args.web_search,
            web_timeout=args.web_timeout,
            web_max_pages=args.web_max_pages,
            web_cache_ttl=args.web_cache_ttl,
            web_total_timeout=args.web_total_timeout,
            web_negative_cache_ttl=args.web_negative_cache_ttl,
            lecture_db_path=args.lecture_db,
        ).serve(args.host, args.port)
        return 0

    store_path = args.lecture_db if args.command == "ingest" else args.db
    csv_path = args.csv
    store = QuestionStore(store_path)
    try:
        if args.command == "ingest":
            count = ingest_paths(store, args.paths)
            store.export_csv(csv_path)
            print(f"Imported {count} lecture file(s).")
            return 0
        if args.command == "export":
            store.export_csv(args.csv)
            print(f"Exported questions to {Path(args.csv).resolve()}.")
            return 0
        if args.command in {"diagnose", "run", "watch", "pages"}:
            index = LectureIndex(store)
            engine = AnswerEngine(store, index, min_confidence=getattr(args, "min_confidence", 0.62))
            runner = BrowserRunner(
                args.url,
                store,
                engine,
                allow_clicks=getattr(args, "allow_clicks", False),
                max_steps=getattr(args, "max_steps", 1),
                page_url=getattr(args, "page_url", None),
            )
            try:
                if args.command == "pages":
                    pages = asyncio.run(runner.list_pages())
                    _print_pages(pages)
                    return 0
                if args.command == "watch":
                    print("Watching Chrome tab. Press Ctrl+C to stop.")
                    results = asyncio.run(
                        runner.watch(
                            interval=args.interval,
                            max_seconds=args.max_seconds,
                            on_result=lambda result: _print_and_export(result, store, args.csv),
                        )
                    )
                else:
                    results = asyncio.run(runner.run(diagnose_only=args.command == "diagnose"))
            except KeyboardInterrupt:
                store.export_csv(args.csv)
                print(f"\nwatch stopped. CSV: {Path(args.csv).resolve()}")
                return 0
            except Exception as error:
                print(f"Cannot connect to Chrome at {args.url}: {error}")
                print("Start Chrome with: test-solver chrome --profile .chrome-profile --port 9222")
                return 2
            store.export_csv(args.csv)
            if args.command != "watch":
                _print_results(results)
            print(f"{args.command} complete. CSV: {Path(args.csv).resolve()}")
            return 0
    finally:
        store.close()
    return 1


def _print_results(results: list[dict[str, object]]) -> None:
    if not results:
        print("No question detected.")
        return
    for index, result in enumerate(results, start=1):
        print(f"\nQuestion #{index}")
        print(f"URL: {result['url']}")
        print(f"Kind: {result['kind']}")
        print(f"Status: {result['status']}")
        print(f"Question: {result['question'] or '[empty]'}")
        options = result["options"] or []
        if options:
            print("Options:")
            for option in options:
                print(f"- {option}")
        answers = result["answers"] or []
        if answers:
            print("Answer:")
            for answer in answers:
                print(f"- {answer}")
            print(f"Confidence: {result['confidence']}")
            print(f"Source: {result['source']}")


def _print_and_export(result: dict[str, object], store: QuestionStore, csv_path: str) -> None:
    _print_results([result])
    store.export_csv(csv_path)


def _print_pages(pages: list[dict[str, str]]) -> None:
    if not pages:
        print("No Chrome tabs found.")
        print("If the test is open in normal Chrome, quit Chrome and restart it with:")
        print("test-solver chrome --default-profile --port 9222")
        return
    for page in pages:
        print(f"[{page['index']}] {page['title']}")
        print(f"    {page['url']}")


if __name__ == "__main__":
    raise SystemExit(main())

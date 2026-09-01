"""Command-line interface: one-shot question or an interactive loop."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .agent import HarbourviewAgent


def _load_env() -> None:
    """Best-effort load of a local .env (API key, model). Optional dependency."""
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass


def _default_root() -> str:
    """Repo root works whether run from the checkout or elsewhere."""
    here = Path(__file__).resolve().parent.parent
    if (here / "data").is_dir():
        return str(here)
    return os.getcwd()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Harbourview Lookout operations assistant",
    )
    parser.add_argument("-q", "--question", help="Ask one question and exit.")
    parser.add_argument("--root-dir", default=_default_root(),
                        help="Directory containing data/ and regulations/ (default: repo root).")
    parser.add_argument("--model", default=None,
                        help="Anthropic model id (default: $HARBOURVIEW_MODEL or claude-sonnet-5).")
    parser.add_argument("--no-llm", action="store_true",
                        help="Force the offline deterministic router even if an API key is set.")
    parser.add_argument("--show-data", action="store_true",
                        help="Print the structured tool result alongside the answer.")
    parser.add_argument("--sql", metavar="QUERY",
                        help="Run a read-only SQL query against the in-memory DB and exit.")
    parser.add_argument("--export-db", metavar="PATH",
                        help="Write the cleaned data to a SQLite file and exit.")
    parser.add_argument("--eval", action="store_true",
                        help="Run the golden-fact answer-quality check and exit.")
    return parser.parse_args(argv)


def _print_answer(result: dict, show_data: bool) -> None:
    print(f"\nAnswer ({result.get('mode', '?')} mode):")
    print(result["answer"])
    if result.get("evidence"):
        print("\nEvidence:")
        for item in result["evidence"]:
            print(f"  - {item['source']}: {item['summary']}")
    if result.get("llm_error"):
        print(f"\n[note] LLM call failed, used deterministic fallback: {result['llm_error']}")
    if show_data and result.get("result") is not None:
        import json

        print("\nStructured result:")
        print(json.dumps(result["result"], indent=2, default=str))


def interactive_loop(agent: HarbourviewAgent, show_data: bool) -> None:
    print(f"Harbourview Lookout assistant ({agent.mode} mode). Type 'exit' to quit.")
    print("Try:")
    for sample in agent.list_sample_questions():
        print(f"  - {sample}")
    while True:
        try:
            question = input("\nAsk a question: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            return
        if question.lower() in {"exit", "quit", "q"}:
            print("Goodbye.")
            return
        if not question:
            continue
        try:
            _print_answer(agent.answer_question(question), show_data)
        except Exception as exc:  # keep the loop alive on any single-question failure
            print(f"Sorry, that question failed: {exc!r}")


def main(argv: list[str] | None = None) -> int:
    _load_env()
    args = parse_args(argv)
    if not Path(args.root_dir, "data").is_dir():
        print(f"error: {args.root_dir!r} has no data/ folder; pass --root-dir.", file=sys.stderr)
        return 2

    if args.export_db or args.sql:
        from .data_store import DataStore

        store = DataStore(args.root_dir)
        if args.export_db:
            print(f"wrote {store.export_sqlite(args.export_db)}")
        if args.sql:
            rows = store.query(args.sql)
            if rows:
                print(" | ".join(rows[0].keys()))
                for row in rows:
                    print(" | ".join(str(v) for v in row))
            print(f"({len(rows)} row{'s' if len(rows) != 1 else ''})")
        return 0

    agent = HarbourviewAgent(
        args.root_dir,
        use_llm=False if args.no_llm else None,
        model=args.model,
    )
    if args.eval:
        from .eval import format_report, run

        results = run(agent, pause_s=4.0)
        print(format_report(results))
        return 0 if all(r.passed for r in results) else 1
    if args.question:
        _print_answer(agent.answer_question(args.question), args.show_data)
        return 0
    interactive_loop(agent, args.show_data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

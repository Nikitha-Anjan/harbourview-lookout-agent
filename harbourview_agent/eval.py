"""Answer-quality evaluation.

A small golden-fact set: each case lists facts that MUST appear in the answer
and facts that must NOT. Runs against whichever mode the agent is in.

- Deterministic mode: strict - the templated answer is fully predictable.
- LLM mode: entity-level - the model's phrasing varies, so we check the key
  facts are present and that no invented number appears (every >=4-digit number
  in the answer must also occur in the deterministic ground truth for that
  question).

Run it:  python -m harbourview_agent.cli --eval          (mode depends on API key)
         python -m harbourview_agent.cli --eval --no-llm  (force deterministic)
Or via pytest: tests/test_eval.py
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .agent import HarbourviewAgent
from .router import DeterministicRouter

# ----------------------------------------------------------------------
# Golden cases. `must_include` applies to every mode; `det_only` adds
# stricter checks that only make sense for the deterministic template.
# ----------------------------------------------------------------------
CASES: list[dict] = [
    {
        "question": "What was our best-selling ticket type last month and how did weather affect it?",
        "expect_intent": "best_seller",
        "must_include": ["family", "3793"],
        "must_include_any": ["weather", "rain", "adverse", "fair"],
        "must_not_include": [],
        "det_only": ["31%", "lower"],
    },
    {
        "question": "Are we ever exceeding our fire-code capacity? On which days and hours?",
        "expect_intent": "capacity",
        "must_include": ["deck", "2025-08-03"],
        "must_include_any": ["15:00", "h15", "3pm", "15 "],
        "must_not_include": [],
        "det_only": ["8 day", "2025-08-12", "349"],
    },
    {
        "question": "A customer wants a refund for a rained-out visit - what does policy allow?",
        "expect_intent": "refund_policy",
        "must_include": ["full refund", "30 day"],
        "must_include_any": ["re-entry", "reentry", "re entry"],
        "must_not_include": ["non-refundable visit"],
        "det_only": ["3.1", "50%"],
    },
    {
        "question": "Recommend staffing for next weekend given the forecast and typical traffic.",
        "expect_intent": "staffing",
        "must_include": ["2025-09-06", "staff"],
        "must_include_any": ["150", "attendant", "monitor"],
        "must_not_include": [],
        "det_only": ["2025-09-07"],
    },
    {
        "question": "Which days had high traffic but low ticket revenue, and why might that be?",
        "expect_intent": "traffic_revenue",
        "must_include": ["2025-06-04"],
        "must_include_any": ["storm", "rain", "weather"],
        "must_not_include": [],
        "det_only": ["16,819", "1,844"],
    },
    {
        "question": "How many hot dogs did we sell?",
        "expect_intent": "general",
        "must_include": [],
        "must_include_any": ["don't have", "do not have", "no data", "cannot", "can't answer",
                             "outside", "not available", "only answer", "four provided", "don't track"],
        "must_not_include": [],
        "det_only": [],
    },
    {
        "question": "What are our opening hours in peak season?",
        "expect_intent": "regulation",
        "must_include": [],
        "must_include_any": ["09:00", "9:00", "21:00", "9 am", "9am", "08:00", "22:00"],
        "must_not_include": [],
        "det_only": [],
    },
]

_NUM = re.compile(r"\d[\d,]{3,}(?:\.\d+)?")


def _norm(text: str) -> str:
    return re.sub(r"[,–—]", lambda m: "" if m.group() == "," else "-", text.lower())


@dataclass
class CaseResult:
    question: str
    mode: str
    passed: bool
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _numbers(text: str) -> set[str]:
    return {m.group().replace(",", "").rstrip(".") for m in _NUM.finditer(text)}


def grade(case: dict, result: dict, ground_truth_numbers: set[str]) -> CaseResult:
    mode = result.get("mode", "?")
    answer = _norm(result.get("answer", ""))
    failures: list[str] = []
    warnings: list[str] = []

    for needle in case["must_include"]:
        if _norm(needle) not in answer:
            failures.append(f"missing required phrase: {needle!r}")
    if case["must_include_any"] and not any(_norm(n) in answer for n in case["must_include_any"]):
        failures.append(f"none of the expected phrases present: {case['must_include_any']}")
    for needle in case["must_not_include"]:
        if _norm(needle) in answer:
            failures.append(f"contains forbidden phrase: {needle!r}")
    if mode == "deterministic":
        for needle in case.get("det_only", []):
            if _norm(needle) not in answer:
                failures.append(f"[det] missing {needle!r}")
    if result.get("intent") and result["intent"] != case["expect_intent"]:
        warnings.append(f"intent was {result['intent']!r}, expected {case['expect_intent']!r}")

    if mode == "llm":
        invented = _numbers(result.get("answer", "")) - ground_truth_numbers
        # allow well-known regulatory / calendar constants
        invented -= {"2025", "2024", "1500", "1000"}
        if invented:
            warnings.append(f"numbers not found in ground truth (possible hallucination): {sorted(invented)}")

    return CaseResult(case["question"], mode, not failures, failures, warnings)


def run(agent: HarbourviewAgent, pause_s: float = 0.0) -> list[CaseResult]:
    """Grade every case against `agent`. `pause_s` spaces LLM calls to stay
    under a low API rate limit (the CLI passes a couple of seconds)."""
    import time

    # deterministic answers + regulation text = the numeric ground truth
    det = DeterministicRouter(agent.data, agent.regulations)
    results: list[CaseResult] = []
    for i, case in enumerate(CASES):
        if i and pause_s and agent.mode == "llm":
            time.sleep(pause_s)
        det_answer = det.answer(case["question"])
        gt_numbers = _numbers(json.dumps(det_answer, default=str))
        for hit in agent.regulations.search(case["question"]):
            gt_numbers |= _numbers(" ".join(hit.get("sections", [])))
        results.append(grade(case, agent.answer_question(case["question"]), gt_numbers))
    return results


def format_report(results: list[CaseResult]) -> str:
    lines = []
    passed = sum(r.passed for r in results)
    for r in results:
        mark = "PASS" if r.passed else "FAIL"
        lines.append(f"[{mark}] ({r.mode}) {r.question}")
        for f in r.failures:
            lines.append(f"        - {f}")
        for w in r.warnings:
            lines.append(f"        ~ {w}")
    lines.append(f"\n{passed}/{len(results)} cases passed")
    return "\n".join(lines)

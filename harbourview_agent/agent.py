"""Public entrypoint: :class:`HarbourviewAgent`.

Question -> answer. Two execution modes behind one interface:

* **llm**  - a Pydantic AI agent (Claude) that answers by calling the
  deterministic data/regulation tools. Used when an ``ANTHROPIC_API_KEY`` is
  available and ``pydantic-ai`` is installed.
* **deterministic** - a keyword router that calls the same underlying methods.
  Always available, needs no API key, and is what the test-suite pins.

Both modes return the same dict shape::

    {"answer": str, "evidence": [{"source": str, "summary": str}, ...],
     "mode": "llm" | "deterministic", "intent": str}
"""

from __future__ import annotations

import os
from typing import Any

from .data_store import DataStore
from .regulation_store import RegulationStore
from .router import DeterministicRouter

_SAMPLE_QUESTIONS = [
    "What was our best-selling ticket type last month and how did weather affect it?",
    "Are we ever exceeding our fire-code capacity? On which days/hours?",
    "A customer wants a refund for a rained-out visit - what does policy allow?",
    "Recommend staffing for next weekend given the forecast and typical traffic.",
    "Which days had high traffic but low ticket revenue, and why might that be?",
]


class HarbourviewAgent:
    def __init__(
        self,
        root_dir: str,
        *,
        use_llm: bool | None = None,
        model: str | None = None,
        default_month: str = "2025-08",
    ):
        self.root_dir = root_dir
        self.data = DataStore(root_dir)
        self.regulations = RegulationStore(f"{root_dir}/regulations")
        self.router = DeterministicRouter(self.data, self.regulations)
        self.default_month = default_month

        self._llm = None
        self._llm_deps = None
        if use_llm is None:
            use_llm = bool(os.environ.get("ANTHROPIC_API_KEY"))
        if use_llm:
            self._try_init_llm(model)

    # ------------------------------------------------------------------
    def _try_init_llm(self, model: str | None) -> None:
        try:
            from .llm_agent import DEFAULT_MODEL, Deps, build_agent

            self._llm = build_agent(model or DEFAULT_MODEL)
            self._llm_deps = Deps(data=self.data, regulations=self.regulations)
        except Exception as exc:  # missing dep / missing key / bad model - degrade gracefully
            self._llm = None
            self._llm_init_error = repr(exc)

    @property
    def mode(self) -> str:
        return "llm" if self._llm is not None else "deterministic"

    # ------------------------------------------------------------------
    def answer_question(self, question: str) -> dict[str, Any]:
        question = (question or "").strip()
        if not question:
            return {"answer": "Please ask a question.", "evidence": [], "mode": self.mode,
                    "intent": "empty"}

        if self._llm is not None:
            try:
                from .llm_agent import run_agent

                result = run_agent(self._llm, question, self._llm_deps)
                return {
                    "answer": result.answer,
                    "evidence": [{"source": e.source, "summary": e.summary} for e in result.evidence],
                    "mode": "llm",
                    "intent": self.router.detect_intent(question),
                }
            except Exception as exc:
                # Never fail the user because the model call failed - fall back.
                fallback = self.router.answer(question, self.default_month)
                fallback["mode"] = "deterministic"
                fallback["llm_error"] = repr(exc)
                return fallback

        return self.router.answer(question, self.default_month)

    # ------------------------------------------------------------------
    def list_sample_questions(self) -> list[str]:
        return list(_SAMPLE_QUESTIONS)

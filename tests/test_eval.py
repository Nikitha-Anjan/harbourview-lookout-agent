"""Golden-fact answer-quality checks (see harbourview_agent/eval.py)."""

import os

import pytest

from harbourview_agent.eval import CASES, run


@pytest.fixture(scope="module")
def det_results(root_dir):
    from harbourview_agent.agent import HarbourviewAgent

    return run(HarbourviewAgent(root_dir, use_llm=False))


@pytest.mark.parametrize("idx", range(len(CASES)), ids=[c["question"][:40] for c in CASES])
def test_eval_deterministic(det_results, idx):
    r = det_results[idx]
    assert r.passed, "\n".join(r.failures)


@pytest.mark.llm
@pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="needs ANTHROPIC_API_KEY")
def test_eval_llm(root_dir):
    """The LLM path stays grounded. Paced to be gentle on low API rate limits;
    questions that still get 429'd fall back to the (correct) deterministic
    answer, so we only require that the LLM path was genuinely exercised."""
    from harbourview_agent.agent import HarbourviewAgent

    results = run(HarbourviewAgent(root_dir, use_llm=True), pause_s=5.0)
    failed = [r for r in results if not r.passed]
    assert not failed, "\n\n".join(f"{r.question}\n  " + "\n  ".join(r.failures) for r in failed)
    llm_ran = sum(r.mode == "llm" for r in results)
    assert llm_ran >= 2, f"LLM path barely exercised ({llm_ran}/{len(results)}) - rate limited?"

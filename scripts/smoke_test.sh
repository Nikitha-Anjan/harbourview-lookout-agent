#!/usr/bin/env bash
#
# Manual smoke test for the Harbourview agent.
#
#   bash scripts/smoke_test.sh          # deterministic mode only (free, no API key)
#   bash scripts/smoke_test.sh --llm    # also run every question through the LLM (costs a few cents)
#
# Deterministic answers are the ground truth; the --llm pass is for eyeballing
# that the model's phrasing stays faithful to those numbers.

set -u
cd "$(dirname "$0")/.."

# --- pick an interpreter: prefer the project venv -------------------------
if [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PY="python3"
else
  echo "no python found"; exit 1
fi
echo "using: $PY"

# make ANTHROPIC_API_KEY from .env visible to this shell (for the --llm gate)
[ -f .env ] && set -a && . ./.env && set +a || true

RUN_LLM=0
[ "${1:-}" = "--llm" ] && RUN_LLM=1

ask() {  # ask "<question>"  -> deterministic, then LLM if enabled
  echo
  echo "========================================================================"
  echo "Q: $1"
  echo "------------------------------------------------------------------------"
  "$PY" -m harbourview_agent.cli --no-llm -q "$1"
  if [ "$RUN_LLM" = "1" ] && [ -n "${ANTHROPIC_API_KEY:-}" ]; then
    echo "------------------------------- LLM ------------------------------------"
    "$PY" -m harbourview_agent.cli -q "$1"
    sleep 3   # stay under a low API rate limit
  fi
}

sql() {
  echo
  echo ">>> SQL: $1"
  "$PY" -m harbourview_agent.cli --sql "$1"
}

# ======================================================================
echo; echo "### 0. Unit tests (the numeric ground truth)"
"$PY" -m pytest -q || { echo "TESTS FAILED"; exit 1; }

# ======================================================================
echo; echo "### 1. The five brief questions"
ask "What was our best-selling ticket type last month and how did weather affect it?"
ask "Are we ever exceeding our fire-code capacity? On which days and hours?"
ask "A customer wants a refund for a rained-out visit - what does policy allow?"
ask "Recommend staffing for next weekend given the forecast and typical traffic."
ask "Which days had high traffic but low ticket revenue, and why might that be?"

# ======================================================================
echo; echo "### 2. Phrasing variety (intent routing / tool selection)"
ask "How many staff do I need on Saturday?"
ask "Did any rainy days hurt our ticket sales?"
ask "What is our refund rule if there is a storm?"
ask "Show me the top ticket type for 2025-07"

# ======================================================================
echo; echo "### 3. Bad / ambiguous / out-of-scope (must NOT hallucinate)"
ask "What will the weather be tomorrow?"
ask "How many hot dogs did we sell?"
ask "asdfjkl ??? "
ask "Best seller for 2024-01"
ask "Recommend staffing for the 32nd of Novembary"

# ======================================================================
echo; echo "### 4. Regulation coverage (all four reg files)"
ask "What are our opening hours in peak season?"
ask "Do we allow service animals inside?"
ask "How many elevators must be working?"
ask "When must we pause entry to a zone?"

# ======================================================================
echo; echo "### 5. SQLite layer"
sql "SELECT COUNT(*) AS ticket_rows FROM ticket_sales"
sql "SELECT date, hour, deck FROM occupancy WHERE deck > 450 ORDER BY deck DESC"
sql "SELECT ticket_type, SUM(quantity) g FROM ticket_sales WHERE date LIKE '2025-08%' GROUP BY ticket_type ORDER BY g DESC"
"$PY" -m harbourview_agent.cli --export-db /tmp/harbourview_smoke.db && echo "export OK" && rm -f /tmp/harbourview_smoke.db

# ======================================================================
echo; echo "### 6. Fallback: bad key must degrade to deterministic, not crash"
ANTHROPIC_API_KEY="sk-ant-invalid" "$PY" -m harbourview_agent.cli -q "capacity incidents?" 2>&1 | tail -n 6

echo
echo "### done."
[ "$RUN_LLM" = "0" ] && echo "(re-run with --llm to also exercise the Claude path)"

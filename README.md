# Harbourview Lookout — AI Ops Assistant

A grounded question-answering agent for the Harbourview Lookout admin office.
Staff ask plain-English questions; the agent answers from the provided CSVs and
regulation documents, with an evidence trail for every answer.

## How it works

```
question
   │
   ▼
HarbourviewAgent ──► llm mode:  Pydantic AI agent (Claude) calls tools
   │                            (needs ANTHROPIC_API_KEY)
   │           └──► deterministic mode: keyword router calls the same tools
   │                            (offline, no key, always available)
   ▼
tools ─► DataStore          (clean data/*.csv in Python → in-memory SQLite → SQL metrics)
      └─ RegulationStore    (keyword search over regulations/*.md)
```

The CSVs are cleaned in Python (dedupe, price imputation, casing, missing
values) and loaded into an **in-memory SQLite database**; every metric is then a
short SQL query. Nothing is written to disk. You can point other tools at the
data with `--export-db` / `--sql` (see below).

The LLM never sees the raw data. It only sees the question and a set of tools
(`best_seller_for_month`, `capacity_report`, `staffing_recommendation_for_weekend`,
`weather_impact_on_ticket_type`, `high_traffic_low_revenue_days`,
`search_regulations`, and a read-only `run_sql`). Every number in an answer
comes from a `DataStore` method; every policy statement comes from a regulation
file.

See [`TECHNICAL_DESIGN.md`](TECHNICAL_DESIGN.md) for architecture, data-flow, and
decisions.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
```

To use the LLM mode, copy `.env.example` to `.env` and set `ANTHROPIC_API_KEY`
(get one at https://console.anthropic.com/). Without a key the agent runs in
deterministic mode and still answers all five example questions.

## Ask a question

```bash
# LLM mode if ANTHROPIC_API_KEY is set, otherwise deterministic
python -m harbourview_agent.cli -q "Are we ever exceeding our fire-code capacity? On which days/hours?"

# force offline mode
python -m harbourview_agent.cli --no-llm -q "Recommend staffing for next weekend."

# interactive loop
python -m harbourview_agent.cli

# show the structured tool output behind the answer
python -m harbourview_agent.cli --no-llm --show-data -q "Which days had high traffic but low ticket revenue?"
```

Flags: `--no-llm`, `--model <id>`, `--root-dir <path>`, `--show-data`.

## Explore the data directly (SQLite)

```bash
# run any read-only query against the cleaned data
python -m harbourview_agent.cli --sql \
  "SELECT date, hour, deck FROM occupancy WHERE deck > 450 ORDER BY deck DESC"

# dump the cleaned data to a .db file for sqlite3 / DB Browser / etc.
python -m harbourview_agent.cli --export-db harbourview.db
```

Tables: `ticket_sales`, `foot_traffic`, `weather` (+ an `adverse` flag),
`events`. Views: `occupancy(date, hour, deck, gardens, site)` (running per-zone
occupancy), `daily_traffic(date, entries, peak_occupancy)`,
`daily_revenue(date, revenue, tickets)`.

## Example questions it handles

| Question | Capability |
|---|---|
| "Best-selling ticket type last month and how did weather affect it?" | sales aggregation × weather correlation |
| "Are we ever exceeding fire-code capacity? Which days/hours?" | running per-zone occupancy vs HVL-REG-001 limits |
| "Refund for a rained-out visit — what does policy allow?" | regulation retrieval (HVL-REG-002 §3) |
| "Recommend staffing for next weekend." | typical traffic × forecast × events × staffing rule |
| "High traffic but low revenue days, and why?" | anomaly detection with weather/event reasons |

## Tests

```bash
pip install pytest
python -m pytest -q
```

20 tests run offline (paths are derived from the repo root, so the suite runs on
any checkout); 2 more (LLM end-to-end + LLM eval) run only when
`ANTHROPIC_API_KEY` is set.

**Answer-quality eval** — a golden-fact check (required phrases, forbidden
phrases, expected intent, and a no-invented-number check for LLM mode):

```bash
python -m harbourview_agent.cli --eval            # mode depends on API key
python -m harbourview_agent.cli --eval --no-llm   # force deterministic
```

For a broader manual pass — the five brief questions plus phrasing variety,
out-of-scope questions, regulation coverage, the SQLite layer, and the
fallback path:

```bash
bash scripts/smoke_test.sh          # deterministic only (free)
bash scripts/smoke_test.sh --llm    # also runs every question through Claude
```

## Known limitations

See [`TECHNICAL_DESIGN.md`](TECHNICAL_DESIGN.md) §5. In short: lexical (not
semantic) regulation retrieval; "expected visitors" for staffing is modelled as
peak concurrent occupancy; no real weather *forecast* source, so the weekend
recommendation uses the weather CSV as a stand-in when the date is in range.

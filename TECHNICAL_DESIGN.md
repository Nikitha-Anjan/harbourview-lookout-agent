# Technical Design Document — Harbourview Lookout AI Ops Assistant

## 1. Overview

A grounded Q&A assistant for non-technical operations staff. It answers
plain-English questions using four evidence sources:

- `data/ticket_sales.csv` — daily sales by ticket type and channel
- `data/foot_traffic.csv` — hourly entries/exits for the deck and gardens
- `data/weather.csv` — daily weather
- `data/events.csv` — scheduled events with an expected uplift
- `regulations/*.md` — fire safety, ticketing/refunds, hours, health & safety

Design priorities: **correct numbers**, **traceable answers**, and **works
without gold-plating**. The agent is deliberately thin; the data layer is where
the real work is.

## 2. Architecture

```text
                 ┌─────────────────────────────┐
   question ───► │      HarbourviewAgent        │  (agent.py — facade + mode switch)
                 └──────────────┬──────────────┘
                                │
             ┌──────────────────┴───────────────────┐
             ▼                                      ▼
   ┌───────────────────┐                  ┌────────────────────────┐
   │  LLM mode         │                  │  Deterministic mode    │
   │  llm_agent.py     │                  │  router.py             │
   │  Pydantic AI      │                  │  keyword intent → tool │
   │  + Claude, tools  │                  │  → templated answer    │
   └─────────┬─────────┘                  └───────────┬────────────┘
             │        both call the same underlying tools
             └──────────────────┬───────────────────┘
                     ┌──────────┴───────────┐
                     ▼                      ▼
             ┌───────────────┐     ┌────────────────────┐
             │  DataStore    │     │  RegulationStore   │
             │ data_store.py │     │ regulation_store.py│
             │ CSV→clean(py)  │    │ keyword retrieval  │
             │ →SQLite→SQL    │    │ over *.md sections │
             └───────────────┘     └────────────────────┘

DataStore pipeline:  data/*.csv ──clean in Python──▶ :memory: SQLite ──SQL──▶ metrics
                     (dedupe, price imputation, casing, missing values)   (views: occupancy,
                                                                           daily_traffic, daily_revenue)
```

### Components

| Component | Responsibility |
|---|---|
| `HarbourviewAgent` (`agent.py`) | Public API `answer_question()`. Picks LLM or deterministic mode. Catches any LLM/runtime failure and falls back to deterministic so the user always gets an answer. Returns a uniform `{answer, evidence, mode, intent}` dict. |
| `llm_agent.py` | Builds a `pydantic_ai.Agent` bound to an Anthropic model. Registers eight tools that wrap `DataStore`/`RegulationStore` (including a read-only `run_sql`). Output is constrained to a `GroundedAnswer` schema (`answer` + `evidence[]`). System prompt forbids un-grounded numbers. `run_agent()` retries a transient malformed-output error before the facade falls back. |
| `router.py` (`DeterministicRouter`) | Offline fallback and the reference the tests pin. Keyword rules → one capability → the matching data method → a fixed answer template. No text generation, so its numbers *are* the data layer's numbers. |
| `DataStore` (`data_store.py`) | Cleans the CSVs in Python (dedupe, price imputation, casing/whitespace, missing-value handling), loads them into an **in-memory SQLite DB**, and computes every metric with SQL. SQL views: `occupancy` (running per-zone occupancy via a window function), `daily_traffic`, `daily_revenue`. `query()` / `export_sqlite()` expose the DB for ad-hoc use. Regulatory constants (zone caps 450/300/900, staffing ratio 1:150, min 2) are named constants citing the source clause. |
| `RegulationStore` (`regulation_store.py`) | Reads `regulations/*.md`, splits into Markdown sections, scores sections by stop-word-filtered term overlap plus topic-hint expansion, returns the top files with their matching paragraphs. |
| `cli.py` | One-shot (`-q`) and interactive modes; `--no-llm`, `--model`, `--root-dir`, `--show-data`, `--sql`, `--export-db`, `--eval`. Loads `.env` if `python-dotenv` is present. |
| `eval.py` | Golden-fact answer-quality check: 7 cases with required/forbidden phrases + expected intent; for LLM mode, every ≥4-digit number in the answer must also appear in the deterministic ground truth (invented-number guard). Run via `--eval` or `tests/test_eval.py`. |

## 3. Logic / data flow — how a question becomes an answer

**LLM mode** (`"Are we exceeding fire-code capacity?"`):

1. `HarbourviewAgent.answer_question()` calls `run_agent()`.
2. Pydantic AI sends the question + tool schemas + system prompt to Claude.
3. Claude calls `capacity_report()`. The tool runs `DataStore.capacity_report()`,
   which reads the `occupancy` SQL view (running per-zone occupancy) and flags
   every hour ≥ 80 / 95 / 100 % of a zone limit.
4. Claude may also call `search_regulations("fire capacity")` for the clause.
5. Claude returns a `GroundedAnswer`: a few sentences + an `evidence` list
   naming `data/foot_traffic.csv` and `regulations/01_fire_safety_and_capacity.md`.
6. The facade maps that to the uniform dict and returns it.

**Deterministic mode**: step 2–5 are replaced by `DeterministicRouter`:
`detect_intent()` → `"capacity"` → `_answer_capacity()` → `DataStore.capacity_report()`
→ templated summary + fixed evidence list.

## 4. Key metrics (what makes the numbers correct)

| Metric | Method | Notes |
|---|---|---|
| Best seller | `best_seller_for_month` | Ranked by **gross tickets**; revenue reported **net of refunds** with blank `unit_price` imputed from the (type, channel) median. |
| Weather effect | `weather_impact_on_ticket_type` | Splits the month's selling days into **adverse** (storm/rain, ≥10 mm, lightning, or wind ≥60 km/h per HVL-REG-001 §3) vs **fair**, compares mean tickets/day. This is the ≥2-source correlation. |
| Fire-code capacity | `occupancy` SQL view + `capacity_report` | **Running** occupancy = cumulative (entries − exits) **per zone, per operating day** via a SQL window function (HVL-REG-001 §1.4). Checked against **all three** zone limits and the 80 / 95 / 100 % actions — not just the site total. Dataset result: deck breaches 450 around 15:00 on 8 days. |
| Staffing | `staffing_recommendation_for_weekend` | Resolves the target Sat+Sun. "Typical traffic" = mean of historical **same-weekday** days. Adjusts for a scheduled **event** uplift and an **adverse forecast** (×0.75). Staff = `max(2, ceil(peak_occupancy / 150))` + 1 capacity monitor on peak-season weekends (HVL-REG-003 §4). |
| High traffic / low revenue | `high_traffic_low_revenue_days` | Days in the **top 40 %** for foot traffic and **bottom 40 %** for net revenue, each annotated with weather/event context so the "why" is grounded. |

### Messy-data handling

- Exact-duplicate trailing row in `ticket_sales.csv` → dropped on read.
- ~40 blank `unit_price` cells → imputed (median by type+channel, then by type).
- Mixed case / trailing spaces in `ticket_type`, `channel`, `condition` → normalised.
- ~5 dates with no weather row + ~3 blank `precip_mm` → surfaced as `None`, callers skip them, never crash.
- Multiple date formats accepted in `_parse_date`.

## 5. Decisions & tradeoffs

**Framework — Pydantic AI.** The brief suggested Pydantic AI or LangGraph. This
task is single-turn tool-calling with strict grounding and typed output — not
stateful multi-step orchestration — so LangGraph's graph/state machinery would
be overhead. Pydantic AI gives exactly what's needed: typed tools from plain
functions, a Pydantic output schema (`GroundedAnswer`), model-agnostic provider
binding, and `run_sync` for a simple CLI. It's also small to install
(`pydantic-ai-slim`).

**Two modes, one interface.** The deterministic router isn't just a fallback —
it's the executable spec for what each answer should contain, and it's what the
tests assert against (fast, free, no flakiness). The LLM mode adds natural
phrasing, multi-tool reasoning, and handling of questions the keyword rules miss.
The facade always degrades to deterministic on any error, so the assistant has
no hard dependency on network or API availability.

**Grounding.** The model has no data access except through tools; the tools
return only computed results; the output schema requires an `evidence` list. A
wrong number would have to originate in `DataStore`, where it's unit-tested.

**Data layer — clean in Python, aggregate in SQLite.** The CSVs are cleaned in
Python (so the messy-data handling is explicit and testable) and loaded into an
**in-memory** SQLite database; each metric is then a short SQL query, and
running per-zone occupancy is a one-line window function instead of a hand-rolled
loop. This is not the "real database" the brief rules out — there's no server,
no persistence, no schema migration; it's an embedded query engine over the same
four files, rebuilt on process start (~1 ms). It also gives the LLM a guarded
read-only `run_sql` tool and the operator a `--sql` / `--export-db` CLI for
questions the fixed tools don't cover, without loosening grounding (still a real
query over real data). Trade-off: a small amount of setup code and an
`sqlite3`-shaped mental model for the reader; the payoff is that the aggregation
logic is declarative and easy to audit against the CSVs.

**Retrieval — lexical, not vector.** Four short, well-structured regulation
files. Keyword scoring over Markdown sections (with stop-word filtering and
topic-hint expansion) is fully explainable — every hit is a real paragraph from
a named file — and needs no embedding model or index. A vector store would be
justified at 10× the corpus size.

**Capacity semantics.** "Occupancy" is explicitly defined in HVL-REG-001 §1.4 as
persons currently inside (entries − exits). The v1 implementation compared a
single hour's *net change* to the site total; this version accumulates within
the day and checks each zone — which is what actually surfaces the deck
overages.

**Staffing — "expected visitors".** HVL-REG-003 §4.1 ("1 attendant per 150
expected visitors") is ambiguous between daily and concurrent. We model it as
**peak concurrent occupancy**, which ties to the capacity model and produces
sane numbers (4–6 staff, not 15). Both the daily total and the peak are returned
so a human can apply their own reading.

## 6. Known limitations & next steps

- **No real forecast source.** "Next weekend" staffing uses the weather CSV as a
  stand-in forecast when the target date is within its range; otherwise it falls
  back to typical conditions. A real deployment would call a weather API.
- **Lexical retrieval** can miss unusual phrasings; topic hints paper over the
  common cases only.
- **Single-turn.** No conversation memory; each question is independent.
- **Deterministic router coverage** is the five brief capabilities plus a
  generic regulation path; genuinely novel questions rely on the LLM mode.
- **Next:** (1) grow the golden-fact eval (`eval.py`) and gate it in CI;
  (2) prompt-caching the tool schemas + system prompt for cost;
  (3) hourly staffing rather than per-day; (4) parse zone limits from the
  regulation text instead of constants, with the constant as a checked fallback;
  (5) if the data grew, persist the SQLite file and load it incrementally
  instead of rebuilding in memory each run.

## 7. Summary

A thin, testable agent over a carefully-built data layer. It meets the core
checklist — plain-language Q&A, real tool-calling, data + regulation grounding,
≥2-source correlation, a correct capacity/safety check, graceful messy-data
handling — and adds recommendations, an evidence trail, env-var config, and an
offline mode, without over-building.

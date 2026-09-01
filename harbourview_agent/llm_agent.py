"""Pydantic AI tool-calling agent.

The LLM never sees the CSVs. It sees a question and a set of tools that wrap the
deterministic :class:`DataStore` / :class:`RegulationStore` methods. It decides
which tools to call, then writes a short answer constrained to a typed schema
whose ``evidence`` list must point back at the tool outputs it used. All numbers
in the answer therefore originate in the data layer, not the model.

This module is imported lazily by :mod:`harbourview_agent.agent`; if
``pydantic-ai`` or an API key is missing, the agent falls back to
:class:`~harbourview_agent.router.DeterministicRouter`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from pydantic import BaseModel, Field
from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.providers.anthropic import AnthropicProvider

from .data_store import DataStore
from .regulation_store import RegulationStore

DEFAULT_MODEL = os.environ.get("HARBOURVIEW_MODEL", "claude-sonnet-5")

SYSTEM_PROMPT = """\
You are the operations assistant for Harbourview Lookout, a tourist attraction.
Your users are non-technical admin staff.

Rules:
- Answer ONLY from tool results. Never invent numbers, dates, or policy text.
- Call whatever tools you need. Prefer several targeted calls over guessing.
- For policy questions, call search_regulations and quote the specific clause.
- For anything numeric, call the matching data tool and use its exact figures.
- Prefer the named data tools; use run_sql only for questions they don't cover.
- If the tools cannot answer, say so plainly and say what is missing.
- Keep the answer to a few sentences in plain English. Give a recommendation
  when the question asks for one.
- Populate `evidence`: one entry per tool result you relied on, naming the
  source file (e.g. "data/ticket_sales.csv" or "regulations/02_ticketing_and_refunds.md")
  and what it told you.

The mock data covers 2025-06-01 to 2025-09-30 and has no concept of "today".
- Default reporting period when the user does not specify one: 2025-08.
- Treat "next weekend" as the weekend of 2025-09-06 unless the user gives a date.
- Call dataset_overview first if you are unsure what is available.
"""


class EvidenceItem(BaseModel):
    source: str = Field(description="Source file the fact came from, e.g. data/weather.csv")
    # optional so a slightly-malformed model response still validates instead of
    # burning all the output retries
    summary: str = Field(default="", description="What this source contributed to the answer")


class GroundedAnswer(BaseModel):
    answer: str
    evidence: list[EvidenceItem] = Field(default_factory=list)


@dataclass
class Deps:
    data: DataStore
    regulations: RegulationStore


def build_agent(model_name: str = DEFAULT_MODEL, api_key: str | None = None) -> Agent:
    from anthropic import AsyncAnthropic

    # Extra retries so short bursts of questions ride out rate-limit 429s
    # instead of falling back to the deterministic router.
    client = AsyncAnthropic(api_key=api_key or os.environ["ANTHROPIC_API_KEY"], max_retries=3)
    model = AnthropicModel(model_name, provider=AnthropicProvider(anthropic_client=client))
    agent = Agent(
        model,
        deps_type=Deps,
        output_type=GroundedAnswer,
        instructions=SYSTEM_PROMPT,
        retries=3,
        model_settings={"max_tokens": 4096},
    )

    @agent.tool
    def dataset_overview(ctx: RunContext[Deps]) -> dict:
        """Date coverage and the ticket types / event dates available."""
        sales = ctx.deps.data.get_ticket_sales()
        dates = sorted({r["date"] for r in sales})
        return {
            "sales_date_range": [dates[0], dates[-1]] if dates else [],
            "ticket_types": sorted({r["ticket_type"] for r in sales if r["ticket_type"]}),
            "events": ctx.deps.data.get_events(),
        }

    @agent.tool
    def best_seller_for_month(ctx: RunContext[Deps], month: str) -> dict:
        """Best-selling ticket type for a month 'YYYY-MM' (gross tickets, net revenue)."""
        return ctx.deps.data.best_seller_for_month(month)

    @agent.tool
    def weather_impact_on_ticket_type(ctx: RunContext[Deps], ticket_type: str, month: str) -> dict:
        """Mean daily sales of a ticket type on adverse-weather vs fair-weather days in a month."""
        return ctx.deps.data.weather_impact_on_ticket_type(ticket_type, month)

    @agent.tool
    def capacity_report(ctx: RunContext[Deps]) -> dict:
        """Hours where any zone's running occupancy reached 80/95/100% of its fire-code limit.

        Returns counts plus the full list of >=100% incidents and a few examples of
        the lower-threshold events (use run_sql on the `occupancy` view for the rest).
        """
        full = ctx.deps.data.capacity_report()
        return {
            "zone_limits": full["site_capacity"],
            "incidents_over_100pct": full["incidents"],
            "incident_days": full["incident_days"],
            "pause_entry_95pct_count": len(full["pause_entry_events"]),
            "pause_entry_examples": full["pause_entry_events"][:5],
            "near_capacity_80pct_count": len(full["near_capacity_events"]),
            "near_capacity_days": sorted({e["date"] for e in full["near_capacity_events"]}),
        }

    @agent.tool
    def staffing_recommendation_for_weekend(ctx: RunContext[Deps], date: str) -> dict:
        """Recommended staffing for the Sat+Sun of the week containing 'date' (YYYY-MM-DD)."""
        return ctx.deps.data.staffing_recommendation_for_weekend(date)

    @agent.tool
    def high_traffic_low_revenue_days(ctx: RunContext[Deps], limit: int = 5) -> list[dict]:
        """Days with high foot traffic but low ticket revenue, with likely reasons."""
        return ctx.deps.data.high_traffic_low_revenue_days(limit=limit)

    @agent.tool
    def search_regulations(ctx: RunContext[Deps], query: str) -> list[dict]:
        """Keyword search over the regulation documents; returns matching sections per file."""
        return ctx.deps.regulations.search(query)

    @agent.tool
    def run_sql(ctx: RunContext[Deps], query: str) -> list[dict]:
        """Read-only SQL over the cleaned data, for questions the other tools don't cover.

        Tables: ticket_sales(date, ticket_type, channel, quantity, refunded_qty,
        net_quantity, unit_price, price_imputed, gross_revenue, net_revenue);
        foot_traffic(date, hour, deck_entries, deck_exits, gardens_entries, gardens_exits);
        weather(date, temp_high_c, temp_low_c, precip_mm, wind_kmh_max, condition, lightning, adverse);
        events(date, event_name, expected_uplift_pct).
        Views: occupancy(date, hour, deck, gardens, site) - running per-zone occupancy;
        daily_traffic(date, entries, peak_occupancy); daily_revenue(date, revenue, tickets).
        Dates are 'YYYY-MM-DD' text. Only a single SELECT is allowed.
        """
        stripped = query.strip().strip(";").lstrip("(").lower()
        if not stripped.startswith(("select", "with")):
            raise ModelRetry("Only read-only SELECT/WITH queries are permitted.")
        return [dict(r) for r in ctx.deps.data.query(query)][:200]

    return agent


def run_agent(agent: Agent, question: str, deps: Deps, attempts: int = 2) -> GroundedAnswer:
    """Run the agent, retrying once on a transient model-behaviour error (a
    malformed structured output that burned the per-run output retries) before
    letting the caller fall back to the deterministic router."""
    from pydantic_ai import UnexpectedModelBehavior

    last_exc: Exception | None = None
    last_output: GroundedAnswer | None = None
    for _ in range(attempts):
        try:
            out = agent.run_sync(question, deps=deps).output
        except UnexpectedModelBehavior as exc:
            last_exc = exc
            continue
        last_output = out
        if out.evidence:  # a grounded answer names its sources; retry if it didn't
            return out
    if last_output is not None:
        return last_output
    raise last_exc  # type: ignore[misc]

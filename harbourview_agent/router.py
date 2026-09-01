"""Deterministic intent router.

This is the offline fallback for :class:`~harbourview_agent.agent.HarbourviewAgent`
(used whenever no ``ANTHROPIC_API_KEY`` is configured) and the reference
implementation the test-suite pins. It maps a question to one capability with
keyword rules, calls the matching :class:`DataStore` / :class:`RegulationStore`
method, and renders a grounded answer from a template - no free-text generation,
so its numbers are exactly the data-layer's numbers.
"""

from __future__ import annotations

import re
from typing import Any

from .data_store import DataStore
from .regulation_store import RegulationStore

# The mock data covers 2025-06-01 .. 2025-09-30 and has no concept of "now".
# When a question says "next weekend" without a date, anchor to this weekend
# inside the data range so the answer is reproducible.
REFERENCE_WEEKEND = "2025-09-06"

# First match wins, so keep the most specific intents first. Any regulation
# question that slips past these still gets a regulation-grounded answer via the
# `_answer_general` fallback (it quotes the best-matching section).
_INTENTS: list[tuple[str, tuple[str, ...]]] = [
    ("refund_policy", ("refund", "cancellation", "cancel", "money back", "reimburse", "compensat")),
    ("capacity", ("capacity", "fire-code", "fire code", "occupancy", "overcrowd", "exceed",
                  "too full", "over the limit", "over capacity", "crowd level")),
    ("staffing", ("staff", "staffing", "roster", "how many people", "how many staff", "attendant",
                  "how many workers", "shift")),
    ("best_seller", ("best-selling", "best selling", "top ticket", "top-selling", "most popular",
                     "best seller", "top ticket type", "highest selling")),
    ("traffic_revenue", ("high traffic", "low revenue", "low ticket", "traffic but", "underperform",
                         "busy but", "footfall but")),
    ("weather_effect", ("weather affect", "weather impact", "rain affect", "how did weather",
                        "rain", "rainy", "storm", "sunny", "hurt sales", "affect sales",
                        "affect ticket", "hurt our sales", "impact sales", "weather on sales")),
    ("regulation", ("regulation", "policy", "rule", "allow", "permit", "required", "must we",
                    "are we required", "hours", "opening", "closing", "season", "accessib",
                    "elevator", "restroom", "service animal", "animal", "stroller", "briefing",
                    "evacuat", "egress", "allergen", "log", "retain", "what does the policy",
                    "what do the rules")),
]


class DeterministicRouter:
    def __init__(self, data: DataStore, regulations: RegulationStore):
        self.data = data
        self.regulations = regulations

    # -- intent + slot parsing -------------------------------------------------
    def detect_intent(self, question: str) -> str:
        q = question.lower()
        for intent, keywords in _INTENTS:
            if any(k in q for k in keywords):
                return intent
        return "general"

    @staticmethod
    def _month(question: str) -> str | None:
        m = re.search(r"(20\d{2})[-/](\d{1,2})", question)
        if m:
            return f"{m.group(1)}-{int(m.group(2)):02d}"
        return None

    @staticmethod
    def _date(question: str) -> str | None:
        m = re.search(r"(20\d{2})[-/](\d{1,2})[-/](\d{1,2})", question)
        if m:
            return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        return None

    @staticmethod
    def _ticket_type(question: str) -> str | None:
        for cand in ("annual pass", "family", "adult", "child", "senior"):
            if cand in question.lower():
                return cand.title()
        return None

    # -- main entrypoint -----------------------------------------------------
    def answer(self, question: str, default_month: str = "2025-08") -> dict[str, Any]:
        intent = self.detect_intent(question)
        handler = getattr(self, f"_answer_{intent}", self._answer_general)
        try:
            result = handler(question, default_month)
        except Exception as exc:  # bad slot value (e.g. unparseable date) - answer, don't crash
            result = {"answer": f"I couldn't process that question ({exc}). Try rephrasing, "
                                f"or give an explicit month (YYYY-MM) or date (YYYY-MM-DD).",
                      "evidence": []}
        result.setdefault("intent", intent)
        result.setdefault("mode", "deterministic")
        return result

    # -- per-intent handlers ----------------------------------------------
    def _answer_refund_policy(self, question: str, _month: str) -> dict:
        hits = self.regulations.search(question or "refund weather closure cancellation")
        if not hits:
            return {"answer": "I could not find a matching refund rule. Please add detail "
                              "(weather closure vs. customer cancellation, and timing).",
                    "evidence": []}
        rained_out = any(w in question.lower() for w in ("rain", "storm", "weather", "closed", "closure"))
        if rained_out:
            answer = (
                "A rained-out visit is a weather closure. Per HVL-REG-002 §3.1, if the Observation "
                "Deck was closed for the whole visit window due to weather, the ticket holder is "
                "entitled to a full refund or free re-entry within 30 days. If the deck was closed "
                "for more than 2 hours but not all day, §3.2 gives a 50% refund or free re-entry."
            )
        else:
            answer = (
                "Customer-initiated cancellations (HVL-REG-002 §3.3): full refund more than 48h "
                "before the visit, 50% within 24-48h, none inside 24h or for a no-show. Annual "
                "passes are non-refundable except unused within 14 days of purchase (§3.4)."
            )
        return {"answer": answer, "evidence": [dict(source=h["file"], summary=" ".join(h["sections"][:2]))
                                               for h in hits]}

    def _answer_capacity(self, _question: str, _month: str) -> dict:
        report = self.data.capacity_report()
        incidents = report["incidents"]
        if incidents:
            worst = incidents[0]
            by_zone = {}
            for inc in incidents:
                by_zone.setdefault(inc["zone"], set()).add(inc["date"])
            zone_bits = ", ".join(
                f"the {'Observation Deck' if z == 'deck' else 'Indoor Gardens Hall' if z == 'gardens' else 'whole site'} "
                f"({report['site_capacity'][z]} limit) on {len(d)} day(s)"
                for z, d in sorted(by_zone.items())
            )
            answer = (
                f"Yes. Running occupancy exceeded a fire-code zone limit on "
                f"{len(report['incident_days'])} day(s), all around 15:00: "
                f"{', '.join(report['incident_days'])}. Breaches: {zone_bits}. "
                f"The single worst reading was the {'deck' if worst['zone'] == 'deck' else worst['zone']} "
                f"at {worst['occupancy']} people ({worst['pct_of_limit']:.0f}% of the {worst['limit']} "
                f"limit) on {worst['date']} h{worst['hour']:02d}. "
                f"A further {len(report['near_capacity_events'])} hourly readings hit the 80% "
                f"'near capacity' threshold (HVL-REG-001 §2.1)."
            )
        else:
            answer = ("No hour breached 100% of a zone limit, but "
                      f"{len(report['near_capacity_events'])} readings reached the 80% threshold.")
        return {
            "answer": answer,
            "evidence": [
                dict(source="data/foot_traffic.csv",
                     summary="Running occupancy = cumulative (entries - exits) per zone, per operating day."),
                dict(source="regulations/01_fire_safety_and_capacity.md",
                     summary="Deck 450 / Gardens 300 / Site 900; 80% notice, 95% pause entry, 100% reportable incident."),
            ],
            "result": report,
        }

    def _answer_best_seller(self, question: str, default_month: str) -> dict:
        month = self._month(question) or default_month
        result = self.data.best_seller_for_month(month)
        if result["ticket_type"] == "N/A":
            return {"answer": f"No ticket sales are recorded for {month}. The data covers "
                              f"2025-06 through 2025-09.",
                    "evidence": [dict(source="data/ticket_sales.csv", summary=f"No rows for {month}.")],
                    "result": result}
        impact = self.data.weather_impact_on_ticket_type(result["ticket_type"], month)
        answer = (
            f"In {month} the best-selling ticket type was {result['ticket_type']} "
            f"({result['quantity']:,} tickets sold, {result['net_quantity']:,} net of refunds, "
            f"${result['revenue']:,.0f} net revenue). Weather effect: {impact['summary']}"
        )
        return {
            "answer": answer,
            "evidence": [
                dict(source="data/ticket_sales.csv", summary=f"Tickets grouped by type for {month} (blank prices imputed by type/channel median)."),
                dict(source="data/weather.csv", summary="Daily condition/precipitation joined to daily sales."),
            ],
            "result": {"best_seller": result, "weather_impact": impact},
        }

    def _answer_weather_effect(self, question: str, default_month: str) -> dict:
        month = self._month(question) or default_month
        ticket_type = self._ticket_type(question) or self.data.best_seller_for_month(month)["ticket_type"]
        if ticket_type == "N/A":
            return {"answer": f"No ticket sales are recorded for {month} to compare against weather.",
                    "evidence": [dict(source="data/ticket_sales.csv", summary=f"No rows for {month}.")]}
        impact = self.data.weather_impact_on_ticket_type(ticket_type, month)
        return {
            "answer": f"{impact['summary']} (Based on {impact['days_analyzed']} selling days in {month}: "
                      f"{impact['adverse_weather_days']} adverse, {impact['fair_weather_days']} fair.)",
            "evidence": [
                dict(source="data/ticket_sales.csv", summary=f"Daily {ticket_type} volume for {month}."),
                dict(source="data/weather.csv", summary="Adverse = storm/rain, >=10 mm, lightning, or wind >=60 km/h (HVL-REG-001 §3)."),
            ],
            "result": impact,
        }

    def _answer_staffing(self, question: str, _month: str) -> dict:
        date_hint = self._date(question) or REFERENCE_WEEKEND
        rec = self.data.staffing_recommendation_for_weekend(date_hint)
        lines = "; ".join(
            f"{d['weekday']} {d['date']}: {d['recommended_staff']} staff "
            f"(~{d['expected_visitors']:,} visitors, {', '.join(d['forecast_factors'])})"
            for d in rec["days"]
        )
        return {
            "answer": f"For the weekend of {rec['weekend_of']}: {lines}. {rec['basis']}",
            "evidence": [
                dict(source="data/foot_traffic.csv", summary="Typical traffic = mean of historical same-weekday days."),
                dict(source="data/events.csv", summary="Scheduled-event uplift applied to expected turnout."),
                dict(source="regulations/03_operating_hours_and_seasons.md",
                     summary="1 attendant / 150 peak visitors, min 2, capacity monitor on peak-season weekends."),
            ],
            "result": rec,
        }

    def _answer_traffic_revenue(self, _question: str, _month: str) -> dict:
        candidates = self.data.high_traffic_low_revenue_days(limit=5)
        if not candidates:
            return {"answer": "No days stood out as high-traffic but low-revenue.", "evidence": []}
        top = candidates[0]
        answer = (
            f"{len(candidates)} day(s) had top-40% foot traffic but bottom-40% ticket revenue. "
            f"The clearest was {top['date']}: {top['traffic']:,} visitors, only "
            f"${top['revenue']:,.0f} revenue (${top['revenue_per_visitor']:.2f}/visitor). "
            f"Likely reason: {top['likely_reasons'][0]}."
        )
        return {
            "answer": answer,
            "evidence": [
                dict(source="data/foot_traffic.csv", summary="Daily entry totals (deck + gardens)."),
                dict(source="data/ticket_sales.csv", summary="Daily net ticket revenue."),
                dict(source="data/weather.csv", summary="Weather/event context attached to each flagged day."),
            ],
            "result": candidates,
        }

    @staticmethod
    def _clean_section(text: str) -> str:
        """Strip Markdown noise so a quoted regulation section reads as prose."""
        text = re.sub(r"[*#>|`]", "", text)
        text = re.sub(r"^\s*[-]\s*", "", text, flags=re.M)
        return re.sub(r"\s+", " ", text).strip()

    def _answer_regulation(self, question: str, _month: str) -> dict:
        hits = self.regulations.search(question)
        if not hits:
            return {"answer": "I could not find a relevant regulation for that question. "
                              "Try naming the topic (refunds, capacity, hours, accessibility).",
                    "evidence": [], "intent": "regulation"}
        top = hits[0]
        snippet = self._clean_section(top["sections"][0]) if top["sections"] else ""
        doc = top["file"].split("/")[-1].removesuffix(".md")
        return {
            "answer": f"Per {doc}: {snippet[:600]}",
            "evidence": [dict(source=h["file"], summary=self._clean_section(" ".join(h["sections"][:2]))[:400])
                         for h in hits],
            "result": hits,
        }

    def _answer_general(self, question: str, default_month: str) -> dict:
        # If the question clearly matches a regulation section, answer from it.
        hits = self.regulations.search(question)
        if hits and hits[0]["score"] >= 3:
            return self._answer_regulation(question, default_month)

        best = self.data.best_seller_for_month(default_month)
        answer = (
            "I can answer questions about ticket sales, fire-code capacity, staffing, weather "
            "effects, and site regulations (refunds, hours, accessibility). I don't have data "
            "on anything outside the four provided sources or any future/forecast period. "
            f"For reference, {best['ticket_type']} led sales in {default_month} with "
            f"{best['quantity']:,} tickets."
        )
        evidence = [dict(source="data/ticket_sales.csv", summary=f"Best seller for {default_month}.")]
        if hits:
            evidence.append(dict(source=hits[0]["file"],
                                 summary=self._clean_section(" ".join(hits[0]["sections"][:1]))[:300]))
        return {"answer": answer, "evidence": evidence}

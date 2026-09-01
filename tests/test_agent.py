"""End-to-end checks for the data layer and the deterministic router.

Paths come from the ``root_dir`` fixture in conftest.py (repo root, derived from
__file__) so the suite runs on any checkout. The LLM path is exercised only when
ANTHROPIC_API_KEY is set; everything else is offline and deterministic.
"""

import os

import pytest

from harbourview_agent.data_store import ZONE_CAPACITY


# --------------------------------------------------------------------------
# Capability 1 - best-selling ticket type + weather effect
# --------------------------------------------------------------------------
def test_best_seller_last_month(store):
    result = store.best_seller_for_month("2025-08")
    assert result["ticket_type"] == "Family"
    assert result["quantity"] > 3000
    # revenue is net of refunds and uses imputed prices, so it must be positive
    assert result["revenue"] > 0
    assert result["net_quantity"] <= result["quantity"]


def test_weather_suppresses_family_sales(store):
    impact = store.weather_impact_on_ticket_type("Family", "2025-08")
    assert impact["adverse_weather_days"] > 0 and impact["fair_weather_days"] > 0
    # rained-out days sell fewer Family tickets than fair days
    assert impact["avg_tickets_adverse_days"] < impact["avg_tickets_fair_days"]
    assert impact["delta_pct"] < 0


# --------------------------------------------------------------------------
# Capability 2 - fire-code capacity (running per-zone occupancy)
# --------------------------------------------------------------------------
def test_capacity_monitoring_detects_zone_overages(store):
    report = store.capacity_report()
    assert report["any_incident"] is True
    incidents = report["incidents"]
    assert incidents, "dataset is seeded with Observation Deck overages ~15:00"
    for inc in incidents:
        assert inc["zone"] in ZONE_CAPACITY
        assert inc["occupancy"] > inc["limit"]
        assert inc["pct_of_limit"] > 100
    # the deck (450 cap) is the binding constraint here
    assert any(inc["zone"] == "deck" for inc in incidents)
    # near-capacity (80-95%) readings are also surfaced
    assert report["near_capacity_events"]


def test_capacity_incidents_helper_matches_report(store):
    assert store.capacity_incidents() == store.capacity_report()["incidents"]


# --------------------------------------------------------------------------
# Capability 3 - refund policy retrieval (grounded in regulations/)
# --------------------------------------------------------------------------
def test_regulation_search_for_refunds(router):
    answer = router.answer_question(
        "A customer wants a refund for a rained-out visit - what does policy allow?"
    )
    assert "full refund" in answer["answer"].lower()
    assert answer["evidence"]
    assert any("02_ticketing_and_refunds.md" in e["source"] for e in answer["evidence"])


# --------------------------------------------------------------------------
# Capability 4 - staffing recommendation
# --------------------------------------------------------------------------
def test_staffing_recommendation_for_next_weekend(store):
    rec = store.staffing_recommendation_for_weekend("2025-09-06")
    assert rec["recommended_staff"] >= 2
    assert rec["expected_visitors"] > 0
    assert len(rec["days"]) == 2
    for day in rec["days"]:
        assert day["recommended_staff"] >= 2
        assert day["expected_peak_occupancy"] > 0
        # September is peak season -> dedicated capacity monitor
        assert day["dedicated_capacity_monitor"] is True


def test_staffing_applies_event_uplift(store):
    # 2025-06-14 (a Saturday) is "Summer Kickoff Festival" (+40%), fair weather
    rec = store.staffing_recommendation_for_weekend("2025-06-14")
    saturday = next(d for d in rec["days"] if d["date"] == "2025-06-14")
    assert any("Summer Kickoff Festival" in f for f in saturday["forecast_factors"])
    assert saturday["expected_visitors"] > saturday["typical_visitors"]


# --------------------------------------------------------------------------
# Capability 5 - high traffic, low revenue
# --------------------------------------------------------------------------
def test_high_traffic_low_revenue_detection(store):
    candidates = store.high_traffic_low_revenue_days()
    assert candidates
    for c in candidates:
        assert c["date"]
        assert c["likely_reasons"]  # every flagged day carries a grounded "why"
        assert c["revenue_per_visitor"] >= 0


# --------------------------------------------------------------------------
# Messy-data handling
# --------------------------------------------------------------------------
def test_messy_data_is_cleaned(store):
    sales = store.get_ticket_sales()
    # the exact-duplicate trailing row is dropped
    assert len(sales) == 1220
    # every row has a usable price, even the ~40 with a blank unit_price column
    assert all(isinstance(r["unit_price"], float) and r["unit_price"] > 0 for r in sales)
    # mixed casing / trailing spaces collapse to canonical labels
    assert {r["ticket_type"] for r in sales} == {"Adult", "Child", "Senior", "Family", "Annual Pass"}


def test_missing_weather_rows_do_not_crash(store):
    weather = store.get_weather()
    # 2025-07-16 has a blank precip_mm; some dates have no row at all
    assert weather["2025-07-16"]["precip_mm"] is None
    assert store.weather_impact_on_ticket_type("Adult", "2025-07")["days_analyzed"] > 0


def test_sqlite_layer_is_queryable(store, tmp_path):
    # cleaned rows land in the in-memory DB
    (count,) = store.query("SELECT COUNT(*) FROM ticket_sales")[0]
    assert count == 1220
    # ad-hoc SQL agrees with the metric method
    row = store.query(
        "SELECT ticket_type, SUM(quantity) g FROM ticket_sales "
        "WHERE date LIKE '2025-08%' GROUP BY ticket_type ORDER BY g DESC LIMIT 1"
    )[0]
    assert row["ticket_type"] == store.best_seller_for_month("2025-08")["ticket_type"]
    # the occupancy view exposes running per-zone occupancy
    assert store.query("SELECT MAX(deck) FROM occupancy")[0][0] > ZONE_CAPACITY["deck"]
    # export to a real file works
    db_file = store.export_sqlite(tmp_path / "harbourview.db")
    assert db_file.exists() and db_file.stat().st_size > 0


def test_bad_question_does_not_crash(router):
    result = router.answer_question("asdfjkl???")
    assert result["answer"]
    assert result["mode"] == "deterministic"


# --------------------------------------------------------------------------
# Agent wiring
# --------------------------------------------------------------------------
def test_agent_defaults_to_deterministic_without_key(root_dir, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from harbourview_agent.agent import HarbourviewAgent

    assert HarbourviewAgent(root_dir).mode == "deterministic"


@pytest.mark.llm
@pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="needs ANTHROPIC_API_KEY")
def test_llm_agent_end_to_end(root_dir):
    """Wiring smoke test: LLM mode initialises, calls the API, returns a
    structured answer with evidence. Fact-correctness is checked separately."""
    from harbourview_agent.agent import HarbourviewAgent

    agent = HarbourviewAgent(root_dir, use_llm=True)
    assert agent.mode == "llm"
    # a transient model hiccup falls back to deterministic (by design); retry a
    # couple of times so the test reflects "the LLM path works", not a bad roll
    for _ in range(3):
        result = agent.answer_question("Are we ever exceeding our fire-code capacity?")
        if result["mode"] == "llm":
            break
    assert result["mode"] == "llm", result.get("llm_error")
    assert len(result["answer"]) > 40
    assert result["evidence"]
    # the model was told to name source files; at least one should look like one
    assert any(
        (".csv" in e["source"]) or (".md" in e["source"]) or ("regulation" in e["source"].lower())
        for e in result["evidence"]
    )

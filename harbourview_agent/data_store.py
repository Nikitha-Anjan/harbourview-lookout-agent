"""Data layer: clean the four operational CSVs, load them into an in-memory
SQLite database, and answer every metric with SQL.

Flow::

    data/*.csv --(clean in Python)--> :memory: SQLite --(SQL)--> metrics

Cleaning (dedupe, price imputation, casing, missing values) stays in Python so
it is explicit and unit-tested; aggregation is SQL so it is short and readable.
Nothing is written to disk unless you call :meth:`DataStore.export_sqlite`.

Regulatory constants (zone capacities, staffing ratio) come from
``regulations/01_fire_safety_and_capacity.md`` (HVL-REG-001) and
``regulations/03_operating_hours_and_seasons.md`` (HVL-REG-003).
"""

from __future__ import annotations

import csv
import math
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean, median

# HVL-REG-001 §1 - maximum instantaneous occupancy per zone (persons inside).
ZONE_CAPACITY = {"deck": 450, "gardens": 300, "site": 900}
# HVL-REG-001 §2 - action thresholds as a fraction of a zone limit.
NEAR_CAPACITY_FRACTION = 0.80   # §2.1 post "Near Capacity" notice + attendant
PAUSE_ENTRY_FRACTION = 0.95     # §2.2 pause entry to the zone
INCIDENT_FRACTION = 1.00        # §2.3 reportable safety incident
# HVL-REG-003 §4.1 - one attendant per this many people; §4.2 - hard floor.
VISITORS_PER_ATTENDANT = 150
MIN_STAFF_ON_SITE = 2
# HVL-REG-003 §1 - peak season runs June 1 - September 30 (capacity monitor rule).
PEAK_MONTHS = {6, 7, 8, 9}


_SCHEMA = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")


class DataStore:
    def __init__(self, root_dir: str | Path):
        self.root_dir = Path(root_dir)
        self.data_dir = self.root_dir / "data"
        self.ticket_sales_path = self.data_dir / "ticket_sales.csv"
        self.foot_traffic_path = self.data_dir / "foot_traffic.csv"
        self.weather_path = self.data_dir / "weather.csv"
        self.events_path = self.data_dir / "events.csv"
        self._db: sqlite3.Connection | None = None
        self._row_cache: dict[str, object] = {}

    # ==================================================================
    # CSV reading + cleaning (Python - explicit and testable)
    # ==================================================================
    def _read_csv_rows(self, path: Path) -> list[dict[str, str]]:
        if not path.exists():
            return []
        with path.open(newline="", encoding="utf-8") as handle:
            return [
                {k: (v.strip() if isinstance(v, str) else "") for k, v in row.items() if k is not None}
                for row in csv.DictReader(handle)
            ]

    @staticmethod
    def _to_int(value: str, default: int = 0) -> int:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _to_float(value: str, default: float | None = 0.0) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _normalize_ticket_type(value: str) -> str:
        """``annual_pass`` / ``ANNUAL PASS`` / ``annual pass `` -> ``Annual Pass``."""
        normalized = (value or "").strip().replace("_", " ")
        return " ".join(part.capitalize() for part in normalized.split())

    @staticmethod
    def _parse_date(date_str: str) -> datetime | None:
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"):
            try:
                return datetime.strptime((date_str or "").strip(), fmt)
            except ValueError:
                continue
        return None

    def get_ticket_sales(self) -> list[dict]:
        if "ticket_sales" in self._row_cache:
            return self._row_cache["ticket_sales"]  # type: ignore[return-value]
        rows: list[dict] = []
        seen: set[tuple] = set()
        for raw in self._read_csv_rows(self.ticket_sales_path):
            key = tuple(sorted(raw.items()))
            if key in seen:  # quirk: an exact-duplicate row is appended to the file
                continue
            seen.add(key)
            rows.append(
                {
                    "date": raw.get("date", ""),
                    "ticket_type": self._normalize_ticket_type(raw.get("ticket_type", "")),
                    "channel": raw.get("channel", "").lower(),
                    "quantity": self._to_int(raw.get("quantity")),
                    "unit_price_raw": self._to_float(raw.get("unit_price"), default=None),
                    "refunded_qty": self._to_int(raw.get("refunded_qty")),
                }
            )
        self._impute_prices(rows)
        self._row_cache["ticket_sales"] = rows
        return rows

    def _impute_prices(self, rows: list[dict]) -> None:
        """Fill blank ``unit_price`` from the median of the same
        (ticket_type, channel), then the same ticket_type. ~40 rows are blank."""
        by_type_channel: defaultdict[tuple, list[float]] = defaultdict(list)
        by_type: defaultdict[str, list[float]] = defaultdict(list)
        for row in rows:
            if row["unit_price_raw"] is not None:
                by_type_channel[(row["ticket_type"], row["channel"])].append(row["unit_price_raw"])
                by_type[row["ticket_type"]].append(row["unit_price_raw"])
        tc_median = {k: median(v) for k, v in by_type_channel.items()}
        t_median = {k: median(v) for k, v in by_type.items()}
        for row in rows:
            if row["unit_price_raw"] is not None:
                row["unit_price"], row["price_imputed"] = row["unit_price_raw"], False
            else:
                row["unit_price"] = tc_median.get((row["ticket_type"], row["channel"])) or t_median.get(
                    row["ticket_type"], 0.0
                )
                row["price_imputed"] = True
            row["net_quantity"] = max(row["quantity"] - row["refunded_qty"], 0)
            row["gross_revenue"] = row["unit_price"] * row["quantity"]
            row["net_revenue"] = row["unit_price"] * row["net_quantity"]

    def get_foot_traffic(self) -> list[dict]:
        if "foot_traffic" in self._row_cache:
            return self._row_cache["foot_traffic"]  # type: ignore[return-value]
        rows = [
            {
                "date": raw.get("date", ""),
                "hour": self._to_int(raw.get("hour")),
                "deck_entries": self._to_int(raw.get("deck_entries")),
                "deck_exits": self._to_int(raw.get("deck_exits")),
                "gardens_entries": self._to_int(raw.get("gardens_entries")),
                "gardens_exits": self._to_int(raw.get("gardens_exits")),
            }
            for raw in self._read_csv_rows(self.foot_traffic_path)
        ]
        self._row_cache["foot_traffic"] = rows
        return rows

    def get_weather(self) -> dict[str, dict]:
        """Keyed by date. ~5 dates have no row and ~3 rows have a blank
        ``precip_mm`` - both surface as ``None``, never a crash."""
        out: dict[str, dict] = {}
        for raw in self._read_csv_rows(self.weather_path):
            row = {
                "date": raw.get("date", ""),
                "temp_high_c": self._to_float(raw.get("temp_high_c"), default=None),
                "temp_low_c": self._to_float(raw.get("temp_low_c"), default=None),
                "precip_mm": self._to_float(raw.get("precip_mm"), default=None),
                "wind_kmh_max": self._to_float(raw.get("wind_kmh_max"), default=None),
                "condition": (raw.get("condition") or "").strip().lower(),
                "lightning": self._to_int(raw.get("lightning")),
            }
            out[row["date"]] = row
        return out

    def get_events(self) -> dict[str, dict]:
        return {
            raw.get("date", ""): {
                "date": raw.get("date", ""),
                "event_name": raw.get("event_name", ""),
                "expected_uplift_pct": self._to_int(raw.get("expected_uplift_pct")),
            }
            for raw in self._read_csv_rows(self.events_path)
        }

    @staticmethod
    def _is_adverse(weather_row: dict | None) -> bool:
        """A 'bad weather' day: storm/rain condition, >=10 mm rain, lightning,
        or the deck-closing wind threshold from HVL-REG-001 §3.1 (60 km/h)."""
        if not weather_row:
            return False
        if weather_row.get("condition") in {"rain", "storm"}:
            return True
        precip = weather_row.get("precip_mm")
        if precip is not None and precip >= 10:
            return True
        if weather_row.get("lightning"):
            return True
        wind = weather_row.get("wind_kmh_max")
        return wind is not None and wind >= 60

    # ==================================================================
    # SQLite layer
    # ==================================================================
    @property
    def db(self) -> sqlite3.Connection:
        if self._db is None:
            self._db = self._build_db()
        return self._db

    def _build_db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA)

        conn.executemany(
            "INSERT INTO ticket_sales VALUES (:date,:ticket_type,:channel,:quantity,"
            ":refunded_qty,:net_quantity,:unit_price,:price_imputed,:gross_revenue,:net_revenue)",
            [{**r, "price_imputed": int(r["price_imputed"])} for r in self.get_ticket_sales()],
        )
        conn.executemany(
            "INSERT INTO foot_traffic VALUES (:date,:hour,:deck_entries,:deck_exits,"
            ":gardens_entries,:gardens_exits)",
            self.get_foot_traffic(),
        )
        conn.executemany(
            "INSERT INTO weather VALUES (:date,:temp_high_c,:temp_low_c,:precip_mm,"
            ":wind_kmh_max,:condition,:lightning,:adverse)",
            [{**r, "adverse": int(self._is_adverse(r))} for r in self.get_weather().values()],
        )
        conn.executemany(
            "INSERT INTO events VALUES (:date,:event_name,:expected_uplift_pct)",
            self.get_events().values(),
        )
        conn.commit()
        return conn

    def query(self, sql: str, params: dict | tuple = ()) -> list[sqlite3.Row]:
        """Run an arbitrary read query against the in-memory DB (used by the
        CLI ``--sql`` flag and handy for exploration)."""
        return self.db.execute(sql, params).fetchall()

    def export_sqlite(self, path: str | Path) -> Path:
        """Write the in-memory DB to a file for external tools (sqlite3, DB
        Browser, ...). Not used by the agent itself."""
        path = Path(path)
        dest = sqlite3.connect(path)
        with dest:
            self.db.backup(dest)
        dest.close()
        return path

    # ==================================================================
    # Capability 1 - best seller + weather effect
    # ==================================================================
    def best_seller_for_month(self, month: str) -> dict:
        """Best-selling ticket type for ``YYYY-MM`` by gross tickets sold.
        Revenue is net of refunds and uses imputed prices for blank rows."""
        rows = self.query(
            """
            SELECT ticket_type,
                   SUM(quantity)     AS gross,
                   SUM(net_quantity) AS net,
                   SUM(net_revenue)  AS revenue
            FROM ticket_sales
            WHERE date LIKE :m
            GROUP BY ticket_type
            ORDER BY gross DESC, revenue DESC
            """,
            {"m": f"{month}%"},
        )
        if not rows:
            return {"month": month, "ticket_type": "N/A", "quantity": 0, "net_quantity": 0,
                    "revenue": 0.0, "by_type": {}}
        top = rows[0]
        return {
            "month": month,
            "ticket_type": top["ticket_type"],
            "quantity": top["gross"],
            "net_quantity": top["net"],
            "revenue": round(top["revenue"], 2),
            "by_type": {r["ticket_type"]: {"quantity": r["gross"], "revenue": round(r["revenue"], 2)}
                        for r in rows},
        }

    def weather_impact_on_ticket_type(self, ticket_type: str, month: str) -> dict:
        """Mean tickets/day for one type on adverse-weather vs fair-weather days."""
        ticket_type = self._normalize_ticket_type(ticket_type)
        rows = self.query(
            """
            SELECT s.day_qty, w.adverse
            FROM (SELECT date, SUM(quantity) AS day_qty
                  FROM ticket_sales
                  WHERE date LIKE :m AND ticket_type = :tt
                  GROUP BY date) s
            LEFT JOIN weather w ON w.date = s.date
            """,
            {"m": f"{month}%", "tt": ticket_type},
        )
        adverse = [r["day_qty"] for r in rows if r["adverse"] == 1]
        fair = [r["day_qty"] for r in rows if r["adverse"] == 0]
        missing = sum(1 for r in rows if r["adverse"] is None)

        adverse_avg = round(mean(adverse), 1) if adverse else 0.0
        fair_avg = round(mean(fair), 1) if fair else 0.0
        delta_pct = round((adverse_avg - fair_avg) / fair_avg * 100, 1) if fair_avg else 0.0
        if not rows:
            summary = f"No {ticket_type} sales recorded in {month}."
        elif adverse and fair and abs(delta_pct) < 3:
            summary = (
                f"{ticket_type} sales were about the same in {month} regardless of weather "
                f"(~{fair_avg:.0f}/day on both adverse and fair days)."
            )
        elif adverse and fair:
            direction = "lower" if delta_pct < 0 else "higher"
            summary = (
                f"{ticket_type} sales averaged {adverse_avg:.0f}/day on adverse-weather days vs "
                f"{fair_avg:.0f}/day on fair days ({abs(delta_pct):.0f}% {direction})."
            )
        else:
            summary = "Not enough weather variation in the month to compare."
        return {
            "ticket_type": ticket_type,
            "month": month,
            "days_analyzed": len(rows),
            "adverse_weather_days": len(adverse),
            "fair_weather_days": len(fair),
            "days_missing_weather": missing,
            "avg_tickets_adverse_days": adverse_avg,
            "avg_tickets_fair_days": fair_avg,
            "delta_pct": delta_pct,
            "summary": summary,
        }

    # ==================================================================
    # Capability 2 - fire-code capacity
    # ==================================================================
    def occupancy_timeline(self) -> list[dict]:
        """Running occupancy per zone per hour (see the ``occupancy`` SQL view)."""
        return [dict(r) for r in self.query(
            "SELECT date, hour, deck, gardens, site FROM occupancy ORDER BY date, hour"
        )]

    def capacity_report(self, min_fraction: float = NEAR_CAPACITY_FRACTION) -> dict:
        """Every hour where a zone reached ``min_fraction`` of its limit, tagged
        with the HVL-REG-001 §2 action that applies."""
        rows: list[dict] = []
        for point in self.occupancy_timeline():
            for zone in ("deck", "gardens", "site"):
                limit = ZONE_CAPACITY[zone]
                frac = point[zone] / limit
                if frac < min_fraction:
                    continue
                if frac >= INCIDENT_FRACTION:
                    action, level = "reportable safety incident (HVL-REG-001 §2.3)", "incident"
                elif frac >= PAUSE_ENTRY_FRACTION:
                    action, level = "pause entry to zone (HVL-REG-001 §2.2)", "pause_entry"
                else:
                    action, level = "post 'Near Capacity' notice + attendant (HVL-REG-001 §2.1)", "near_capacity"
                rows.append(
                    {"date": point["date"], "hour": point["hour"], "zone": zone,
                     "occupancy": point[zone], "limit": limit,
                     "pct_of_limit": round(frac * 100, 1), "level": level, "action": action}
                )
        rows.sort(key=lambda r: (r["pct_of_limit"], r["date"]), reverse=True)
        incidents = [r for r in rows if r["level"] == "incident"]
        return {
            "site_capacity": ZONE_CAPACITY,
            "incidents": incidents,
            "pause_entry_events": [r for r in rows if r["level"] == "pause_entry"],
            "near_capacity_events": [r for r in rows if r["level"] == "near_capacity"],
            "incident_days": sorted({r["date"] for r in incidents}),
            "any_incident": bool(incidents),
        }

    def capacity_incidents(self) -> list[dict]:
        """Hours that exceeded 100% of a zone limit - reportable per
        HVL-REG-001 §2.3. Non-empty for this dataset (deck peaks ~15:00)."""
        return self.capacity_report(min_fraction=INCIDENT_FRACTION)["incidents"]

    # ==================================================================
    # Capability 4 - staffing recommendation
    # ==================================================================
    def staffing_recommendation_for_weekend(self, date_str: str) -> dict:
        """Staffing for the Sat+Sun of the target week.

        'Typical traffic' = mean of historical same-weekday days (SQL). Staffing
        uses peak concurrent occupancy / 150 (HVL-REG-003 §4.1), floor 2 (§4.2);
        peak-season weekends add a capacity monitor (§4.3). A scheduled event or
        an adverse forecast for the target date adjusts the expected load.
        """
        target = self._parse_date(date_str)
        if target is None:
            raise ValueError(f"Could not parse date: {date_str!r}")
        saturday = target + timedelta(days=(5 - target.weekday()) % 7)
        weekend = [saturday, saturday + timedelta(days=1)]

        events = self.get_events()
        weather = self.get_weather()
        days_out = []
        for day in weekend:
            key = day.strftime("%Y-%m-%d")
            dow = str(int(day.strftime("%w")))  # sqlite: 0=Sun .. 6=Sat
            row = self.query(
                """
                SELECT AVG(entries) AS typ_entries, AVG(peak_occupancy) AS typ_peak
                FROM daily_traffic
                WHERE strftime('%w', date) = :dow AND date < :key
                """,
                {"dow": dow, "key": key},
            )[0]
            fallback = self.query("SELECT AVG(entries) e, AVG(peak_occupancy) p FROM daily_traffic")[0]
            typical_entries = row["typ_entries"] or fallback["e"] or 0
            typical_peak = row["typ_peak"] or fallback["p"] or 0

            factors: list[str] = []
            multiplier = 1.0
            if key in events:
                multiplier *= 1 + events[key]["expected_uplift_pct"] / 100
                factors.append(f"event '{events[key]['event_name']}' "
                               f"(+{events[key]['expected_uplift_pct']}% expected)")
            wrow = weather.get(key)
            if self._is_adverse(wrow):
                multiplier *= 0.75
                factors.append(f"adverse forecast ({wrow['condition'] or 'rain'})")
            if wrow and wrow.get("temp_high_c") is not None and wrow["temp_high_c"] >= 32:
                factors.append("heat >=32C: deploy water stations (HVL-REG-004 §2.1)")

            expected_visitors = round(typical_entries * multiplier)
            expected_peak = round(typical_peak * multiplier)
            attendants = max(MIN_STAFF_ON_SITE, math.ceil(expected_peak / VISITORS_PER_ATTENDANT))
            capacity_monitor = day.month in PEAK_MONTHS
            days_out.append(
                {
                    "date": key,
                    "weekday": day.strftime("%A"),
                    "typical_visitors": round(typical_entries),
                    "expected_visitors": expected_visitors,
                    "expected_peak_occupancy": expected_peak,
                    "forecast_factors": factors or ["typical conditions"],
                    "attendants": attendants,
                    "dedicated_capacity_monitor": capacity_monitor,
                    "recommended_staff": attendants + (1 if capacity_monitor else 0),
                    "crowd_briefing_required": expected_peak >= NEAR_CAPACITY_FRACTION * ZONE_CAPACITY["site"],
                }
            )

        peak_day = max(days_out, key=lambda d: d["recommended_staff"])
        return {
            "weekend_of": weekend[0].strftime("%Y-%m-%d"),
            "days": days_out,
            "recommended_staff": peak_day["recommended_staff"],
            "expected_visitors": max(d["expected_visitors"] for d in days_out),
            "basis": (
                "Mean of historical same-weekday traffic, adjusted for scheduled events and the "
                "weather forecast; 1 attendant per 150 peak concurrent visitors, min 2, "
                "plus a capacity monitor on peak-season weekends (HVL-REG-003 §4)."
            ),
        }

    # ==================================================================
    # Capability 5 - high traffic, low revenue
    # ==================================================================
    def high_traffic_low_revenue_days(self, limit: int = 5) -> list[dict]:
        """Days in the top 40% for foot traffic but the bottom 40% for ticket
        revenue, annotated with weather + events so the 'why' is grounded."""
        rows = self.query(
            """
            SELECT t.date,
                   t.entries                       AS traffic,
                   COALESCE(r.revenue, 0.0)        AS revenue,
                   CUME_DIST() OVER (ORDER BY t.entries)              AS traffic_rank,
                   CUME_DIST() OVER (ORDER BY COALESCE(r.revenue,0))  AS revenue_rank
            FROM daily_traffic t
            LEFT JOIN daily_revenue r ON r.date = t.date
            """
        )
        if len(rows) < 5:
            return []
        weather = self.get_weather()
        events = self.get_events()
        out: list[dict] = []
        for row in rows:
            if row["traffic_rank"] < 0.60 or row["revenue_rank"] > 0.40:
                continue
            reasons: list[str] = []
            wrow = weather.get(row["date"])
            if self._is_adverse(wrow):
                reasons.append(
                    f"{wrow['condition'] or 'wet weather'}"
                    + (f", {wrow['precip_mm']:.0f} mm rain" if wrow.get("precip_mm") else "")
                    + " - visitors browse the gardens but buy fewer deck tickets"
                )
            if row["date"] in events:
                reasons.append(
                    f"event '{events[row['date']]['event_name']}' may drive non-ticketed or discounted attendance"
                )
            if not reasons:
                reasons.append("no weather/event signal - possible group comps or pass holders")
            traffic = row["traffic"]
            revenue = row["revenue"]
            out.append(
                {
                    "date": row["date"],
                    "traffic": traffic,
                    "revenue": round(revenue, 2),
                    "revenue_per_visitor": round(revenue / traffic, 2) if traffic else 0.0,
                    "likely_reasons": reasons,
                }
            )
        out.sort(key=lambda d: (d["traffic"], -d["revenue"]), reverse=True)
        return out[:limit]

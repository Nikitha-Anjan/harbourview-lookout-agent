-- In-memory SQLite schema for the Harbourview data layer.
-- Built once per process by DataStore._build_db() from the cleaned CSV rows.

CREATE TABLE ticket_sales (
    date TEXT, ticket_type TEXT, channel TEXT,
    quantity INTEGER, refunded_qty INTEGER, net_quantity INTEGER,
    unit_price REAL, price_imputed INTEGER,
    gross_revenue REAL, net_revenue REAL
);

CREATE TABLE foot_traffic (
    date TEXT, hour INTEGER,
    deck_entries INTEGER, deck_exits INTEGER,
    gardens_entries INTEGER, gardens_exits INTEGER
);

CREATE TABLE weather (
    date TEXT PRIMARY KEY, temp_high_c REAL, temp_low_c REAL, precip_mm REAL,
    wind_kmh_max REAL, condition TEXT, lightning INTEGER, adverse INTEGER
);

CREATE TABLE events (date TEXT PRIMARY KEY, event_name TEXT, expected_uplift_pct INTEGER);

CREATE INDEX ix_sales_date ON ticket_sales(date);
CREATE INDEX ix_traffic_date ON foot_traffic(date);

-- Running occupancy per zone = cumulative (entries - exits) within each
-- operating day, clamped at zero (HVL-REG-001 §1.4: "persons currently inside").
CREATE VIEW occupancy AS
SELECT date, hour,
       MAX(deck_run, 0) AS deck,
       MAX(gardens_run, 0) AS gardens,
       MAX(deck_run, 0) + MAX(gardens_run, 0) AS site
FROM (
    SELECT date, hour,
           SUM(deck_entries - deck_exits)
               OVER (PARTITION BY date ORDER BY hour ROWS UNBOUNDED PRECEDING) AS deck_run,
           SUM(gardens_entries - gardens_exits)
               OVER (PARTITION BY date ORDER BY hour ROWS UNBOUNDED PRECEDING) AS gardens_run
    FROM foot_traffic
);

-- One row per operating day: total entries + peak concurrent site occupancy.
CREATE VIEW daily_traffic AS
SELECT f.date,
       SUM(f.deck_entries + f.gardens_entries) AS entries,
       (SELECT MAX(o.site) FROM occupancy o WHERE o.date = f.date) AS peak_occupancy
FROM foot_traffic f
GROUP BY f.date;

CREATE VIEW daily_revenue AS
SELECT date, SUM(net_revenue) AS revenue, SUM(net_quantity) AS tickets
FROM ticket_sales GROUP BY date;

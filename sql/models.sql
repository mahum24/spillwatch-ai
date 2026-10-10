-- Modelled layer, rebuilt from raw on every `spillwatch transform`.

-- One row per overflow with its most recent attributes and monitor reliability.
CREATE OR REPLACE TABLE dim_overflows AS
WITH ranked AS (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY overflow_id ORDER BY snapshot_at DESC) AS rn
    FROM raw_overflow_snapshots
),
uptime AS (
    SELECT overflow_id,
           COUNT(*)                                    AS snapshots_seen,
           AVG(CASE WHEN status = -1 THEN 1 ELSE 0 END) AS offline_ratio
    FROM raw_overflow_snapshots
    GROUP BY overflow_id
)
SELECT r.overflow_id, r.company, r.site_id, r.company_name, r.receiving_water,
       r.latitude, r.longitude,
       r.status AS current_status, r.status_start AS current_status_since,
       r.snapshot_at AS last_seen_at,
       u.snapshots_seen, u.offline_ratio
FROM ranked r JOIN uptime u USING (overflow_id)
WHERE r.rn = 1;

-- Spill events reconstructed from snapshots. Each distinct latest_event_start is one
-- event; its end is the latest end time ever reported for it (NULL = still spilling).
-- Local snapshots and cloud keyframes/changes are combined, so an event seen by
-- either collector is captured (the cloud keeps running while the laptop sleeps).
CREATE OR REPLACE TABLE fct_spill_events AS
WITH observations AS (
    SELECT overflow_id, company, latest_event_start, latest_event_end, snapshot_at FROM raw_overflow_snapshots
    UNION ALL
    SELECT overflow_id, company, latest_event_start, latest_event_end, snapshot_at FROM raw_overflow_cloud
)
SELECT overflow_id,
       ANY_VALUE(company)                          AS company,
       latest_event_start                          AS event_start,
       MAX(latest_event_end) FILTER (WHERE latest_event_end >= latest_event_start) AS event_end,
       MIN(snapshot_at)                            AS first_seen_at,
       MAX(snapshot_at)                            AS last_seen_at
FROM observations
WHERE latest_event_start IS NOT NULL
GROUP BY overflow_id, latest_event_start;

ALTER TABLE fct_spill_events ADD COLUMN is_ongoing BOOLEAN;
ALTER TABLE fct_spill_events ADD COLUMN duration_hours DOUBLE;
UPDATE fct_spill_events SET
    is_ongoing = event_end IS NULL,
    duration_hours = DATE_DIFF('second', event_start, COALESCE(event_end, last_seen_at)) / 3600.0;

-- Up to 3 nearest rain gauges within 10 km of each overflow (haversine distance).
CREATE OR REPLACE TABLE bridge_overflow_gauge AS
WITH pairs AS (
    SELECT o.overflow_id, s.station_id,
           2 * 6371 * ASIN(SQRT(
               POWER(SIN(RADIANS(s.latitude - o.latitude) / 2), 2) +
               COS(RADIANS(o.latitude)) * COS(RADIANS(s.latitude)) *
               POWER(SIN(RADIANS(s.longitude - o.longitude) / 2), 2)
           )) AS distance_km
    FROM dim_overflows o
    JOIN raw_rain_stations s
      ON ABS(s.latitude - o.latitude) < 0.1 AND ABS(s.longitude - o.longitude) < 0.16
    WHERE o.latitude IS NOT NULL
)
SELECT overflow_id, station_id, distance_km,
       ROW_NUMBER() OVER (PARTITION BY overflow_id ORDER BY distance_km) AS gauge_rank
FROM pairs
WHERE distance_km <= 10
QUALIFY gauge_rank <= 3;

-- Rainfall context for every spill event: average across the nearby gauges.
-- `has_rain_data` is false when no gauge or no readings cover the window,
-- so missing data is never mistaken for "dry".
CREATE OR REPLACE TABLE fct_event_rainfall AS
WITH per_gauge AS (
    SELECT e.overflow_id, e.event_start, b.station_id,
           SUM(r.rain_mm) FILTER (WHERE r.reading_at >= e.event_start - INTERVAL 24 HOUR AND r.reading_at < e.event_start) AS rain_24h_before,
           SUM(r.rain_mm) FILTER (WHERE r.reading_at >= e.event_start - INTERVAL 48 HOUR AND r.reading_at < e.event_start) AS rain_48h_before,
           SUM(r.rain_mm) FILTER (WHERE r.reading_at >= e.event_start AND r.reading_at < COALESCE(e.event_end, e.last_seen_at)) AS rain_during,
           COUNT(r.rain_mm) FILTER (WHERE r.reading_at >= e.event_start - INTERVAL 48 HOUR AND r.reading_at < e.event_start) AS readings_48h
    FROM fct_spill_events e
    JOIN bridge_overflow_gauge b USING (overflow_id)
    LEFT JOIN raw_rainfall_readings r
      ON r.station_id = b.station_id
     AND r.reading_at >= e.event_start - INTERVAL 48 HOUR
     AND r.reading_at <  COALESCE(e.event_end, e.last_seen_at)
    GROUP BY ALL
)
SELECT e.*,
       COUNT(g.station_id) FILTER (WHERE g.readings_48h > 0)          AS gauges_with_data,
       AVG(g.rain_24h_before) FILTER (WHERE g.readings_48h > 0)       AS rain_24h_before_mm,
       AVG(g.rain_48h_before) FILTER (WHERE g.readings_48h > 0)       AS rain_48h_before_mm,
       AVG(g.rain_during)     FILTER (WHERE g.readings_48h > 0)       AS rain_during_mm,
       COUNT(g.station_id) FILTER (WHERE g.readings_48h > 0) > 0      AS has_rain_data
FROM fct_spill_events e
LEFT JOIN per_gauge g USING (overflow_id, event_start)
GROUP BY ALL;

-- Baseline "dry spill" rule (the simple heuristic used in public analyses):
-- a spill with < 0.25 mm of rain in the 48 h before it started and during it.
-- This is the benchmark the ML model must beat (thesis RQ1).
ALTER TABLE fct_event_rainfall ADD COLUMN is_dry_spill_rule BOOLEAN;
UPDATE fct_event_rainfall SET is_dry_spill_rule = CASE
    WHEN NOT has_rain_data THEN NULL
    ELSE rain_48h_before_mm + COALESCE(rain_during_mm, 0) < 0.25
END;

-- Collector health: gap since the previous snapshot. Gaps > 20 min mean events
-- may have been missed, so they bound the completeness of fct_spill_events.
CREATE OR REPLACE TABLE fct_collection_gaps AS
WITH snaps AS (
    SELECT snapshot_at FROM raw_overflow_snapshots
    UNION
    SELECT snapshot_at FROM raw_cloud_runs
)
SELECT snapshot_at,
       LAG(snapshot_at) OVER (ORDER BY snapshot_at) AS previous_snapshot_at,
       DATE_DIFF('minute', LAG(snapshot_at) OVER (ORDER BY snapshot_at), snapshot_at) AS gap_minutes
FROM snaps;

-- Monitor status changes per overflow (online <-> offline <-> spilling), used to
-- measure monitor reliability and to exclude unreliable sites from training data.
CREATE OR REPLACE TABLE fct_status_changes AS
WITH s AS (
    SELECT overflow_id, company, snapshot_at, status,
           LAG(status) OVER (PARTITION BY overflow_id ORDER BY snapshot_at) AS previous_status
    FROM raw_overflow_snapshots
)
SELECT * FROM s WHERE previous_status IS DISTINCT FROM status AND previous_status IS NOT NULL;

-- Hourly rainfall per measure from every source; for each hour keep the most
-- complete version. A 15-min reading timestamped T covers the period ending at T.
CREATE OR REPLACE TABLE rain_hourly AS
WITH from_readings AS (
    SELECT measure_id, ANY_VALUE(station_id) AS station_id,
           DATE_TRUNC('hour', reading_at - INTERVAL 1 SECOND) AS hour_start,
           SUM(rain_mm) AS mm, COUNT(*) AS readings
    FROM raw_rainfall_readings GROUP BY measure_id, hour_start
),
combined AS (
    SELECT * FROM from_readings
    UNION ALL
    SELECT measure_id, station_id, hour_start, mm, readings FROM raw_rain_hourly
)
SELECT * FROM combined
QUALIFY ROW_NUMBER() OVER (PARTITION BY measure_id, hour_start ORDER BY readings DESC) = 1;

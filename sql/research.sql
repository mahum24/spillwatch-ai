-- Research database: historical spills + daily rainfall -> site-day modelling table.
-- Rebuilt from the raw parquet landing zone by `spillwatch build-research`.
-- {raw} is replaced with the raw data directory.
--
-- Time convention: everything is expressed in EA "rain days" (09:00 to 09:00 GMT),
-- so a spill at 08:00 on D+1 belongs to rain day D, matching the rainfall totals.

SET TimeZone = 'UTC';

-- United Utilities changed site-id scheme between 2023 (UUG...) and 2024 (UUP...).
-- Map old ids to new ones by location: 97% sit within 1 m of a new site. Only
-- one-to-one matches are used, so overflows sharing an outfall are never merged.
CREATE OR REPLACE TABLE site_id_aliases AS
WITH s AS (
    SELECT source, company, site_id, MEDIAN(latitude) AS lat, MEDIAN(longitude) AS lon
    FROM read_parquet('{raw}/history/*.parquet') GROUP BY ALL
),
old AS (SELECT * FROM s WHERE source = 'united_utilities_2023'),
new AS (SELECT DISTINCT company, site_id, lat, lon FROM s
        WHERE company = 'united_utilities' AND source <> 'united_utilities_2023'),
pairs AS (
    SELECT o.company, o.site_id AS old_id, n.site_id AS new_id,
           2 * 6371000 * ASIN(SQRT(POWER(SIN(RADIANS(n.lat - o.lat) / 2), 2) +
               COS(RADIANS(o.lat)) * COS(RADIANS(n.lat)) *
               POWER(SIN(RADIANS(n.lon - o.lon) / 2), 2))) AS metres
    FROM old o JOIN new n
      ON n.company = o.company AND ABS(n.lat - o.lat) < 0.001 AND ABS(n.lon - o.lon) < 0.0015
)
SELECT company, old_id, new_id, metres
FROM pairs
WHERE metres <= 1
QUALIFY COUNT(*) OVER (PARTITION BY old_id) = 1      -- old id matches exactly one new id
    AND COUNT(*) OVER (PARTITION BY new_id) = 1;     -- and nothing else claims that new id

CREATE OR REPLACE TABLE hist_events_raw AS
SELECT h.* REPLACE (COALESCE(a.new_id, h.site_id) AS site_id)
FROM read_parquet('{raw}/history/*.parquet') h
LEFT JOIN site_id_aliases a ON a.company = h.company AND a.old_id = h.site_id;

-- Companies report differently: Northumbrian logs 15-minute blocks (median 0.25 h),
-- others log whole spills (Yorkshire median ~3 h). Merge events separated by
-- <= 30 minutes into continuous spells so every company is counted the same way
-- (gaps-and-islands). Overlapping sources for the same site are merged too.
CREATE OR REPLACE TABLE hist_events AS
WITH ordered AS (
    SELECT *,
           MAX(event_end) OVER (PARTITION BY company, site_id ORDER BY event_start, event_end
                                ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prev_max_end
    FROM hist_events_raw
),
islands AS (
    SELECT *,
           SUM(CASE WHEN prev_max_end IS NULL OR event_start > prev_max_end + INTERVAL 30 MINUTE
                    THEN 1 ELSE 0 END)
               OVER (PARTITION BY company, site_id ORDER BY event_start, event_end) AS spell_no
    FROM ordered
)
SELECT MIN(source) AS source, company, site_id,
       MIN(event_start) AS event_start, MAX(event_end) AS event_end,
       MEDIAN(latitude) AS latitude, MEDIAN(longitude) AS longitude,
       COUNT(*) AS raw_events,
       CAST(MIN(event_start) - INTERVAL 9 HOUR AS DATE) AS start_rain_day,
       CAST(MAX(event_end)   - INTERVAL 9 HOUR AS DATE) AS end_rain_day,
       DATE_DIFF('second', MIN(event_start), MAX(event_end)) / 3600.0 AS duration_hours
FROM islands
GROUP BY company, site_id, spell_no;

-- Period each source covers (sites are observed for every day in this window)
CREATE OR REPLACE TABLE hist_source_windows AS
SELECT source, company,
       MIN(CAST(event_start - INTERVAL 9 HOUR AS DATE)) AS first_day,
       MAX(CAST(event_start - INTERVAL 9 HOUR AS DATE)) AS last_day
FROM hist_events_raw GROUP BY ALL;

-- One row per site with a representative location
CREATE OR REPLACE TABLE hist_sites AS
SELECT company, site_id,
       MEDIAN(latitude) AS latitude, MEDIAN(longitude) AS longitude,
       COUNT(*) AS events
FROM hist_events
WHERE latitude BETWEEN 49.8 AND 55.9 AND longitude BETWEEN -6.5 AND 2.0
GROUP BY ALL;

-- Daily rainfall per gauge with rolling antecedent totals
CREATE OR REPLACE TABLE rain_gauges AS
SELECT * FROM read_parquet('{raw}/rain_daily_gauges.parquet');

CREATE OR REPLACE TABLE rain_daily AS
WITH r AS (
    SELECT measure_id, CAST(rain_day AS DATE) AS rain_day, AVG(rain_mm) AS rain_mm
    FROM read_parquet('{raw}/rain_daily/*.parquet')
    GROUP BY ALL
)
SELECT measure_id, rain_day, rain_mm,
       SUM(rain_mm) OVER w1  AS rain_prev_day,
       SUM(rain_mm) OVER w3  AS rain_3d,
       SUM(rain_mm) OVER w7  AS rain_7d,
       SUM(rain_mm) OVER w14 AS rain_14d,
       SUM(rain_mm) OVER w30 AS rain_30d,
       MAX(rain_mm) OVER w7  AS rain_max_day_7d,
       COUNT(rain_mm) OVER w30 AS days_reported_30d
FROM r
WINDOW
    w1  AS (PARTITION BY measure_id ORDER BY rain_day RANGE BETWEEN INTERVAL 1 DAY PRECEDING AND INTERVAL 1 DAY PRECEDING),
    w3  AS (PARTITION BY measure_id ORDER BY rain_day RANGE BETWEEN INTERVAL 2 DAY PRECEDING AND CURRENT ROW),
    w7  AS (PARTITION BY measure_id ORDER BY rain_day RANGE BETWEEN INTERVAL 6 DAY PRECEDING AND CURRENT ROW),
    w14 AS (PARTITION BY measure_id ORDER BY rain_day RANGE BETWEEN INTERVAL 13 DAY PRECEDING AND CURRENT ROW),
    w30 AS (PARTITION BY measure_id ORDER BY rain_day RANGE BETWEEN INTERVAL 29 DAY PRECEDING AND CURRENT ROW);

-- Up to 3 nearest gauges within 10 km of each site
CREATE OR REPLACE TABLE bridge_site_gauge AS
WITH pairs AS (
    SELECT s.company, s.site_id, g.measure_id,
           2 * 6371 * ASIN(SQRT(
               POWER(SIN(RADIANS(g.latitude - s.latitude) / 2), 2) +
               COS(RADIANS(s.latitude)) * COS(RADIANS(g.latitude)) *
               POWER(SIN(RADIANS(g.longitude - s.longitude) / 2), 2))) AS distance_km
    FROM hist_sites s
    JOIN rain_gauges g
      ON ABS(g.latitude - s.latitude) < 0.1 AND ABS(g.longitude - s.longitude) < 0.16
)
SELECT *, ROW_NUMBER() OVER (PARTITION BY company, site_id ORDER BY distance_km) AS gauge_rank
FROM pairs WHERE distance_km <= 10
QUALIFY gauge_rank <= 3;

-- Days on which a site was already spilling at 09:00 (a spill that began on an
-- earlier day). A new spill cannot "start" then, so these days are excluded.
CREATE OR REPLACE TABLE hist_ongoing_days AS
SELECT DISTINCT company, site_id, CAST(d AS DATE) AS rain_day
FROM hist_events,
     generate_series(start_rain_day + 1, end_rain_day, INTERVAL 1 DAY) AS t(d)
WHERE end_rain_day > start_rain_day;

-- The modelling table: one row per site per day in its source's window.
CREATE OR REPLACE TABLE site_day AS
WITH site_windows AS (
    SELECT DISTINCT e.company, e.site_id, w.first_day, w.last_day
    FROM (SELECT DISTINCT source, company, site_id FROM hist_events_raw) e
    JOIN hist_source_windows w USING (source, company)
),
days AS (
    SELECT company, site_id, CAST(d AS DATE) AS rain_day
    FROM site_windows, generate_series(first_day, last_day, INTERVAL 1 DAY) AS t(d)
),
spills AS (
    SELECT company, site_id, start_rain_day AS rain_day,
           COUNT(*) AS spills_started, SUM(duration_hours) AS spill_hours
    FROM hist_events GROUP BY ALL
),
rain AS (
    SELECT b.company, b.site_id, r.rain_day,
           COUNT(*)              AS gauges,
           MIN(b.distance_km)    AS nearest_gauge_km,
           AVG(r.rain_mm)        AS rain_day_mm,
           AVG(r.rain_prev_day)  AS rain_prev_day_mm,
           AVG(r.rain_3d)        AS rain_3d_mm,
           AVG(r.rain_7d)        AS rain_7d_mm,
           AVG(r.rain_14d)       AS rain_14d_mm,
           AVG(r.rain_30d)       AS rain_30d_mm,
           AVG(r.rain_max_day_7d) AS rain_max_day_7d_mm,
           MIN(r.days_reported_30d) AS min_days_reported_30d
    FROM bridge_site_gauge b
    JOIN rain_daily r USING (measure_id)
    GROUP BY ALL
)
SELECT d.company, d.site_id, d.rain_day,
       COALESCE(s.spills_started, 0) > 0 AS spill_started,
       COALESCE(s.spill_hours, 0)        AS spill_hours,
       r.* EXCLUDE (company, site_id, rain_day),
       o.rain_day IS NOT NULL            AS already_spilling
FROM days d
JOIN hist_sites hs USING (company, site_id)
LEFT JOIN spills s USING (company, site_id, rain_day)
LEFT JOIN rain r USING (company, site_id, rain_day)
LEFT JOIN hist_ongoing_days o USING (company, site_id, rain_day);

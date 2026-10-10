-- Raw layer: data exactly as collected (after field-name normalisation).

CREATE TABLE IF NOT EXISTS raw_overflow_snapshots (
    overflow_id        VARCHAR NOT NULL,
    company            VARCHAR NOT NULL,
    site_id            VARCHAR,
    company_name       VARCHAR,
    status             INTEGER,      -- -1 offline, 0 stopped, 1 spilling
    status_start       TIMESTAMPTZ,
    latest_event_start TIMESTAMPTZ,
    latest_event_end   TIMESTAMPTZ,
    latitude           DOUBLE,
    longitude          DOUBLE,
    receiving_water    VARCHAR,
    last_updated       TIMESTAMPTZ,
    snapshot_at        TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (overflow_id, snapshot_at)
);

CREATE TABLE IF NOT EXISTS raw_rain_stations (
    station_id VARCHAR PRIMARY KEY,
    label      VARCHAR,
    latitude   DOUBLE,
    longitude  DOUBLE
);

CREATE TABLE IF NOT EXISTS raw_rainfall_readings (
    station_id VARCHAR NOT NULL,
    measure_id VARCHAR NOT NULL,
    reading_at TIMESTAMPTZ NOT NULL,
    rain_mm    DOUBLE,
    PRIMARY KEY (measure_id, reading_at)
);

-- Audit trail of every pipeline run (useful for monitoring and for the report)
CREATE TABLE IF NOT EXISTS pipeline_runs (
    job         VARCHAR,
    started_at  TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    rows_loaded BIGINT,
    status      VARCHAR,
    message     VARCHAR
);

-- One row per data-quality check per run (see src/spillwatch/quality.py)
CREATE TABLE IF NOT EXISTS dq_results (
    checked_at TIMESTAMPTZ,
    check_name VARCHAR,
    value      DOUBLE,
    status     VARCHAR,      -- pass / warn / fail
    message    VARCHAR
);

-- Cloud collection (GitHub Actions): weekly keyframes + changed overflows only
CREATE TABLE IF NOT EXISTS raw_overflow_cloud (
    overflow_id        VARCHAR NOT NULL,
    company            VARCHAR,
    status             INTEGER,
    status_start       TIMESTAMPTZ,
    latest_event_start TIMESTAMPTZ,
    latest_event_end   TIMESTAMPTZ,
    last_updated       TIMESTAMPTZ,
    snapshot_at        TIMESTAMPTZ NOT NULL,
    kind               VARCHAR,       -- keyframe / change
    PRIMARY KEY (overflow_id, snapshot_at)
);

CREATE TABLE IF NOT EXISTS raw_cloud_runs (
    snapshot_at  TIMESTAMPTZ,
    overflows    INTEGER,
    rows_written INTEGER,
    keyframe     BOOLEAN,
    companies    VARCHAR
);

-- Hourly rainfall per gauge measure. Filled from the cloud data branch; locally it
-- is merged with hourly sums of raw_rainfall_readings (see models.sql: rain_hourly).
CREATE TABLE IF NOT EXISTS raw_rain_hourly (
    measure_id VARCHAR NOT NULL,
    station_id VARCHAR,
    hour_start TIMESTAMPTZ NOT NULL,   -- readings in [hour_start, hour_start + 1 h)
    mm         DOUBLE,
    readings   INTEGER,                -- 15-min readings present (4 = complete hour)
    PRIMARY KEY (measure_id, hour_start)
);

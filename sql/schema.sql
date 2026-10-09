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

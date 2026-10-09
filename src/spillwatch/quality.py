"""Data-quality checks run after every collection.

Each check returns a measured value and a status (pass / warn / fail). Results are
stored in `dq_results`, giving a history of pipeline health for the dashboard and
for evaluating how trustworthy the data behind each AI prediction is.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import duckdb

from spillwatch.config import OVERFLOW_FEEDS

log = logging.getLogger(__name__)

# Rough bounding box for England and Wales: anything outside is a bad coordinate
LAT_RANGE = (49.8, 55.9)
LON_RANGE = (-6.5, 2.0)


@dataclass
class Check:
    name: str
    sql: str               # must return a single numeric value
    warn_above: float | None = None
    fail_above: float | None = None
    fail_below: float | None = None
    description: str = ""

    def evaluate(self, value: float | None) -> str:
        if value is None:
            return "fail"
        if self.fail_below is not None and value < self.fail_below:
            return "fail"
        if self.fail_above is not None and value > self.fail_above:
            return "fail"
        if self.warn_above is not None and value > self.warn_above:
            return "warn"
        return "pass"


LATEST = "(SELECT MAX(snapshot_at) FROM raw_overflow_snapshots)"
PREVIOUS = f"(SELECT MAX(snapshot_at) FROM raw_overflow_snapshots WHERE snapshot_at < {LATEST})"

CHECKS = [
    Check(
        "companies_in_latest_snapshot",
        f"SELECT COUNT(DISTINCT company) FROM raw_overflow_snapshots WHERE snapshot_at = {LATEST}",
        fail_below=len(OVERFLOW_FEEDS),
        description="Every company feed responded in the latest run",
    ),
    Check(
        "minutes_since_last_snapshot",
        f"SELECT DATE_DIFF('minute', {LATEST}, now())",
        warn_above=20, fail_above=60,
        description="Collector is running on schedule",
    ),
    Check(
        "overflow_count_change_pct",
        f"""SELECT 100.0 * ABS(cur.n - prev.n) / NULLIF(prev.n, 0) FROM
              (SELECT COUNT(*) n FROM raw_overflow_snapshots WHERE snapshot_at = {LATEST}) cur,
              (SELECT COUNT(*) n FROM raw_overflow_snapshots WHERE snapshot_at = {PREVIOUS}) prev""",
        warn_above=2, fail_above=10,
        description="Number of overflows is stable between runs (sudden drops = partial feed)",
    ),
    Check(
        "duplicate_overflow_ids",
        f"""SELECT COUNT(*) - COUNT(DISTINCT overflow_id)
            FROM raw_overflow_snapshots WHERE snapshot_at = {LATEST}""",
        fail_above=0,
        description="Overflow ids are unique within a snapshot",
    ),
    Check(
        "bad_coordinates_pct",
        f"""SELECT 100.0 * AVG(CASE WHEN latitude NOT BETWEEN {LAT_RANGE[0]} AND {LAT_RANGE[1]}
                                    OR longitude NOT BETWEEN {LON_RANGE[0]} AND {LON_RANGE[1]}
                                    OR latitude IS NULL THEN 1 ELSE 0 END)
            FROM raw_overflow_snapshots WHERE snapshot_at = {LATEST}""",
        warn_above=0.5, fail_above=5,
        description="Overflow locations fall inside England/Wales",
    ),
    Check(
        "offline_monitors_pct",
        f"""SELECT 100.0 * AVG(CASE WHEN status = -1 THEN 1 ELSE 0 END)
            FROM raw_overflow_snapshots WHERE snapshot_at = {LATEST}""",
        warn_above=5, fail_above=20,
        description="Share of monitors reporting offline",
    ),
    Check(
        "events_ending_before_start",
        f"""SELECT COUNT(*) FROM raw_overflow_snapshots
            WHERE snapshot_at = {LATEST} AND latest_event_end < latest_event_start
              AND status <> 1""",
        warn_above=0,
        description="Stopped overflows whose latest end precedes latest start (stale end time)",
    ),
    Check(
        "future_timestamps",
        f"""SELECT COUNT(*) FROM raw_overflow_snapshots
            WHERE snapshot_at = {LATEST}
              AND GREATEST(latest_event_start, latest_event_end) > snapshot_at + INTERVAL 1 HOUR""",
        fail_above=0,
        description="No event timestamps in the future",
    ),
    Check(
        "minutes_since_latest_rainfall",
        "SELECT DATE_DIFF('minute', MAX(reading_at), now()) FROM raw_rainfall_readings",
        warn_above=60, fail_above=180,
        description="Rainfall data is fresh",
    ),
    Check(
        "rain_gauges_reporting_last_hour",
        "SELECT COUNT(DISTINCT station_id) FROM raw_rainfall_readings WHERE reading_at > now() - INTERVAL 1 HOUR",
        fail_below=500,
        description="Enough rain gauges are reporting",
    ),
    Check(
        "rainfall_coverage_last_7d_pct",
        """SELECT 100.0 * COUNT(*) / NULLIF(
                 (SELECT COUNT(*) FROM raw_rain_stations) * 7 * 96, 0)
           FROM raw_rainfall_readings WHERE reading_at > now() - INTERVAL 7 DAY""",
        fail_below=70,
        description="Share of expected 15-min rainfall readings present in the last 7 days",
    ),
]


def run_checks(con: duckdb.DuckDBPyConnection, checks: list[Check] = CHECKS) -> list[dict]:
    checked_at = datetime.now(timezone.utc).replace(microsecond=0)
    results = []
    for check in checks:
        try:
            value = con.execute(check.sql).fetchone()[0]
            value = None if value is None else float(value)
            status = check.evaluate(value)
            message = check.description
        except Exception as exc:  # a broken check is itself a failure
            value, status, message = None, "fail", f"check errored: {exc}"[:500]
        results.append({"checked_at": checked_at, "check_name": check.name,
                        "value": value, "status": status, "message": message})
        log_fn = log.info if status == "pass" else log.warning
        log_fn("DQ %-32s %-5s %s", check.name, status.upper(), "n/a" if value is None else f"{value:,.2f}")

    con.executemany(
        "INSERT INTO dq_results VALUES (?, ?, ?, ?, ?)",
        [[r["checked_at"], r["check_name"], r["value"], r["status"], r["message"]] for r in results],
    )
    return results

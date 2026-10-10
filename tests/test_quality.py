from datetime import datetime, timedelta, timezone

import pandas as pd

from spillwatch import quality, warehouse
from spillwatch.ingest.overflows import normalise

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def _row(site, status=0, lat=54.57, lon=-1.23):
    return {"Id": site, "Company": "Test Water", "Status": status,
            "LatestEventStart": int((NOW - timedelta(hours=3)).timestamp() * 1000),
            "LatestEventEnd": int((NOW - timedelta(hours=2)).timestamp() * 1000),
            "Latitude": lat, "Longitude": lon}


def _results(con):
    return {r["check_name"]: r for r in quality.run_checks(con)}


def test_evaluate_thresholds():
    c = quality.Check("x", "SELECT 1", warn_above=5, fail_above=10, fail_below=1)
    assert c.evaluate(3) == "pass"
    assert c.evaluate(7) == "warn"
    assert c.evaluate(11) == "fail"
    assert c.evaluate(0) == "fail"
    assert c.evaluate(None) == "fail"


def test_checks_detect_partial_feed_and_bad_coordinates(tmp_path):
    con = warehouse.connect(tmp_path / "test.duckdb")
    prev = normalise([_row(f"S{i}") for i in range(100)], "test", NOW - timedelta(minutes=15))
    # Latest run lost half the sites, and one site has a coordinate in the Atlantic
    latest = normalise([_row(f"S{i}") for i in range(49)] + [_row("S99", lat=40.0, lon=-30.0)],
                       "test", NOW)
    warehouse.insert_ignore(con, "raw_overflow_snapshots", pd.concat([prev, latest]))

    results = _results(con)

    assert results["overflow_count_change_pct"]["status"] == "fail"
    assert results["overflow_count_change_pct"]["value"] == 50.0
    assert results["bad_coordinates_pct"]["value"] == 2.0      # 1 of 50 sites
    assert results["bad_coordinates_pct"]["status"] == "warn"
    assert results["duplicate_overflow_ids"]["status"] == "pass"
    assert results["minutes_since_last_snapshot"]["status"] == "pass"
    # No rainfall loaded: freshness check must fail, not silently pass
    assert results["minutes_since_latest_rainfall"]["status"] == "fail"
    # Results are persisted for the dashboard
    assert con.execute("SELECT COUNT(*) FROM dq_results").fetchone()[0] == len(quality.CHECKS)

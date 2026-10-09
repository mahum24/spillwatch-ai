"""SpillWatch command-line entry point.

    python -m spillwatch.cli collect              # one overflow snapshot + latest rainfall
    python -m spillwatch.cli stations             # refresh rain gauge locations
    python -m spillwatch.cli backfill-rain --days 14
    python -m spillwatch.cli transform            # rebuild modelled tables
    python -m spillwatch.cli check [--strict]     # run data-quality checks
    python -m spillwatch.cli health               # pipeline + data-quality status
    python -m spillwatch.cli summary              # quick look at what's in the warehouse
"""

import argparse
import logging
from datetime import date, datetime, timedelta, timezone

from spillwatch import quality, warehouse
from spillwatch.http import make_session
from spillwatch.ingest import overflows, rainfall

log = logging.getLogger("spillwatch")


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def _run_job(con, job, fn):
    started = _now()
    try:
        rows = fn()
    except Exception as exc:
        warehouse.log_run(con, job, started, 0, "failed", str(exc)[:500])
        raise
    warehouse.log_run(con, job, started, rows)
    log.info("%s: %d rows loaded", job, rows)
    return rows


def cmd_stations(con, session, _args):
    def job():
        df = rainfall.fetch_stations(session)
        warehouse.land_raw(df, "rain_stations", _now())
        return warehouse.replace_table(con, "raw_rain_stations", df)

    _run_job(con, "rain_stations", job)


def cmd_collect(con, session, _args):
    def overflow_job():
        df = overflows.collect_snapshot(session)
        warehouse.land_raw(df, "overflow_snapshots", df["snapshot_at"].iloc[0].to_pydatetime())
        return warehouse.insert_ignore(con, "raw_overflow_snapshots", df)

    def rain_job():
        df = rainfall.fetch_latest(session)
        warehouse.land_raw(df, "rainfall_latest", _now())
        return warehouse.insert_ignore(con, "raw_rainfall_readings", df)

    _run_job(con, "overflow_snapshot", overflow_job)
    _run_job(con, "rainfall_latest", rain_job)


def cmd_backfill_rain(con, session, args):
    end = date.today() - timedelta(days=1)  # today's archive isn't published yet
    for i in range(args.days):
        day = end - timedelta(days=i)
        already = con.execute(
            "SELECT COUNT(*) FROM raw_rainfall_readings WHERE CAST(reading_at AS DATE) = ?", [day]
        ).fetchone()[0]
        if already > 50_000 and not args.force:
            log.info("Skipping %s (already loaded)", day)
            continue

        def job(day=day):
            df = rainfall.fetch_archive_day(session, day)
            warehouse.land_raw(df, "rainfall_archive", datetime.combine(day, datetime.min.time()))
            return warehouse.insert_ignore(con, "raw_rainfall_readings", df)

        _run_job(con, f"rainfall_archive:{day}", job)


def cmd_transform(con, _session, _args):
    _run_job(con, "transform", lambda: (warehouse.run_models(con), 0)[1])


def cmd_check(con, _session, args):
    results = quality.run_checks(con)
    failed = [r["check_name"] for r in results if r["status"] == "fail"]
    if failed and args.strict:
        raise SystemExit(f"Data-quality checks failed: {', '.join(failed)}")


def cmd_health(con, _session, _args):
    print("Latest data-quality results:")
    print(con.sql("""
        SELECT check_name, ROUND(value, 2) AS value, status
        FROM dq_results WHERE checked_at = (SELECT MAX(checked_at) FROM dq_results)
        ORDER BY CASE status WHEN 'fail' THEN 0 WHEN 'warn' THEN 1 ELSE 2 END, check_name
    """))
    print("Pipeline runs (last 24h):")
    print(con.sql("""
        SELECT split_part(job, ':', 1) AS job, COUNT(*) AS runs,
               COUNT(*) FILTER (WHERE status = 'failed') AS failed,
               SUM(rows_loaded) AS rows_loaded, MAX(finished_at) AS last_run
        FROM pipeline_runs WHERE started_at > now() - INTERVAL 24 HOUR
        GROUP BY 1 ORDER BY 1
    """))
    tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
    if "fct_collection_gaps" in tables:
        print("Collector gaps over 20 minutes:")
        print(con.sql("""
            SELECT previous_snapshot_at, snapshot_at, gap_minutes
            FROM fct_collection_gaps WHERE gap_minutes > 20 ORDER BY snapshot_at DESC LIMIT 10
        """))


def cmd_summary(con, _session, _args):
    queries = {
        "Snapshots collected": "SELECT COUNT(DISTINCT snapshot_at) FROM raw_overflow_snapshots",
        "Overflows tracked": "SELECT COUNT(DISTINCT overflow_id) FROM raw_overflow_snapshots",
        "Rain gauges": "SELECT COUNT(*) FROM raw_rain_stations",
        "Rainfall readings": "SELECT COUNT(*) FROM raw_rainfall_readings",
    }
    for label, sql in queries.items():
        print(f"{label:<22} {con.execute(sql).fetchone()[0]:>12,}")

    tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
    if "fct_event_rainfall" not in tables:
        print("\nRun `transform` to build the modelled tables.")
        return
    print("\nSpilling right now, by company:")
    print(con.sql("""
        SELECT company,
               COUNT(*) FILTER (WHERE current_status = 1)  AS spilling,
               COUNT(*) FILTER (WHERE current_status = -1) AS offline,
               COUNT(*)                                    AS total
        FROM dim_overflows GROUP BY company ORDER BY spilling DESC
    """))
    print("Spill events and baseline dry-spill flags:")
    print(con.sql("""
        SELECT company,
               COUNT(*)                                     AS events,
               COUNT(*) FILTER (WHERE has_rain_data)        AS with_rain_data,
               COUNT(*) FILTER (WHERE is_dry_spill_rule)    AS dry_by_rule,
               ROUND(MEDIAN(duration_hours), 1)             AS median_hours
        FROM fct_event_rainfall GROUP BY company ORDER BY events DESC
    """))


COMMANDS = {
    "collect": cmd_collect,
    "stations": cmd_stations,
    "backfill-rain": cmd_backfill_rain,
    "transform": cmd_transform,
    "check": cmd_check,
    "health": cmd_health,
    "summary": cmd_summary,
}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="spillwatch")
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("--days", type=int, default=7, help="days of rainfall to backfill")
    parser.add_argument("--force", action="store_true", help="re-download days already loaded")
    parser.add_argument("--strict", action="store_true", help="exit non-zero if any check fails")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    con = warehouse.connect()
    try:
        COMMANDS[args.command](con, make_session(), args)
    finally:
        con.close()


if __name__ == "__main__":
    main()

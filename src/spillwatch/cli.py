"""SpillWatch command-line entry point.

    python -m spillwatch.cli collect              # one overflow snapshot + latest rainfall
    python -m spillwatch.cli stations             # refresh rain gauge locations
    python -m spillwatch.cli backfill-rain --days 14
    python -m spillwatch.cli transform            # rebuild modelled tables
    python -m spillwatch.cli check [--strict]     # run data-quality checks
    python -m spillwatch.cli health               # pipeline + data-quality status
    python -m spillwatch.cli summary              # quick look at what's in the warehouse
    python -m spillwatch.cli history [--source S] # download historical spill events
    python -m spillwatch.cli rain-history         # download daily rainfall history
    python -m spillwatch.cli build-research       # build the site-day modelling table
    python -m spillwatch.cli train                # train + evaluate the suspicious-spill model
    python -m spillwatch.cli forecast-history     # download archived rain forecasts (test period)
    python -m spillwatch.cli train-forecast       # train + evaluate 1-2 day spill forecasting
    python -m spillwatch.cli cloud-collect        # (GitHub Actions) write overflow changes to --data-dir
    python -m spillwatch.cli sync-cloud           # pull cloud-collected data into the local warehouse
"""

import argparse
import json
import logging
from datetime import date, datetime, timedelta, timezone

from spillwatch import quality, warehouse
from spillwatch.http import make_session
from spillwatch.config import HISTORY_SOURCES
from spillwatch.ingest import history, hydrology, overflows, rainfall

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
        # Median sites per company over the last day, to spot partially reloaded feeds
        typical = dict(con.execute("""
            SELECT company, MEDIAN(n)::INT FROM (
                SELECT company, snapshot_at, COUNT(*) AS n FROM raw_overflow_snapshots
                WHERE snapshot_at > now() - INTERVAL 24 HOUR GROUP BY ALL)
            GROUP BY company
        """).fetchall())
        df = overflows.collect_snapshot(session, typical_counts=typical)
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


def cmd_history(_con, session, args):
    sources = [args.source] if args.source else list(HISTORY_SOURCES)
    for source in sources:
        rows = history.download_source(session, source, force=args.force)
        log.info("%s: %d events saved", source, rows)


def cmd_rain_history(_con, session, args):
    hydrology.download_all(session, force=args.force)


def cmd_build_research(_con, _session, _args):
    import duckdb

    from spillwatch.config import RAW_DIR, RESEARCH_DB_PATH, SQL_DIR

    sql = (SQL_DIR / "research.sql").read_text().replace("{raw}", str(RAW_DIR))
    con = duckdb.connect(str(RESEARCH_DB_PATH))
    try:
        con.execute(sql)
        print(con.sql("""
            SELECT company, COUNT(DISTINCT site_id) AS sites, MIN(rain_day) AS first_day,
                   MAX(rain_day) AS last_day, COUNT(*) AS site_days,
                   SUM(spill_started::INT) AS spill_days,
                   ROUND(100 * AVG(CASE WHEN gauges > 0 THEN 1 ELSE 0 END), 1) AS pct_with_rain
            FROM site_day GROUP BY company ORDER BY company
        """))
    finally:
        con.close()


def cmd_train(_con, _session, _args):
    from spillwatch.ml import dry_spill

    res = dry_spill.run()
    print(json.dumps({"data": res.data, "prediction": res.prediction}, indent=2))


def cmd_forecast_history(_con, session, args):
    """Download archived forecasts for every 0.25-degree cell holding a test-period site."""
    import duckdb

    from spillwatch.config import RESEARCH_DB_PATH
    from spillwatch.ingest import forecasts
    from spillwatch.ml.forecast import FORECAST_END, FORECAST_START

    con = duckdb.connect(str(RESEARCH_DB_PATH), read_only=True)
    sites = con.execute("""
        SELECT DISTINCT s.latitude, s.longitude FROM hist_sites s
        JOIN (SELECT DISTINCT company, site_id FROM site_day WHERE rain_day >= ?) d USING (company, site_id)
    """, [FORECAST_START]).fetchall()
    con.close()
    cells = sorted({forecasts.cell_of(lat, lon) for lat, lon in sites})
    forecasts.download(session, cells, FORECAST_START, FORECAST_END, force=args.force)


def cmd_train_forecast(_con, _session, _args):
    from spillwatch.ml import forecast

    res = forecast.run()
    for h, r in res.items():
        print(f"\n== Horizon {r['horizon_days']} day(s): {r['test_site_days']:,} test site-days, "
              f"{r['test_active_rate']:.1%} active")
        for name, m in r.items():
            if isinstance(m, dict) and "roc_auc" in m:
                hit = next(v for k, v in m.items() if k.startswith("hit_rate"))
                far = next(v for k, v in m.items() if k.startswith("false_alarm"))
                print(f"  {name:<44} AUC {m['roc_auc']:.3f}  PR-AUC {m['pr_auc']:.3f}  "
                      f"hit rate {hit:.1%}  false alarms {far:.1%}")


def cmd_cloud_collect(_con, _session, args):
    """Used by GitHub Actions: write changed overflows into a git-tracked directory."""
    from pathlib import Path

    from spillwatch import cloud

    data_dir = Path(args.data_dir)
    run = cloud.collect(data_dir, typical_counts=cloud.typical_counts_from_runs(data_dir))
    log.info("cloud run: %s", run)


def cmd_sync_cloud(con, _session, _args):
    """Pull the cloud-collected data branch and load it into the local warehouse."""
    import subprocess

    from spillwatch import cloud
    from spillwatch.config import DATA_DIR, PROJECT_ROOT

    target = DATA_DIR / "cloud"
    if (target / ".git").exists():
        subprocess.run(["git", "-C", str(target), "pull", "--quiet", "--ff-only"], check=True)
    else:
        remote = subprocess.run(["git", "-C", str(PROJECT_ROOT), "remote", "get-url", "origin"],
                                check=True, capture_output=True, text=True).stdout.strip()
        subprocess.run(["git", "clone", "--quiet", "--branch", "data", "--single-branch", remote, str(target)],
                       check=True)
    rows = cloud.load_into_warehouse(con, target)
    runs = con.execute("SELECT COUNT(*), MIN(snapshot_at), MAX(snapshot_at) FROM raw_cloud_runs").fetchone()
    log.info("Loaded %d cloud rows from %d runs (%s to %s)", rows, *runs)


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
    "history": cmd_history,
    "rain-history": cmd_rain_history,
    "build-research": cmd_build_research,
    "train": cmd_train,
    "forecast-history": cmd_forecast_history,
    "train-forecast": cmd_train_forecast,
    "cloud-collect": cmd_cloud_collect,
    "sync-cloud": cmd_sync_cloud,
}

# Research commands use their own files/database and never lock the live warehouse
RESEARCH_COMMANDS = {"history", "rain-history", "build-research", "train", "forecast-history", "train-forecast", "cloud-collect"}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="spillwatch")
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("--days", type=int, default=7, help="days of rainfall to backfill")
    parser.add_argument("--force", action="store_true", help="re-download days already loaded")
    parser.add_argument("--source", choices=HISTORY_SOURCES, help="history: one source only")
    parser.add_argument("--data-dir", default="cloud-data", help="cloud-collect: output directory")
    parser.add_argument("--strict", action="store_true", help="exit non-zero if any check fails")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.command in RESEARCH_COMMANDS:
        COMMANDS[args.command](None, make_session(), args)
        return
    con = warehouse.connect()
    try:
        COMMANDS[args.command](con, make_session(), args)
    finally:
        con.close()


if __name__ == "__main__":
    main()

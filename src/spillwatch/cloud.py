"""Cloud collection: store overflow *changes* in a git-friendly layout.

Runs on a schedule (GitHub Actions) without a database. Each run fetches every
company feed and writes only the overflows whose state changed since the previous
run, plus a full keyframe once a week. History can always be rebuilt from the
latest keyframe and the change files after it.

Layout of the data directory (the repository's `data` branch):
    keyframes/date=YYYY-MM-DD/keyframe_HHMMSS.parquet   full snapshot, weekly
    changes/date=YYYY-MM-DD/changes_HHMMSS.parquet      changed overflows only
    runs/runs_YYYY-MM.csv                               one line per run (health)
"""

import csv
import glob
import json
import logging
from pathlib import Path

import pandas as pd

from spillwatch.http import make_session
from spillwatch.ingest import overflows

log = logging.getLogger(__name__)

STATE_COLUMNS = ["status", "status_start", "latest_event_start", "latest_event_end"]
CHANGE_COLUMNS = ["overflow_id", "company", *STATE_COLUMNS, "last_updated", "snapshot_at"]
KEYFRAME_WEEKDAY = 0  # Monday


def _latest_state(data_dir: Path) -> pd.DataFrame | None:
    """Rebuild the last known state from the newest keyframe plus later changes."""
    keyframes = sorted(glob.glob(str(data_dir / "keyframes" / "*" / "*.parquet")))
    if not keyframes:
        return None
    state = pd.read_parquet(keyframes[-1])
    kf_time = state["snapshot_at"].max()
    changes = [pd.read_parquet(f) for f in sorted(glob.glob(str(data_dir / "changes" / "*" / "*.parquet")))
               if _file_time(f) > kf_time]
    if changes:
        later = pd.concat(changes).sort_values("snapshot_at").drop_duplicates("overflow_id", keep="last")
        state = state.set_index("overflow_id")
        later = later.set_index("overflow_id")
        state.update(later[STATE_COLUMNS + ["last_updated", "snapshot_at"]])
        state = pd.concat([state, later.loc[later.index.difference(state.index)]]).reset_index()
    return state


def _file_time(path: str) -> pd.Timestamp:
    day = Path(path).parent.name.split("=")[1]
    hhmmss = Path(path).stem.split("_")[-1]
    return pd.Timestamp(f"{day} {hhmmss[:2]}:{hhmmss[2:4]}:{hhmmss[4:]}", tz="UTC")


def changed_rows(current: pd.DataFrame, previous: pd.DataFrame | None) -> pd.DataFrame:
    """Overflows that are new or whose status/event times differ from the previous state."""
    if previous is None:
        return current
    prev = previous.set_index("overflow_id")[STATE_COLUMNS]
    cur = current.set_index("overflow_id")
    common = cur.index.intersection(prev.index)
    a, b = cur.loc[common, STATE_COLUMNS], prev.loc[common, STATE_COLUMNS]
    differs = ((a != b) & ~(a.isna() & b.isna())).any(axis=1)
    new_ids = cur.index.difference(prev.index)
    return cur.loc[differs[differs].index.union(new_ids)].reset_index()


def collect(data_dir: Path, typical_counts: dict | None = None) -> dict:
    snap = overflows.collect_snapshot(make_session(), typical_counts=typical_counts)
    snapshot_at: pd.Timestamp = snap["snapshot_at"].iloc[0]
    day, stamp = snapshot_at.strftime("%Y-%m-%d"), snapshot_at.strftime("%H%M%S")

    previous = _latest_state(data_dir)
    needs_keyframe = previous is None or (
        snapshot_at.weekday() == KEYFRAME_WEEKDAY
        and not glob.glob(str(data_dir / "keyframes" / f"date={day}" / "*.parquet"))
    )
    if needs_keyframe:
        path = data_dir / "keyframes" / f"date={day}" / f"keyframe_{stamp}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        snap.to_parquet(path, index=False)
        n_changes = len(snap)
        log.info("Wrote keyframe with %d overflows", len(snap))
    else:
        changes = changed_rows(snap, previous)[CHANGE_COLUMNS]
        n_changes = len(changes)
        if n_changes:
            path = data_dir / "changes" / f"date={day}" / f"changes_{stamp}.parquet"
            path.parent.mkdir(parents=True, exist_ok=True)
            changes.to_parquet(path, index=False)
        log.info("Wrote %d changed overflows", n_changes)

    counts = snap.groupby("company").size().to_dict()
    run = {"snapshot_at": snapshot_at.isoformat(), "overflows": len(snap), "rows_written": n_changes,
           "keyframe": needs_keyframe, "companies": json.dumps(counts, sort_keys=True)}
    runs_file = data_dir / "runs" / f"runs_{snapshot_at:%Y-%m}.csv"
    runs_file.parent.mkdir(parents=True, exist_ok=True)
    new_file = not runs_file.exists()
    with open(runs_file, "a", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(run))
        if new_file:
            writer.writeheader()
        writer.writerow(run)
    return run


def typical_counts_from_runs(data_dir: Path, last_n: int = 96) -> dict:
    """Median overflows per company over recent runs (for partial-feed detection)."""
    files = sorted(glob.glob(str(data_dir / "runs" / "runs_*.csv")))
    if not files:
        return {}
    runs = pd.concat(pd.read_csv(f) for f in files[-2:]).tail(last_n)
    per_company = pd.DataFrame([json.loads(c) for c in runs["companies"]])
    return per_company.median().round().astype(int).to_dict()


def load_into_warehouse(con, data_dir: Path) -> int:
    """Rebuild cloud tables in the local warehouse from the synced data branch."""
    con.execute("DELETE FROM raw_overflow_cloud")
    loaded = 0
    for kind, pattern in (("keyframe", "keyframes"), ("change", "changes")):
        files = glob.glob(str(data_dir / pattern / "*" / "*.parquet"))
        if not files:
            continue
        cols = ", ".join(CHANGE_COLUMNS)
        con.execute(f"""
            INSERT OR IGNORE INTO raw_overflow_cloud ({cols}, kind)
            SELECT {cols}, '{kind}' FROM read_parquet(?, union_by_name = true)
        """, [files])
    loaded = con.execute("SELECT COUNT(*) FROM raw_overflow_cloud").fetchone()[0]
    runs = glob.glob(str(data_dir / "runs" / "runs_*.csv"))
    con.execute("DELETE FROM raw_cloud_runs")
    if runs:
        con.execute("""
            INSERT INTO raw_cloud_runs
            SELECT CAST(snapshot_at AS TIMESTAMPTZ), overflows, rows_written, keyframe, companies
            FROM read_csv(?, union_by_name = true, all_varchar = true)
        """, [runs])
    return loaded


# --------------------------------------------------------------------------- rainfall

# 30-day antecedent rainfall for spills up to 7 days old, plus a margin
RAIN_DAYS = 38


def hourly_from_readings(df: pd.DataFrame) -> pd.DataFrame:
    """15-min readings -> hourly totals. A reading timestamped T covers the period ending at T."""
    if df.empty:
        return pd.DataFrame(columns=["measure_id", "station_id", "hour_start", "mm", "readings"])
    hour = (pd.to_datetime(df["reading_at"], utc=True) - pd.Timedelta(seconds=1)).dt.floor("h")
    return (df.assign(hour_start=hour)
              .groupby(["measure_id", "hour_start"], as_index=False)
              .agg(station_id=("station_id", "first"), mm=("rain_mm", "sum"), readings=("rain_mm", "size")))


def rain_file(data_dir: Path, day) -> Path:
    return data_dir / "rain_hourly" / f"date={day:%Y-%m-%d}" / "hourly.parquet"


def sync_rain(con, session, data_dir: Path, today, days: int = RAIN_DAYS) -> int:
    """Make the last `days` complete calendar days available in the data branch (fetching
    any that are missing), load them into raw_rain_hourly, and load today's partial
    readings into raw_rainfall_readings without storing them."""
    from datetime import timedelta

    from spillwatch import warehouse
    from spillwatch.ingest import rainfall

    fetched = 0
    for i in range(1, days + 1):
        day = today - timedelta(days=i)
        path = rain_file(data_dir, day)
        if path.exists():
            continue
        readings = rainfall.fetch_day(session, day)
        if readings.empty:
            # Older than the readings API keeps (~4 weeks): use the daily archive file
            try:
                readings = rainfall.fetch_archive_day(session, day)
            except Exception as exc:
                log.warning("No rainfall available for %s: %s", day, exc)
                continue
        hourly = hourly_from_readings(readings)
        path.parent.mkdir(parents=True, exist_ok=True)
        hourly.to_parquet(path, index=False)
        fetched += 1
    files = [str(rain_file(data_dir, today - timedelta(days=i))) for i in range(1, days + 1)]
    files = [f for f in files if Path(f).exists()]
    if files:
        con.execute("""
            INSERT OR IGNORE INTO raw_rain_hourly (measure_id, station_id, hour_start, mm, readings)
            SELECT measure_id, station_id, hour_start, mm, readings FROM read_parquet(?)
        """, [files])
    warehouse.insert_ignore(con, "raw_rainfall_readings", rainfall.fetch_day(session, today))
    log.info("Rainfall: %d stored days available, %d fetched this run", len(files), fetched)
    return fetched


def export_rain(con, data_dir: Path, today, days: int = RAIN_DAYS) -> int:
    """Seed the data branch from a local warehouse (complete calendar days only)."""
    from datetime import timedelta

    written = 0
    for i in range(1, days + 1):
        day = today - timedelta(days=i)
        path = rain_file(data_dir, day)
        if path.exists():
            continue
        df = con.execute("""
            SELECT measure_id, station_id, hour_start, mm, readings FROM rain_hourly
            WHERE hour_start >= ? AND hour_start < ? + INTERVAL 1 DAY
        """, [day, day]).df()
        if df.empty or df["hour_start"].nunique() < 24:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(path, index=False)
        written += 1
    return written

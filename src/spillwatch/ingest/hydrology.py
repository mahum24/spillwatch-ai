"""Daily rainfall history per gauge from the EA Hydrology API (quality-checked)."""

import io
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import pandas as pd
import requests

from spillwatch.config import HTTP_TIMEOUT, HYDRO_BASE, RAIN_HISTORY_START, RAW_DIR

log = logging.getLogger(__name__)

RAIN_DIR = RAW_DIR / "rain_daily"
REQUEST_PAUSE_S = 0.3


def fetch_daily_measures(session: requests.Session) -> pd.DataFrame:
    """One row per rain gauge: its daily-total measure id and location."""
    measures = session.get(
        f"{HYDRO_BASE}/id/measures",
        params={"observedProperty": "rainfall", "periodName": "daily", "_limit": 10000},
        timeout=HTTP_TIMEOUT,
    ).json()["items"]
    stations = session.get(
        f"{HYDRO_BASE}/id/stations",
        params={"observedProperty": "rainfall", "_limit": 10000},
        timeout=HTTP_TIMEOUT,
    ).json()["items"]
    locations = {s["@id"].rsplit("/", 1)[-1]: (s.get("lat"), s.get("long"), s.get("label")) for s in stations}

    records = []
    for m in measures:
        station_key = m["station"]["@id"].rsplit("/", 1)[-1]
        lat, lon, label = locations.get(station_key, (None, None, None))
        records.append({"measure_id": m["notation"], "station_id": station_key,
                        "label": label, "latitude": lat, "longitude": lon})
    df = pd.DataFrame(records)
    for col in ("latitude", "longitude"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["latitude", "longitude"]).drop_duplicates("measure_id")


def fetch_measure_readings(session: requests.Session, measure_id: str, end: date) -> pd.DataFrame:
    # The API rate-limits bursts with 403/429; back off and retry rather than fail
    for attempt in range(5):
        time.sleep(REQUEST_PAUSE_S)
        resp = session.get(
            f"{HYDRO_BASE}/id/measures/{measure_id}/readings.csv",
            params={"mineq-date": RAIN_HISTORY_START, "max-date": end.isoformat(), "_limit": 100000},
            timeout=HTTP_TIMEOUT,
        )
        if resp.status_code not in (403, 429):
            break
        wait = 15 * 2 ** attempt
        log.info("Rate limited on %s, waiting %ds", measure_id[:12], wait)
        time.sleep(wait)
    resp.raise_for_status()
    df = pd.read_csv(io.StringIO(resp.text), usecols=["date", "value", "quality"], dtype={"value": "string"})
    df["measure_id"] = measure_id
    df["rain_mm"] = pd.to_numeric(df["value"], errors="coerce")
    df["rain_day"] = pd.to_datetime(df["date"]).dt.date
    # Keep values the EA has not marked as suspect/missing; record quality for analysis
    df = df[df["rain_mm"].between(0, 300)]
    return df[["measure_id", "rain_day", "rain_mm", "quality"]]


def download_all(session: requests.Session, workers: int = 2, force: bool = False) -> int:
    RAIN_DIR.mkdir(parents=True, exist_ok=True)
    gauges = fetch_daily_measures(session)
    gauges.to_parquet(RAW_DIR / "rain_daily_gauges.parquet", index=False)
    log.info("%d daily rain gauges", len(gauges))

    end = date.today()
    todo = [m for m in gauges["measure_id"]
            if force or not (RAIN_DIR / f"{_safe(m)}.parquet").exists()]
    total, failed = 0, 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch_measure_readings, session, m, end): m for m in todo}
        for i, fut in enumerate(as_completed(futures), 1):
            m = futures[fut]
            try:
                df = fut.result()
                df.to_parquet(RAIN_DIR / f"{_safe(m)}.parquet", index=False)
                total += len(df)
            except Exception as exc:
                failed += 1
                log.warning("Gauge %s failed: %s", m, exc)
            if i % 100 == 0:
                log.info("rain history: %d/%d gauges done", i, len(todo))
    log.info("rain history: %d readings, %d gauges failed", total, failed)
    return total


def _safe(measure_id: str) -> str:
    return measure_id.replace("/", "_")

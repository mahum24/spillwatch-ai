"""Environment Agency rainfall: gauge locations, latest readings and daily archive backfill."""

import logging
import tempfile
from datetime import date
from pathlib import Path

import duckdb
import pandas as pd
import requests

from spillwatch.config import EA_ARCHIVE_URL, EA_BASE, HTTP_TIMEOUT

log = logging.getLogger(__name__)

READING_COLUMNS = ["station_id", "measure_id", "reading_at", "rain_mm"]


def fetch_stations(session: requests.Session) -> pd.DataFrame:
    resp = session.get(
        f"{EA_BASE}/id/stations", params={"parameter": "rainfall", "_limit": 10000}, timeout=HTTP_TIMEOUT
    )
    resp.raise_for_status()
    records = [
        {
            "station_id": item.get("stationReference") or item.get("notation"),
            "label": item.get("label"),
            "latitude": item.get("lat"),
            "longitude": item.get("long"),
        }
        for item in resp.json()["items"]
    ]
    df = pd.DataFrame(records)
    # A few stations have multiple/missing coordinates; keep only clean single points
    df["latitude"] = pd.to_numeric(df["latitude"], errors="coerce")
    df["longitude"] = pd.to_numeric(df["longitude"], errors="coerce")
    return df.dropna(subset=["station_id", "latitude", "longitude"]).drop_duplicates("station_id")


def fetch_latest(session: requests.Session) -> pd.DataFrame:
    """Most recent 15-minute rainfall total for every gauge."""
    resp = session.get(
        f"{EA_BASE}/data/readings", params={"parameter": "rainfall", "latest": ""}, timeout=HTTP_TIMEOUT
    )
    resp.raise_for_status()
    records = [
        {
            "measure_id": item["measure"].rsplit("/", 1)[-1],
            "reading_at": item["dateTime"],
            "rain_mm": item.get("value"),
        }
        for item in resp.json()["items"]
    ]
    return _finalise(pd.DataFrame(records))


def fetch_archive_day(session: requests.Session, day: date) -> pd.DataFrame:
    """All rainfall readings for one day from the EA archive (~150MB CSV, all parameters)."""
    url = EA_ARCHIVE_URL.format(day=day.isoformat())
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "archive.csv"
        with session.get(url, stream=True, timeout=HTTP_TIMEOUT) as resp:
            resp.raise_for_status()
            with open(path, "wb") as fh:
                for block in resp.iter_content(chunk_size=1 << 20):
                    fh.write(block)
        # DuckDB filters the ~1.5M-row file to rainfall only far faster than pandas
        df = duckdb.sql(f"""
            SELECT regexp_extract(measure, '[^/]+$') AS measure_id,
                   dateTime AS reading_at,
                   CAST(value AS VARCHAR) AS rain_mm
            FROM read_csv('{path}', all_varchar = true)
            WHERE parameter = 'rainfall'
        """).df()
    log.info("Archive %s: %d rainfall readings", day, len(df))
    return _finalise(df)


def _finalise(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=READING_COLUMNS)
    # Measure ids look like "E7050-rainfall-tipping_bucket_raingauge-t-15_min-mm"
    df["station_id"] = df["measure_id"].str.split("-rainfall", n=1).str[0]
    df["reading_at"] = pd.to_datetime(df["reading_at"], utc=True, errors="coerce")
    # Occasional values arrive as "0.2|0.4" when a reading was revised; take the first
    df["rain_mm"] = pd.to_numeric(df["rain_mm"].astype("string").str.split("|").str[0], errors="coerce")
    df = df.dropna(subset=["reading_at", "rain_mm"])
    # Negative or absurd 15-min totals are sensor faults
    df = df[(df["rain_mm"] >= 0) & (df["rain_mm"] < 100)]
    return df[READING_COLUMNS]

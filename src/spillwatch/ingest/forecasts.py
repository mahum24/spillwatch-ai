"""Archived rainfall forecasts (Open-Meteo Previous Runs API) for honest forecast evaluation.

`precipitation_previous_day1` for an hour is what the model predicted for that hour
~24 h beforehand; `_previous_day2` ~48 h beforehand. Using these (rather than the rain
that actually fell) shows how well spill forecasting works with real, imperfect
forecasts. Sites are grouped into 0.25° cells to stay within the free API limits.
"""

import logging
import time

import pandas as pd
import requests

from spillwatch.config import HTTP_TIMEOUT, RAW_DIR

log = logging.getLogger(__name__)

PREVIOUS_RUNS_URL = "https://previous-runs-api.open-meteo.com/v1/forecast"
FORECAST_DIR = RAW_DIR / "forecast_archive"
CELL_DEG = 0.25
# Free tier: 600 weighted calls/min, 5,000/hour. A 14-month request weighs ~31 calls,
# so pace requests to ~15 locations per minute.
PAUSE_S = 4.0


def cell_of(lat: float, lon: float) -> tuple[float, float]:
    return round(round(lat / CELL_DEG) * CELL_DEG, 4), round(round(lon / CELL_DEG) * CELL_DEG, 4)


def cell_key(lat: float, lon: float) -> str:
    return f"{lat:+08.4f}_{lon:+09.4f}"


def fetch_cell(session: requests.Session, lat: float, lon: float, start: str, end: str) -> pd.DataFrame:
    for attempt in range(5):
        resp = session.get(PREVIOUS_RUNS_URL, params={
            "latitude": lat, "longitude": lon,
            "hourly": "precipitation_previous_day1,precipitation_previous_day2",
            "start_date": start, "end_date": end, "timezone": "GMT",
        }, timeout=HTTP_TIMEOUT)
        if resp.status_code != 429:
            break
        wait = 60 * (attempt + 1)
        log.info("Rate limited, waiting %ds", wait)
        time.sleep(wait)
    resp.raise_for_status()
    hourly = resp.json()["hourly"]
    return pd.DataFrame({
        "cell_lat": lat, "cell_lon": lon,
        # Each value is the total for the hour *ending* at this time (GMT)
        "valid_hour_end": pd.to_datetime(hourly["time"]).tz_localize("UTC"),
        "fc_day1_mm": pd.to_numeric(pd.Series(hourly["precipitation_previous_day1"]), errors="coerce"),
        "fc_day2_mm": pd.to_numeric(pd.Series(hourly["precipitation_previous_day2"]), errors="coerce"),
    })


def download(session: requests.Session, cells: list[tuple[float, float]], start: str, end: str,
             force: bool = False) -> int:
    FORECAST_DIR.mkdir(parents=True, exist_ok=True)
    todo = [c for c in cells if force or not (FORECAST_DIR / f"{cell_key(*c)}.parquet").exists()]
    log.info("Forecast archive: %d cells to download (%d already done)", len(todo), len(cells) - len(todo))
    rows = 0
    for i, (lat, lon) in enumerate(todo, 1):
        try:
            df = fetch_cell(session, lat, lon, start, end)
            df.to_parquet(FORECAST_DIR / f"{cell_key(lat, lon)}.parquet", index=False)
            rows += len(df)
        except Exception as exc:
            log.warning("Cell %s,%s failed: %s", lat, lon, exc)
        if i % 20 == 0:
            log.info("forecast archive: %d/%d cells", i, len(todo))
        time.sleep(PAUSE_S)
    return rows

"""Historical spill events (one row per spill) from water companies' EDM services.

Each source is downloaded once to a parquet file in the raw landing zone; the
research database is then built from those files.
"""

import logging

import pandas as pd
import requests

from spillwatch.config import HISTORY_SOURCES, RAW_DIR
from spillwatch.ingest.overflows import fetch_layer

log = logging.getLogger(__name__)

HISTORY_DIR = RAW_DIR / "history"
COLUMNS = ["source", "company", "site_id", "event_start", "event_end", "latitude", "longitude"]


def parse_times(values: pd.Series, time_format: str | None, timezone: str | None = None) -> pd.Series:
    """Parse epoch-ms numbers, ISO strings or a custom format into UTC timestamps."""
    numeric = pd.to_numeric(values, errors="coerce")
    if numeric.notna().any():
        return pd.to_datetime(numeric.astype("Int64"), unit="ms", utc=True)
    if time_format:
        parsed = pd.to_datetime(values, format=time_format, errors="coerce")
        # Ambiguous/non-existent local times at clock changes become NaT rather than guessed
        return parsed.dt.tz_localize(timezone or "UTC", ambiguous="NaT", nonexistent="NaT").dt.tz_convert("UTC")
    return pd.to_datetime(values, utc=True, errors="coerce", format="ISO8601")


def normalise_history(rows: list[dict], source: str, spec: dict) -> pd.DataFrame:
    raw = pd.DataFrame(rows)
    df = pd.DataFrame({
        "source": source,
        "company": spec["company"],
        "site_id": raw[spec["site"]].astype("string").str.strip(),
        "event_start": parse_times(raw[spec["start"]], spec["time_format"], spec.get("timezone")),
        "event_end": parse_times(raw[spec["end"]], spec["time_format"], spec.get("timezone")),
        "latitude": pd.to_numeric(raw[spec["lat"]], errors="coerce"),
        "longitude": pd.to_numeric(raw[spec["lon"]], errors="coerce"),
    })
    before = len(df)
    df = df.dropna(subset=["site_id", "event_start", "event_end"])
    df = df[df["event_end"] >= df["event_start"]]
    df = df.drop_duplicates(["site_id", "event_start", "event_end"])
    log.info("%s: %d events kept of %d (%d dropped: missing/invalid/duplicate)",
             source, len(df), before, before - len(df))
    return df[COLUMNS]


def download_source(session: requests.Session, source: str, force: bool = False) -> int:
    path = HISTORY_DIR / f"{source}.parquet"
    if path.exists() and not force:
        log.info("%s: already downloaded, skipping", source)
        return 0
    spec = HISTORY_SOURCES[source]
    log.info("%s: downloading from %s", source, spec["url"])
    rows = fetch_layer(session, spec["url"])
    df = normalise_history(rows, source, spec)
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)
    return len(df)

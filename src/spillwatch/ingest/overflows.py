"""Collect a snapshot of every storm overflow's live status from all company feeds.

The feeds only expose *current* state (status + latest event), not history, so
history is built by polling regularly and storing every snapshot.
"""

import logging
from datetime import datetime, timezone

import pandas as pd
import requests

from spillwatch.config import HTTP_TIMEOUT, OVERFLOW_FEEDS

log = logging.getLogger(__name__)

# Feeds differ in casing (e.g. "Status" vs "status"), so match case-insensitively.
FIELD_MAP = {
    "id": "site_id",
    "company": "company_name",
    "status": "status",
    "statusstart": "status_start",
    "latesteventstart": "latest_event_start",
    "latesteventend": "latest_event_end",
    "latitude": "latitude",
    "longitude": "longitude",
    "receivingwatercourse": "receiving_water",
    "lastupdated": "last_updated",
}
DATE_COLUMNS = ["status_start", "latest_event_start", "latest_event_end", "last_updated"]
OUTPUT_COLUMNS = ["overflow_id", "company", *FIELD_MAP.values(), "snapshot_at"]


def fetch_layer(session: requests.Session, layer_url: str) -> list[dict]:
    """Page through an ArcGIS FeatureServer layer and return all attribute dicts."""
    meta = session.get(layer_url, params={"f": "json"}, timeout=HTTP_TIMEOUT).json()
    page_size = meta.get("maxRecordCount", 1000)
    oid_field = next(f["name"] for f in meta["fields"] if f["type"] == "esriFieldTypeOID")

    rows, offset = [], 0
    while True:
        resp = session.get(
            f"{layer_url}/query",
            params={
                "where": "1=1",
                "outFields": "*",
                "returnGeometry": "false",
                "orderByFields": oid_field,
                "resultOffset": offset,
                "resultRecordCount": page_size,
                "f": "json",
            },
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        payload = resp.json()
        if "error" in payload:
            raise RuntimeError(f"ArcGIS error from {layer_url}: {payload['error']}")
        features = payload.get("features", [])
        rows.extend(f["attributes"] for f in features)
        if not features or not payload.get("exceededTransferLimit"):
            break
        offset += len(features)
    return rows


def normalise(rows: list[dict], company: str, snapshot_at: datetime) -> pd.DataFrame:
    """Map a feed's raw attributes onto the canonical snapshot schema."""
    records = []
    for row in rows:
        lowered = {k.lower(): v for k, v in row.items()}
        records.append({out: lowered.get(src) for src, out in FIELD_MAP.items()})

    df = pd.DataFrame(records, columns=list(FIELD_MAP.values()))
    df["company"] = company
    df["site_id"] = df["site_id"].astype("string").str.strip()
    # Site ids are only unique within a company
    df["overflow_id"] = company + ":" + df["site_id"]
    df["status"] = pd.to_numeric(df["status"], errors="coerce").astype("Int64")
    for col in DATE_COLUMNS:
        # ArcGIS returns dates as epoch milliseconds (UTC)
        df[col] = pd.to_datetime(pd.to_numeric(df[col], errors="coerce"), unit="ms", utc=True)
    df["latitude"] = pd.to_numeric(df["latitude"], errors="coerce")
    df["longitude"] = pd.to_numeric(df["longitude"], errors="coerce")
    df["snapshot_at"] = pd.Timestamp(snapshot_at)
    return df[OUTPUT_COLUMNS]


def collect_snapshot(session: requests.Session, feeds: dict[str, str] = OVERFLOW_FEEDS) -> pd.DataFrame:
    """Fetch every feed; a failing company is logged and skipped, not fatal."""
    snapshot_at = datetime.now(timezone.utc).replace(microsecond=0)
    frames = []
    for company, url in feeds.items():
        try:
            rows = fetch_layer(session, url)
        except Exception:
            log.exception("Failed to fetch %s", company)
            continue
        df = normalise(rows, company, snapshot_at)
        log.info("%-18s %5d overflows, %3d spilling", company, len(df), int((df["status"] == 1).sum()))
        frames.append(df)
    if not frames:
        raise RuntimeError("No overflow feeds could be fetched")
    return pd.concat(frames, ignore_index=True)

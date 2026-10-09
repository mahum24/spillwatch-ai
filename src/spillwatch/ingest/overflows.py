"""Collect a snapshot of every storm overflow's live status from all company feeds.

The feeds only expose *current* state (status + latest event), not history, so
history is built by polling regularly and storing every snapshot.
"""

import logging
import time
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
MIN_EPOCH_MS = 946_684_800_000    # 2000-01-01
MAX_EPOCH_MS = 4_102_444_800_000  # 2100-01-01
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
        # ArcGIS returns dates as epoch milliseconds (UTC). Feeds caught mid-refresh
        # have returned garbage values, so anything outside 2000-2100 becomes NaT.
        millis = pd.to_numeric(df[col], errors="coerce")
        millis = millis.where(millis.between(MIN_EPOCH_MS, MAX_EPOCH_MS))
        # Convert via nullable Int64: pandas 2.3's float path can raise a spurious
        # FloatingPointError on columns containing NaN.
        df[col] = pd.to_datetime(millis.astype("Int64"), unit="ms", utc=True)
    df["latitude"] = pd.to_numeric(df["latitude"], errors="coerce")
    df["longitude"] = pd.to_numeric(df["longitude"], errors="coerce")
    df["snapshot_at"] = pd.Timestamp(snapshot_at)

    # Some feeds list a site more than once (e.g. Severn Trent keeps stale copies);
    # keep the most recently updated row per site.
    before = len(df)
    df = (df.sort_values("last_updated", ascending=False, na_position="last")
            .drop_duplicates("overflow_id", keep="first")
            .sort_index())
    if len(df) < before:
        log.warning("%s: removed %d duplicate site rows from source feed", company, before - len(df))
    return df[OUTPUT_COLUMNS]


def fetch_layer_consistent(session: requests.Session, layer_url: str, attempts: int = 3) -> list[dict]:
    """Fetch a layer and verify the pages formed one complete, non-overlapping read.

    Offset paging over a table that changes mid-read can return overlapping pages
    (repeated row ids) and silently skip others. Every row has a unique ArcGIS object
    id, so the read is valid only if the unique object ids match the server's count.
    """
    for attempt in range(1, attempts + 1):
        expected = session.get(
            f"{layer_url}/query",
            params={"where": "1=1", "returnCountOnly": "true", "f": "json"},
            timeout=HTTP_TIMEOUT,
        ).json().get("count")
        rows = fetch_layer(session, layer_url)
        unique_rows = len({_object_id(r) for r in rows})
        if unique_rows == expected == len(rows):
            return rows
        log.warning("%s: inconsistent read (expected %s, got %d rows / %d unique), attempt %d",
                    layer_url.split("/services/")[1].split("/")[0], expected, len(rows), unique_rows, attempt)
        time.sleep(10 * attempt)
    raise RuntimeError(f"Could not get a consistent read of {layer_url}")


def _object_id(row: dict):
    return next((v for k, v in row.items() if k.lower() in ("objectid", "fid")), None)


def collect_snapshot(session: requests.Session, feeds: dict[str, str] = OVERFLOW_FEEDS) -> pd.DataFrame:
    """Fetch every feed; a failing company is logged and skipped, not fatal."""
    snapshot_at = datetime.now(timezone.utc).replace(microsecond=0)
    frames = []
    for company, url in feeds.items():
        # One company's bad data must never stop the others being collected
        try:
            rows = fetch_layer_consistent(session, url)
            df = normalise(rows, company, snapshot_at)
        except Exception:
            log.exception("Failed to collect %s", company)
            continue
        log.info("%-18s %5d overflows, %3d spilling", company, len(df), int((df["status"] == 1).sum()))
        frames.append(df)
    if not frames:
        raise RuntimeError("No overflow feeds could be fetched")
    return pd.concat(frames, ignore_index=True)

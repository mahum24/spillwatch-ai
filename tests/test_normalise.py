from datetime import datetime, timezone

import pandas as pd

from spillwatch.ingest.overflows import normalise
from spillwatch.ingest.rainfall import _finalise

SNAP = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)


def test_normalise_handles_mixed_field_casing():
    title_case = {"Id": "NW001", "Company": "Northumbrian Water", "Status": 1,
                  "LatestEventStart": 1760000000000, "LatestEventEnd": None,
                  "Latitude": 54.57, "Longitude": -1.23, "ReceivingWaterCourse": "River Tees"}
    lower_case = {"Id": "SW9", "company": "South West Water", "status": 0,
                  "latestEventStart": 1760000000000, "latestEventEnd": 1760003600000,
                  "latitude": 50.4, "longitude": -4.1, "receivingWaterCourse": "Sea"}

    a = normalise([title_case], "northumbrian", SNAP).iloc[0]
    b = normalise([lower_case], "south_west", SNAP).iloc[0]

    assert a["overflow_id"] == "northumbrian:NW001"
    assert a["status"] == 1 and pd.isna(a["latest_event_end"])
    assert a["receiving_water"] == "River Tees"
    assert b["company_name"] == "South West Water"
    assert (b["latest_event_end"] - b["latest_event_start"]) == pd.Timedelta(hours=1)
    assert str(b["latest_event_start"].tz) == "UTC"


def test_normalise_empty_feed_has_schema():
    df = normalise([], "thames", SNAP)
    assert df.empty and "overflow_id" in df.columns


def test_rainfall_finalise_cleans_values():
    raw = pd.DataFrame({
        "measure_id": ["E7050-rainfall-tipping_bucket_raingauge-t-15_min-mm"] * 4,
        "reading_at": ["2026-10-01T00:00:00Z", "2026-10-01T00:15:00Z",
                       "2026-10-01T00:30:00Z", "2026-10-01T00:45:00Z"],
        "rain_mm": ["0.2", "0.4|0.6", "-1", "bad"],
    })
    df = _finalise(raw)
    assert list(df["rain_mm"]) == [0.2, 0.4]          # revised value split, fault + junk dropped
    assert set(df["station_id"]) == {"E7050"}


def test_normalise_nulls_out_of_range_timestamps():
    row = {"Id": "X1", "Status": 0, "LatestEventStart": 9.9e17, "LatestEventEnd": -5,
           "LastUpdated": 1760000000000}
    df = normalise([row], "severn_trent", SNAP)
    assert pd.isna(df.loc[0, "latest_event_start"])
    assert pd.isna(df.loc[0, "latest_event_end"])
    assert df.loc[0, "last_updated"].year == 2025

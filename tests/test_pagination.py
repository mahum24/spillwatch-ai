from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from spillwatch.ingest import overflows

URL = "https://example.com/arcgis/rest/services/Test_Feed/FeatureServer/0"


class FakeSession:
    """Returns the server-side row count; rows come from the patched fetch_layer."""

    def __init__(self, count):
        self.count = count

    def get(self, *_args, **_kwargs):
        count = self.count

        class Resp:
            def json(self_inner):
                return {"count": count}

        return Resp()


def _rows(object_ids):
    return [{"OBJECTID": oid, "Id": f"S{oid}"} for oid in object_ids]


def test_overlapping_pages_trigger_refetch(monkeypatch):
    monkeypatch.setattr(overflows.time, "sleep", lambda _s: None)
    good = _rows(range(100))
    overlapped = _rows(list(range(50)) + list(range(30, 80)))  # page 2 overlapped page 1
    with patch.object(overflows, "fetch_layer", side_effect=[overlapped, good]) as fetch:
        assert overflows.fetch_layer_consistent(FakeSession(100), URL) == good
    assert fetch.call_count == 2


def test_duplicate_sites_with_distinct_rows_are_a_valid_read():
    # Same site id twice but different object ids: a source-data issue, not a paging one
    rows = _rows(range(100))
    rows[99]["Id"] = "S5"
    with patch.object(overflows, "fetch_layer", return_value=rows) as fetch:
        assert overflows.fetch_layer_consistent(FakeSession(100), URL) == rows
    assert fetch.call_count == 1


def test_persistently_inconsistent_feed_raises(monkeypatch):
    monkeypatch.setattr(overflows.time, "sleep", lambda _s: None)
    with patch.object(overflows, "fetch_layer", return_value=_rows(range(60))):
        with pytest.raises(RuntimeError):
            overflows.fetch_layer_consistent(FakeSession(100), URL)


def test_normalise_keeps_most_recent_duplicate():
    snap = datetime(2026, 10, 9, tzinfo=timezone.utc)
    rows = [
        {"Id": "SVT01716", "Status": 0, "LastUpdated": 1791578894000},  # stale copy
        {"Id": "SVT01716", "Status": 1, "LastUpdated": 1791579737000},  # latest copy
        {"Id": "SVT00002", "Status": 0, "LastUpdated": 1791579000000},
    ]
    df = overflows.normalise(rows, "severn_trent", snap)
    assert len(df) == 2
    assert df.set_index("site_id").loc["SVT01716", "status"] == 1

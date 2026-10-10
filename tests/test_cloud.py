from datetime import datetime, timezone

import pandas as pd

from spillwatch import cloud
from spillwatch.ingest.overflows import normalise


def _snapshot(when, statuses, event_starts=None):
    """Snapshot with sites S0..Sn; statuses/event starts given per site."""
    rows = []
    for i, status in enumerate(statuses):
        start = (event_starts or {}).get(i, 1760000000000)
        rows.append({"Id": f"S{i}", "Status": status, "LatestEventStart": start,
                     "LatestEventEnd": start + 3_600_000, "LastUpdated": int(when.timestamp() * 1000)})
    return normalise(rows, "test", when)


def _run(monkeypatch, tmp_path, when, statuses, event_starts=None):
    snap = _snapshot(when, statuses, event_starts)
    monkeypatch.setattr(cloud.overflows, "collect_snapshot", lambda *_a, **_k: snap)
    monkeypatch.setattr(cloud, "make_session", lambda: None)
    return cloud.collect(tmp_path), snap


def test_first_run_writes_keyframe_then_only_changes(monkeypatch, tmp_path):
    t0 = datetime(2026, 10, 6, 10, 7, tzinfo=timezone.utc)   # a Monday
    t1 = datetime(2026, 10, 6, 10, 22, tzinfo=timezone.utc)
    t2 = datetime(2026, 10, 6, 10, 37, tzinfo=timezone.utc)

    run0, _ = _run(monkeypatch, tmp_path, t0, [0, 0, 0, 0])
    assert run0["keyframe"] and run0["rows_written"] == 4

    # S1 starts spilling with a new event: only S1 should be written
    run1, _ = _run(monkeypatch, tmp_path, t1, [0, 1, 0, 0], {1: 1760100000000})
    assert not run1["keyframe"] and run1["rows_written"] == 1

    # Nothing changes (only LastUpdated moves): nothing written
    run2, _ = _run(monkeypatch, tmp_path, t2, [0, 1, 0, 0], {1: 1760100000000})
    assert run2["rows_written"] == 0
    assert len(list((tmp_path / "runs").glob("*.csv"))) == 1


def test_state_rebuild_matches_latest_snapshot(monkeypatch, tmp_path):
    times = [datetime(2026, 10, 6, 10, m, tzinfo=timezone.utc) for m in (7, 22, 37)]
    sequences = [
        ([0, 0, 0], None),
        ([1, 0, 0], {0: 1760100000000}),
        ([0, 1, -1], {0: 1760100000000, 1: 1760200000000}),
    ]
    for when, (statuses, starts) in zip(times, sequences):
        _, latest = _run(monkeypatch, tmp_path, when, statuses, starts)

    state = cloud._latest_state(tmp_path).set_index("overflow_id").sort_index()
    latest = latest.set_index("overflow_id").sort_index()
    pd.testing.assert_frame_equal(state[cloud.STATE_COLUMNS], latest[cloud.STATE_COLUMNS],
                                  check_dtype=False)


def test_missing_company_rows_are_not_changes():
    t = datetime(2026, 10, 6, 10, 7, tzinfo=timezone.utc)
    prev = _snapshot(t, [0, 0, 0])
    cur = prev[prev["site_id"] != "S2"]          # S2's feed failed this run
    assert cloud.changed_rows(cur, prev).empty


def test_typical_counts_from_runs(tmp_path):
    (tmp_path / "runs").mkdir()
    pd.DataFrame({
        "snapshot_at": ["a", "b", "c"], "overflows": [3, 3, 2], "rows_written": [0, 0, 0],
        "keyframe": [False] * 3,
        "companies": ['{"x": 1342, "y": 994}', '{"x": 1342, "y": 994}', '{"x": 1000, "y": 994}'],
    }).to_csv(tmp_path / "runs" / "runs_2026-10.csv", index=False)
    assert cloud.typical_counts_from_runs(tmp_path) == {"x": 1342, "y": 994}

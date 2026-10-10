from datetime import date, datetime, timezone

import pandas as pd

from spillwatch import live


def test_issue_day_switches_at_0900_gmt():
    assert live.issue_day(datetime(2026, 10, 10, 8, 59, tzinfo=timezone.utc)) == date(2026, 10, 9)
    assert live.issue_day(datetime(2026, 10, 10, 9, 0, tzinfo=timezone.utc)) == date(2026, 10, 10)


def test_risk_levels():
    assert live.risk_level(0.75) == "high"
    assert live.risk_level(0.5) == "high"
    assert live.risk_level(0.3) == "elevated"
    assert live.risk_level(0.05) == "low"
    assert live.risk_level(float("nan")) is None


def _spill(**overrides):
    row = {"event_start": pd.Timestamp("2026-10-09 16:31", tz="UTC"), "is_ongoing": False, "duration_hours": 0.3,
           "rain_complete": True, "rain_day_mm": 0.3, "rain_prev_day_mm": 5.2, "rain_7d_mm": 15.5,
           "site_wet_median_rain_3d": 51.0, "p_spill": 0.0004, "model_flag": True, "rule_flag": False}
    row.update(overrides)
    return pd.Series(row)


def test_explanation_model_only_flag():
    text = live._explain(_spill())
    assert "Fri 09 Oct 17:31" in text                 # shown in UK local time (BST)
    assert "less than 0.1%" in text and "0.0%" not in text
    assert "usually spills after about 51 mm" in text
    assert "the simple dry-day rule does not" in text


def test_explanation_rule_only_and_unscored():
    rule_only = live._explain(_spill(model_flag=False, rule_flag=True, p_spill=0.08, rain_prev_day_mm=0.0))
    assert "8.0%" in rule_only and "model considers it plausible" in rule_only
    unscored = live._explain(_spill(rain_complete=False))
    assert "not complete yet" in unscored and "model put" not in unscored
    no_history = live._explain(_spill(site_wet_median_rain_3d=float("nan")))
    assert "usually spills" not in no_history

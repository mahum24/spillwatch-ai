import numpy as np
import pandas as pd

from spillwatch.ingest.history import parse_times
from spillwatch.ml import dry_spill


def test_parse_times_formats():
    epoch = parse_times(pd.Series([1767225600000]), None)
    assert str(epoch[0]) == "2026-01-01 00:00:00+00:00"

    iso = parse_times(pd.Series(["2025-01-05T19:19:16Z", "2025-12-04T12:30:00+00:00"]), None)
    assert iso.notna().all() and str(iso[0].tz) == "UTC"

    # Northumbrian: UK local time. Summer (BST) is UTC+1; the ambiguous hour at the
    # autumn clock change becomes NaT instead of being silently guessed.
    local = parse_times(pd.Series(["17/07/2022 10:00", "17/01/2022 10:00", "30/10/2022 01:30"]),
                        "%d/%m/%Y %H:%M", "Europe/London")
    assert str(local[0]) == "2022-07-17 09:00:00+00:00"
    assert str(local[1]) == "2022-01-17 10:00:00+00:00"
    assert pd.isna(local[2])


def test_rule_flags_dry_days_only():
    frame = pd.DataFrame({"rain_day_mm": [0.0, 0.1, 0.0, 3.0], "rain_prev_day_mm": [0.2, 0.0, 1.0, 0.0]})
    assert dry_spill.rule_flags(frame).tolist() == [True, True, False, False]


def test_top_fraction_exact_budget():
    rng = np.random.default_rng(0)
    flags = dry_spill._top_fraction(pd.Series([0.5] * 50 + [0.9] * 50), 0.1, rng)
    assert flags.sum() == 10


def _toy_site_days():
    days = pd.date_range("2024-01-01", "2025-12-31", freq="D")
    rng = np.random.default_rng(1)
    df = pd.DataFrame({
        "company": "c", "site_id": "S1", "rain_day": days,
        "rain_3d_mm": rng.uniform(0, 20, len(days)).astype("float32"),
    })
    df["spill_started"] = df["rain_3d_mm"] > 12
    df["is_test"] = df["rain_day"] >= dry_spill.TEST_START
    return df


def test_site_features_out_of_fold_has_no_leakage():
    df = dry_spill.add_site_features(_toy_site_days())
    train = df[~df["is_test"]]
    # Recompute the rate for one training fold by hand using only *other* folds
    fold = train["fold"].iloc[0]
    others = train[(train["fold"] != fold) & (train["rain_3d_mm"] >= dry_spill.WET_3D_MM)]
    prior = train[train["rain_3d_mm"] >= dry_spill.WET_3D_MM]["spill_started"].mean()
    expected = (others["spill_started"].sum() + 20 * prior) / (len(others) + 20)
    got = train.loc[train["fold"] == fold, "site_wet_spill_rate"].iloc[0]
    assert np.isclose(got, expected, rtol=1e-5)


def test_site_features_ignore_dry_weather_spills():
    """The normalisation trap: dry-day spills must not change a site's features."""
    base = _toy_site_days()
    habitual = base.copy()
    dry = habitual["rain_3d_mm"] < dry_spill.WET_3D_MM
    habitual.loc[dry, "spill_started"] = True  # site now spills on every dry day
    a = dry_spill.add_site_features(base)
    b = dry_spill.add_site_features(habitual)
    assert np.allclose(a["site_wet_spill_rate"], b["site_wet_spill_rate"])
    assert np.allclose(a["site_wet_median_rain_3d"], b["site_wet_median_rain_3d"], equal_nan=True)

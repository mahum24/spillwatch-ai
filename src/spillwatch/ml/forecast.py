"""Spill forecasting (thesis RQ2): will an overflow be spilling 1 or 2 days ahead?

Issue time is 09:00 GMT on day I, when rain day I-1 has been fully observed.
  horizon 1: target rain day T = I   (09:00 today -> 09:00 tomorrow)
  horizon 2: target rain day T = I+1 (tomorrow)
Observed rainfall and spill status are known up to rain day T-h; rainfall for the
remaining days comes from a forecast.

Training uses observed rain in place of forecasts ("perfect prognosis"). The test
period is then scored three ways to separate model skill from forecast skill:
  oracle    - observed rain (upper bound: a perfect weather forecast)
  forecast  - archived real forecasts made 1-2 days ahead (the honest result)
  no rain   - no forecast at all (only what is known at issue time)
"""

import json
import logging
from pathlib import Path

import duckdb
import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score

from spillwatch.config import DATA_DIR, RAW_DIR, RESEARCH_DB_PATH
from spillwatch.ml.dry_spill import N_FOLDS, SEED, TEST_START

log = logging.getLogger(__name__)

# Test period with archived forecasts (Previous Runs API archive starts Jan 2024).
# Starts 2 days before the test period so lagged features are available.
FORECAST_START = "2025-06-29"
FORECAST_END = "2026-08-31"
MODEL_DIR = DATA_DIR / "models" / "forecast"
WARNING_RATE = 0.10   # share of site-days on which a warning is issued

OBS_FEATURES = ["last_obs_rain_mm", "obs_rain_7d_mm", "obs_rain_14d_mm", "obs_rain_30d_mm",
                "obs_rain_max_day_7d_mm", "active_last_obs"]
SITE_FEATURES = ["site_active_rate", "site_wet_spill_rate", "site_wet_median_rain_3d"]
SEASON_FEATURES = ["month_sin", "month_cos"]
FUTURE_RAIN = {1: ["rain_T_mm"], 2: ["rain_Tm1_mm", "rain_T_mm"]}


def features(h: int, with_future_rain: bool = True) -> list[str]:
    return OBS_FEATURES + (FUTURE_RAIN[h] if with_future_rain else []) + SITE_FEATURES + SEASON_FEATURES


def monotonic(cols: list[str]) -> list[int]:
    rain = set(OBS_FEATURES + FUTURE_RAIN[2]) - {"active_last_obs"}
    return [1 if c in rain or c in ("active_last_obs", "site_active_rate", "site_wet_spill_rate")
            else -1 if c == "site_wet_median_rain_3d" else 0 for c in cols]


FORECAST_TABLES_SQL = """
SET TimeZone = 'UTC';
-- Archived hourly forecasts -> rain-day totals (09:00-09:00 GMT) per 0.25 deg cell.
-- An hourly value is the total for the hour ending at valid_hour_end.
CREATE OR REPLACE TABLE fc_rain_day AS
SELECT cell_lat, cell_lon,
       CAST(valid_hour_end - INTERVAL 9 HOUR - INTERVAL 1 SECOND AS DATE) AS rain_day,
       SUM(fc_day1_mm) AS fc1_mm, SUM(fc_day2_mm) AS fc2_mm, COUNT(*) AS hours
FROM read_parquet('{raw}/forecast_archive/*.parquet')
GROUP BY ALL
HAVING COUNT(*) = 24 AND COUNT(fc_day1_mm) = 24 AND COUNT(fc_day2_mm) = 24;

-- Each site's forecast cell: the nearest downloaded cell centre
CREATE OR REPLACE TABLE site_fc_cell AS
WITH cells AS (SELECT DISTINCT cell_lat, cell_lon FROM fc_rain_day)
SELECT s.company, s.site_id, c.cell_lat, c.cell_lon
FROM hist_sites s JOIN cells c
  ON ABS(c.cell_lat - s.latitude) <= 0.13 AND ABS(c.cell_lon - s.longitude) <= 0.13
QUALIFY ROW_NUMBER() OVER (PARTITION BY s.company, s.site_id
        ORDER BY ABS(c.cell_lat - s.latitude) + ABS(c.cell_lon - s.longitude)) = 1;
"""

PANEL_SQL = """
SELECT t.company, t.site_id, t.rain_day,
       (t.spill_started OR t.already_spilling)    AS spill_active,
       t.spill_started, t.rain_3d_mm,
       -- known at issue time (observed up to rain day T-h)
       o.rain_day_mm                              AS last_obs_rain_mm,
       o.rain_7d_mm                               AS obs_rain_7d_mm,
       o.rain_14d_mm                              AS obs_rain_14d_mm,
       o.rain_30d_mm                              AS obs_rain_30d_mm,
       o.rain_max_day_7d_mm                       AS obs_rain_max_day_7d_mm,
       CAST(o.spill_started OR o.already_spilling AS FLOAT) AS active_last_obs,
       -- future rain: observed values (training / oracle)
       t.rain_day_mm                              AS obs_rain_T_mm,
       t.rain_prev_day_mm                         AS obs_rain_Tm1_mm,
       -- future rain: archived forecasts at the matching lead time
       fT.fc{h}_mm                                AS fc_rain_T_mm,
       fG.fc1_mm                                  AS fc_rain_Tm1_mm
FROM site_day t
JOIN site_day o
  ON o.company = t.company AND o.site_id = t.site_id AND o.rain_day = t.rain_day - {h}
LEFT JOIN site_fc_cell c ON c.company = t.company AND c.site_id = t.site_id
LEFT JOIN fc_rain_day fT ON fT.cell_lat = c.cell_lat AND fT.cell_lon = c.cell_lon AND fT.rain_day = t.rain_day
LEFT JOIN fc_rain_day fG ON fG.cell_lat = c.cell_lat AND fG.cell_lon = c.cell_lon AND fG.rain_day = t.rain_day - 1
WHERE t.gauges > 0 AND o.gauges > 0 AND o.min_days_reported_30d >= 27
  AND t.rain_day_mm IS NOT NULL AND t.rain_prev_day_mm IS NOT NULL AND o.rain_day_mm IS NOT NULL
"""


def build_tables(db_path: Path = RESEARCH_DB_PATH) -> None:
    con = duckdb.connect(str(db_path))
    con.execute(FORECAST_TABLES_SQL.replace("{raw}", str(RAW_DIR)))
    n_days, n_sites = con.execute(
        "SELECT (SELECT COUNT(*) FROM fc_rain_day), (SELECT COUNT(*) FROM site_fc_cell)").fetchone()
    log.info("Forecast rain-days: %d; sites mapped to a forecast cell: %d", n_days, n_sites)
    con.close()


def load_panel(h: int, db_path: Path = RESEARCH_DB_PATH) -> pd.DataFrame:
    con = duckdb.connect(str(db_path), read_only=True)
    df = con.execute(PANEL_SQL.replace("{h}", str(h))).df()
    con.close()
    df["rain_day"] = pd.to_datetime(df["rain_day"])
    df["is_test"] = df["rain_day"] >= TEST_START
    # Fair comparison: test only where an archived forecast exists for every needed day
    needed = ["fc_rain_T_mm"] + (["fc_rain_Tm1_mm"] if h == 2 else [])
    df = df[~df["is_test"] | df[needed].notna().all(axis=1)].copy()
    month = df["rain_day"].dt.month
    df["month_sin"] = np.sin(2 * np.pi * month / 12).astype("float32")
    df["month_cos"] = np.cos(2 * np.pi * month / 12).astype("float32")
    return df


def add_site_features(df: pd.DataFrame) -> pd.DataFrame:
    """Out-of-fold site rates from training data only (test rows use all training data)."""
    from spillwatch.ml import dry_spill

    # Wet-weather sensitivity features, shared with the Phase 3 model
    df = dry_spill.add_site_features(df)
    key = ["company", "site_id"]
    train = df[~df["is_test"]]
    total = train.groupby(key)["spill_active"].agg(n="size", pos="sum")
    per_fold = train.groupby(key + ["fold"])["spill_active"].agg(fn="size", fpos="sum")
    df = df.join(total, on=key).join(per_fold, on=key + ["fold"])
    prior, k = train["spill_active"].mean(), 20
    # Sites never seen in training fall back to the overall rate (via the smoothing prior)
    df["n"], df["pos"] = df["n"].fillna(0), df["pos"].fillna(0)
    oof_n = df["n"] - df["fn"].fillna(0)
    oof_pos = df["pos"] - df["fpos"].fillna(0)
    df["site_active_rate"] = np.where(df["is_test"], (df["pos"] + k * prior) / (df["n"] + k),
                                      (oof_pos + k * prior) / (oof_n + k)).astype("float32")
    return df.drop(columns=["n", "pos", "fn", "fpos"])


def _fit(train: pd.DataFrame, cols: list[str]) -> HistGradientBoostingClassifier:
    model = HistGradientBoostingClassifier(
        max_iter=600, learning_rate=0.08, max_leaf_nodes=48, min_samples_leaf=200,
        l2_regularization=1.0, monotonic_cst=monotonic(cols),
        early_stopping=True, validation_fraction=0.1, n_iter_no_change=20, random_state=SEED)
    model.fit(train[cols], train["spill_active"])
    return model


def _metrics(y, p) -> dict:
    y = np.asarray(y, dtype=int)
    p = np.asarray(p, dtype=float)
    k = int(round(WARNING_RATE * len(p)))
    warned = np.zeros(len(p), dtype=bool)
    warned[np.argsort(-p, kind="stable")[:k]] = True
    hits = int((warned & (y == 1)).sum())
    return {"roc_auc": float(roc_auc_score(y, p)), "pr_auc": float(average_precision_score(y, p)),
            "brier": float(brier_score_loss(y, p)) if p.max() <= 1 else None,
            f"hit_rate@{WARNING_RATE:.0%}_warnings": hits / max(int(y.sum()), 1),
            f"false_alarm_ratio@{WARNING_RATE:.0%}_warnings": 1 - hits / max(k, 1)}


def run_horizon(h: int) -> dict:
    df = add_site_features(load_panel(h))
    train, test = df[~df["is_test"]].copy(), df[df["is_test"]].copy()
    future = FUTURE_RAIN[h]
    # Perfect prognosis: train on observed future rain
    for c in future:
        train[c] = train[f"obs_{c}"]
    log.info("h=%d: training on %d site-days (%.1f%% active)", h, len(train), 100 * train["spill_active"].mean())
    full = _fit(train, features(h))
    no_rain = _fit(train, features(h, with_future_rain=False))

    y = test["spill_active"]
    oracle = test.assign(**{c: test[f"obs_{c}"] for c in future})
    archived = test.assign(**{c: test[f"fc_{c}"] for c in future})
    out = {
        "horizon_days": h,
        "train_site_days": int(len(train)), "test_site_days": int(len(test)),
        "test_active_rate": float(y.mean()),
        "climatology (site rate)": _metrics(y, test["site_active_rate"]),
        "persistence (active on last observed day)": _metrics(
            y, test["active_last_obs"] + 1e-6 * test["site_active_rate"]),  # tie-break by site rate
        "model, no rain forecast": _metrics(y, no_rain.predict_proba(test[features(h, False)])[:, 1]),
        "model + archived forecast (realistic)": _metrics(y, full.predict_proba(archived[features(h)])[:, 1]),
        "model + observed rain (oracle)": _metrics(y, full.predict_proba(oracle[features(h)])[:, 1]),
    }
    # Forecast quality itself: how well did the archived forecast match gauge rain?
    for c in future:
        obs, fc = test[f"obs_{c}"], test[f"fc_{c}"]
        out[f"forecast_check_{c}"] = {
            "corr": float(np.corrcoef(obs, fc)[0, 1]),
            "mean_obs_mm": float(obs.mean()), "mean_fc_mm": float(fc.mean()),
            "wet_day_hit_rate": float(((fc >= 1) & (obs >= 1)).sum() / max((obs >= 1).sum(), 1)),
        }
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    joblib.dump({"model": full, "features": features(h)}, MODEL_DIR / f"model_h{h}.joblib")
    return out


def run() -> dict:
    build_tables()
    results = {f"h{h}": run_horizon(h) for h in (1, 2)}
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    (MODEL_DIR / "metrics.json").write_text(json.dumps(results, indent=2))
    return results

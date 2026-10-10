"""Suspicious-spill detection (thesis RQ1).

The model learns P(spill starts on a rain day | rainfall history, site's wet-weather
behaviour, season). A real spill the model considered very unlikely given the
weather is "unexpected", and therefore suspicious.

Because there are no ground-truth labels for illegal spills, detection is evaluated
by injecting synthetic weather-independent spills into held-out data and measuring
how many each method recovers at the same flagging budget (see `evaluate`).
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import average_precision_score, brier_score_loss, log_loss, roc_auc_score

from spillwatch.config import DATA_DIR, RESEARCH_DB_PATH

log = logging.getLogger(__name__)

MODEL_DIR = DATA_DIR / "models" / "dry_spill"
TEST_START = "2025-07-01"
WET_3D_MM = 5.0          # a "wet" spell for site wet-weather features
DRY_DAY_MM = 0.25        # baseline rule: < 0.25 mm on the rain day and the day before
N_FOLDS = 5
SEED = 42

RAIN_FEATURES = ["rain_day_mm", "rain_prev_day_mm", "rain_3d_mm", "rain_7d_mm",
                 "rain_14d_mm", "rain_30d_mm", "rain_max_day_7d_mm"]
SITE_FEATURES = ["site_wet_spill_rate", "site_wet_median_rain_3d"]
SEASON_FEATURES = ["month_sin", "month_cos"]
FEATURES = RAIN_FEATURES + SITE_FEATURES + SEASON_FEATURES
# More rain can only make a spill more likely; a site that spills more often in wet
# weather is more spill-prone; a site needing more rain is less spill-prone.
MONOTONIC = [1] * len(RAIN_FEATURES) + [1, -1] + [0, 0]


def load_site_days(db_path: Path = RESEARCH_DB_PATH) -> pd.DataFrame:
    con = duckdb.connect(str(db_path), read_only=True)
    df = con.execute(f"""
        SELECT company, site_id, rain_day, spill_started, spill_hours,
               {', '.join(RAIN_FEATURES)}, gauges, nearest_gauge_km
        FROM site_day
        WHERE NOT already_spilling            -- a new spill cannot start mid-spill
          AND gauges > 0
          AND min_days_reported_30d >= 27     -- rainfall history ~complete
          -- the baseline rule needs these two values; compare both methods on the same days
          AND rain_day_mm IS NOT NULL AND rain_prev_day_mm IS NOT NULL
    """).df()
    con.close()
    df["rain_day"] = pd.to_datetime(df["rain_day"])
    month = df["rain_day"].dt.month
    df["month_sin"] = np.sin(2 * np.pi * month / 12)
    df["month_cos"] = np.cos(2 * np.pi * month / 12)
    df["is_test"] = df["rain_day"] >= TEST_START
    for col in RAIN_FEATURES + SEASON_FEATURES:
        df[col] = df[col].astype("float32")
    return df


def add_site_features(df: pd.DataFrame) -> pd.DataFrame:
    """Site wet-weather behaviour, computed without target leakage.

    Training rows get out-of-fold values (folds are blocks of months, so a row never
    sees its own month's labels); test rows get values from all training data.
    Only wet days are used, so habitual dry-weather spilling is never "learned" as
    normal for a site (the normalisation trap).
    """
    df = df.copy()
    month_index = df["rain_day"].dt.year * 12 + df["rain_day"].dt.month
    df["fold"] = (month_index % N_FOLDS).astype(int)
    wet = df["rain_3d_mm"] >= WET_3D_MM
    key = ["company", "site_id"]
    train_wet = df[~df["is_test"] & wet]

    def stats(frame: pd.DataFrame) -> pd.DataFrame:
        g = frame.groupby(key)
        return pd.DataFrame({
            "n": g.size(),
            "pos": g["spill_started"].sum(),
            "median_rain": frame[frame["spill_started"]].groupby(key)["rain_3d_mm"].median(),
        })

    total = stats(train_wet)
    df = df.join(total.rename(columns=lambda c: f"all_{c}"), on=key)

    # Out-of-fold for training rows
    per_fold = train_wet.groupby(key + ["fold"]).agg(n=("spill_started", "size"),
                                                     pos=("spill_started", "sum"))
    df = df.join(per_fold.rename(columns=lambda c: f"fold_{c}"), on=key + ["fold"])
    oof_n = df["all_n"] - df["fold_n"].fillna(0)
    oof_pos = df["all_pos"] - df["fold_pos"].fillna(0)
    # Light smoothing towards the global wet-day rate for sites with few wet days
    prior = train_wet["spill_started"].mean()
    k = 20
    df["site_wet_spill_rate"] = np.where(
        df["is_test"],
        (df["all_pos"] + k * prior) / (df["all_n"] + k),
        (oof_pos + k * prior) / (oof_n + k),
    )
    # Median rain on wet spill days: OOF per fold for training rows
    oof_median = {}
    for f in range(N_FOLDS):
        sub = train_wet[(train_wet["fold"] != f) & train_wet["spill_started"]]
        oof_median[f] = sub.groupby(key)["rain_3d_mm"].median()
    medians = pd.concat(oof_median, names=["fold"]).rename("oof_median").reset_index()
    df = df.merge(medians, on=key + ["fold"], how="left")
    df["site_wet_median_rain_3d"] = np.where(df["is_test"], df["all_median_rain"], df["oof_median"])

    df[SITE_FEATURES] = df[SITE_FEATURES].astype("float32")
    return df.drop(columns=[c for c in df.columns if c.startswith(("all_", "fold_", "oof_"))])


def train(df: pd.DataFrame) -> HistGradientBoostingClassifier:
    tr = df[~df["is_test"]]
    model = HistGradientBoostingClassifier(
        max_iter=400, learning_rate=0.08, max_leaf_nodes=48, min_samples_leaf=200,
        l2_regularization=1.0, monotonic_cst=MONOTONIC,
        early_stopping=True, validation_fraction=0.1, n_iter_no_change=20,
        random_state=SEED,
    )
    log.info("Training on %d site-days (%d spill starts)", len(tr), int(tr["spill_started"].sum()))
    model.fit(tr[FEATURES], tr["spill_started"])
    log.info("Stopped after %d iterations", model.n_iter_)
    return model


def rule_flags(frame: pd.DataFrame) -> pd.Series:
    """Baseline: a spill on a 'dry day' (< 0.25 mm on the rain day and the day before)."""
    return (frame["rain_day_mm"] < DRY_DAY_MM) & (frame["rain_prev_day_mm"] < DRY_DAY_MM)


@dataclass
class Results:
    prediction: dict = field(default_factory=dict)
    detection: dict = field(default_factory=dict)
    disagreement: dict = field(default_factory=dict)
    data: dict = field(default_factory=dict)


def evaluate(model, df: pd.DataFrame, n_inject: int = 20_000) -> tuple[Results, pd.DataFrame]:
    res = Results()
    te = df[df["is_test"]].copy()
    te["p_spill"] = model.predict_proba(te[FEATURES])[:, 1]
    y = te["spill_started"].astype(int)

    res.data = {
        "train_site_days": int((~df["is_test"]).sum()),
        "train_spill_starts": int(df.loc[~df["is_test"], "spill_started"].sum()),
        "test_site_days": int(len(te)),
        "test_spill_starts": int(y.sum()),
        "sites": int(df[["company", "site_id"]].drop_duplicates().shape[0]),
        "test_period_start": TEST_START,
        "companies": sorted(df["company"].unique().tolist()),
    }

    # --- 1. How well does the model understand normal (rain-driven) spilling? ----
    rain_only = te["rain_day_mm"] + te["rain_prev_day_mm"]
    res.prediction = {
        "model": _scores(y, te["p_spill"]),
        "baseline_rain_2day_auc": float(roc_auc_score(y, rain_only)),
        "baseline_not_dry_rule_auc": float(roc_auc_score(y, ~rule_flags(te))),
    }
    for company, g in te.groupby("company"):
        if g["spill_started"].nunique() == 2:
            res.prediction[f"auc_{company}"] = float(roc_auc_score(g["spill_started"], g["p_spill"]))
    seen = te["site_wet_spill_rate"].notna() & te["site_wet_median_rain_3d"].notna()
    res.prediction["auc_sites_seen_in_training"] = float(roc_auc_score(y[seen], te.loc[seen, "p_spill"]))
    if (~seen).sum() and y[~seen].nunique() == 2:
        res.prediction["auc_sites_unseen_in_training"] = float(roc_auc_score(y[~seen], te.loc[~seen, "p_spill"]))

    # --- 2. Detection: recover injected weather-independent spills ----------------
    # Real spills are mostly legitimate (rain-driven). Injected spills are placed on
    # random site-days regardless of weather, mimicking spills with no rain cause.
    rng = np.random.default_rng(SEED)
    real = te[te["spill_started"]].assign(injected=False)
    candidates = te[~te["spill_started"]]
    injected = candidates.iloc[rng.choice(len(candidates), size=min(n_inject, len(candidates)), replace=False)]
    pool = pd.concat([real, injected.assign(injected=True)], ignore_index=True)
    pool["model_score"] = 1 - pool["p_spill"]                         # unexpectedness
    pool["rain_score"] = -(pool["rain_day_mm"] + pool["rain_prev_day_mm"])  # less rain = more suspicious
    pool["rule_flag"] = rule_flags(pool)

    rule_rate = float(pool["rule_flag"].mean())
    detection = {"injected": int(pool["injected"].sum()), "real_spills": int(len(real)),
                 "rule_flag_rate": rule_rate,
                 "rule": _flag_stats(pool, pool["rule_flag"])}
    for budget in sorted({0.01, 0.02, 0.05, 0.10, round(rule_rate, 4)}):
        for name in ("model_score", "rain_score"):
            flags = _top_fraction(pool[name], budget, rng)
            detection[f"{name}@{budget:.2%}"] = _flag_stats(pool, flags)
    # Random injection lands mostly on dry days, where every "less rain = suspicious"
    # method succeeds, so the overall recall cannot separate methods. Break recall
    # down by the weather on the injected day relative to what the site needs.
    two_day = pool["rain_day_mm"] + pool["rain_prev_day_mm"]
    need = pool["site_wet_median_rain_3d"]
    band = np.select(
        [pool["rule_flag"],
         two_day < 0.25 * need.fillna(np.inf),
         two_day < need.fillna(np.inf)],
        ["dry (<0.25 mm both days)", "light (<25% of site's usual spill rain)",
         "moderate (25-100% of usual)"],
        default="wet (>= site's usual spill rain)",
    )
    pool["band"] = band
    flags_at_rule_rate = {
        "rule": pool["rule_flag"],
        "model": _top_fraction(pool["model_score"], rule_rate, rng),
        "rain": _top_fraction(pool["rain_score"], rule_rate, rng),
    }
    by_band = {}
    for b, idx in pool.groupby("band").groups.items():
        inj = pool.loc[idx, "injected"]
        by_band[b] = {"injected": int(inj.sum()), "real_spills": int((~inj).sum()),
                      **{f"{m}_recall": float((f.loc[idx] & inj).sum() / max(inj.sum(), 1))
                         for m, f in flags_at_rule_rate.items()},
                      **{f"{m}_real_flagged": int((f.loc[idx] & ~inj).sum())
                         for m, f in flags_at_rule_rate.items()}}
    detection["by_weather_band_at_rule_rate"] = by_band
    res.detection = detection

    # --- 3. Where do the rule and the model disagree, and why? --------------------
    real_scored = pool[~pool["injected"]].copy()
    model_flag = _top_fraction(pool["model_score"], rule_rate, rng)[~pool["injected"]]
    real_scored["model_flag"] = model_flag.values
    rule_only = real_scored[real_scored["rule_flag"] & ~real_scored["model_flag"]]
    model_only = real_scored[~real_scored["rule_flag"] & real_scored["model_flag"]]
    res.disagreement = {
        "real_spills_flagged_by_rule": int(real_scored["rule_flag"].sum()),
        "real_spills_flagged_by_model": int(real_scored["model_flag"].sum()),
        "flagged_by_both": int((real_scored["rule_flag"] & real_scored["model_flag"]).sum()),
        "rule_only": _describe(rule_only),
        "model_only": _describe(model_only),
    }

    scored = te[te["spill_started"]].copy()
    scored["suspicion"] = 1 - scored["p_spill"]
    scored["rule_flag"] = rule_flags(scored)
    return res, scored


def _scores(y, p) -> dict:
    return {"roc_auc": float(roc_auc_score(y, p)), "pr_auc": float(average_precision_score(y, p)),
            "brier": float(brier_score_loss(y, p)), "log_loss": float(log_loss(y, p)),
            "base_rate": float(np.mean(y))}


def _top_fraction(score: pd.Series, fraction: float, rng) -> pd.Series:
    """Flag the top `fraction` by score; random tie-breaking keeps budgets exact."""
    jitter = rng.random(len(score)) * 1e-9
    k = int(round(fraction * len(score)))
    order = np.argsort(-(score.to_numpy() + jitter))
    flags = np.zeros(len(score), dtype=bool)
    flags[order[:k]] = True
    return pd.Series(flags, index=score.index)


def _flag_stats(pool: pd.DataFrame, flags: pd.Series) -> dict:
    inj = pool["injected"]
    tp = int((flags & inj).sum())
    return {"flag_rate": float(flags.mean()), "flagged": int(flags.sum()),
            "injected_recall": tp / int(inj.sum()),
            "precision_on_injected": tp / max(int(flags.sum()), 1),
            "real_spills_flagged": int((flags & ~inj).sum())}


def _describe(frame: pd.DataFrame) -> dict:
    if frame.empty:
        return {"n": 0}
    return {"n": int(len(frame)),
            **{f"median_{c}": float(frame[c].median()) for c in
               ["rain_day_mm", "rain_prev_day_mm", "rain_7d_mm", "rain_14d_mm", "p_spill",
                "site_wet_median_rain_3d", "spill_hours"]}}


def run(db_path: Path = RESEARCH_DB_PATH) -> Results:
    df = add_site_features(load_site_days(db_path))
    model = train(df)
    res, scored = evaluate(model, df)

    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    joblib.dump({"model": model, "features": FEATURES}, MODEL_DIR / "model.joblib")
    (MODEL_DIR / "metrics.json").write_text(json.dumps(res.__dict__, indent=2))
    scored.to_parquet(MODEL_DIR / "test_spills_scored.parquet", index=False)
    log.info("Saved model, metrics and scored test spills to %s", MODEL_DIR)
    return res

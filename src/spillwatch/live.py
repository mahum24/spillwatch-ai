"""Live scoring: apply both models to current data and publish JSON for the website.

For every live overflow this produces:
  - current status and recent spills,
  - a suspicion score for each recent spill (Phase 3 model), with a plain-English
    explanation and the baseline dry-day rule for comparison,
  - spill risk for today and tomorrow (Phase 4 models + a live rain forecast).

Rain-day convention as everywhere else: 09:00 to 09:00 GMT. "Issue day" I is the
rain day that started at the most recent 09:00 GMT; rain day I-1 is the last
fully observed day.
"""

import json
import logging
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import duckdb
import joblib
import numpy as np
import pandas as pd
import requests

from spillwatch.config import DATA_DIR, HTTP_TIMEOUT, RESEARCH_DB_PATH, WAREHOUSE_PATH
from spillwatch.ingest.forecasts import cell_of
from spillwatch.ml import dry_spill, forecast

log = logging.getLogger(__name__)

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
RECENT_DAYS = 7
SITE_PRIOR_K = 20
RISK_LEVELS = [(0.5, "high"), (0.2, "elevated"), (0.0, "low")]
# Exported with the models (`spillwatch export-models`) so the cloud needs no research DB
SITE_FEATURES_PATH = DATA_DIR / "models" / "site_features.parquet"
THRESHOLD_PATH = DATA_DIR / "models" / "dry_spill" / "threshold.json"


# --------------------------------------------------------------------------- inputs

def open_readonly(path: Path, wait_s: int = 120) -> duckdb.DuckDBPyConnection:
    """Read-only connection that waits while the collector holds the write lock."""
    deadline = time.monotonic() + wait_s
    while True:
        try:
            return duckdb.connect(str(path), read_only=True)
        except duckdb.IOException as exc:
            if "lock" not in str(exc).lower() or time.monotonic() > deadline:
                raise
            time.sleep(5)


def issue_day(now: datetime) -> date:
    return (now.astimezone(timezone.utc) - timedelta(hours=9)).date()


SITE_FEATURES_SQL = """
WITH base AS (SELECT * FROM r.site_day WHERE gauges > 0),
wet AS (SELECT * FROM base WHERE rain_3d_mm >= {wet} AND NOT already_spilling),
wet_prior AS (SELECT AVG(spill_started::INT) AS p FROM wet),
act_prior AS (SELECT AVG((spill_started OR already_spilling)::INT) AS p FROM base),
w AS (SELECT company, site_id, COUNT(*) AS n, SUM(spill_started::INT) AS pos,
             MEDIAN(rain_3d_mm) FILTER (WHERE spill_started) AS med
      FROM wet GROUP BY ALL),
a AS (SELECT company, site_id, COUNT(*) AS n, SUM((spill_started OR already_spilling)::INT) AS pos
      FROM base GROUP BY ALL)
SELECT h.company, h.site_id, h.latitude, h.longitude,
       (COALESCE(w.pos, 0) + {k} * wp.p) / (COALESCE(w.n, 0) + {k}) AS site_wet_spill_rate,
       w.med AS site_wet_median_rain_3d,
       (COALESCE(a.pos, 0) + {k} * ap.p) / (COALESCE(a.n, 0) + {k}) AS site_active_rate,
       a.n AS history_days
FROM r.hist_sites h
LEFT JOIN w USING (company, site_id)
LEFT JOIN a USING (company, site_id)
CROSS JOIN wet_prior wp CROSS JOIN act_prior ap
"""

LIVE_SQL = """
-- Live overflows linked to their history: same id, or (for id changes) the nearest
-- history site of the same company within 5 m.
CREATE OR REPLACE TEMP TABLE live_sites AS
WITH by_id AS (
    SELECT o.overflow_id, s.* EXCLUDE (company, site_id, latitude, longitude)
    FROM w.dim_overflows o JOIN site_feats s ON s.company = o.company AND s.site_id = o.site_id
),
by_location AS (
    SELECT o.overflow_id, s.* EXCLUDE (company, site_id, latitude, longitude)
    FROM w.dim_overflows o JOIN site_feats s
      ON s.company = o.company AND ABS(s.latitude - o.latitude) < 0.0001 AND ABS(s.longitude - o.longitude) < 0.00015
    WHERE o.overflow_id NOT IN (SELECT overflow_id FROM by_id)
    QUALIFY ROW_NUMBER() OVER (PARTITION BY o.overflow_id ORDER BY ABS(s.latitude - o.latitude) + ABS(s.longitude - o.longitude)) = 1
),
linked AS (SELECT * FROM by_id UNION ALL SELECT * FROM by_location)
SELECT o.overflow_id, o.company, o.site_id, o.receiving_water, o.latitude, o.longitude,
       o.current_status, o.current_status_since, o.last_seen_at,
       l.site_wet_spill_rate, l.site_wet_median_rain_3d, l.site_active_rate, l.history_days,
       l.overflow_id IS NOT NULL AS has_history
FROM w.dim_overflows o LEFT JOIN linked l USING (overflow_id);

-- Observed rain per gauge per rain day (complete days only: >= 88 of 96 readings)
CREATE OR REPLACE TEMP TABLE gauge_days AS
WITH m AS (
    SELECT measure_id, ANY_VALUE(station_id) AS station_id,
           CAST(hour_start - INTERVAL 9 HOUR AS DATE) AS rain_day,
           SUM(mm) AS mm, SUM(readings) AS n
    FROM w.rain_hourly
    WHERE hour_start >= DATE '{start}'
    GROUP BY measure_id, rain_day
)
SELECT station_id, rain_day, AVG(mm) AS rain_mm FROM m WHERE n >= 88 GROUP BY ALL;

CREATE OR REPLACE TEMP TABLE gauge_features AS
SELECT station_id, rain_day, rain_mm,
       SUM(rain_mm) OVER (PARTITION BY station_id ORDER BY rain_day RANGE BETWEEN INTERVAL 1 DAY PRECEDING AND INTERVAL 1 DAY PRECEDING) AS rain_prev_day,
       SUM(rain_mm) OVER (PARTITION BY station_id ORDER BY rain_day RANGE BETWEEN INTERVAL 2 DAY PRECEDING AND CURRENT ROW) AS rain_3d,
       SUM(rain_mm) OVER (PARTITION BY station_id ORDER BY rain_day RANGE BETWEEN INTERVAL 6 DAY PRECEDING AND CURRENT ROW) AS rain_7d,
       SUM(rain_mm) OVER (PARTITION BY station_id ORDER BY rain_day RANGE BETWEEN INTERVAL 13 DAY PRECEDING AND CURRENT ROW) AS rain_14d,
       SUM(rain_mm) OVER (PARTITION BY station_id ORDER BY rain_day RANGE BETWEEN INTERVAL 29 DAY PRECEDING AND CURRENT ROW) AS rain_30d,
       MAX(rain_mm) OVER (PARTITION BY station_id ORDER BY rain_day RANGE BETWEEN INTERVAL 6 DAY PRECEDING AND CURRENT ROW) AS rain_max_day_7d,
       COUNT(rain_mm) OVER (PARTITION BY station_id ORDER BY rain_day RANGE BETWEEN INTERVAL 29 DAY PRECEDING AND CURRENT ROW) AS days_reported_30d
FROM gauge_days;

-- Rain features per overflow per rain day (average of its <= 3 gauges within 10 km)
CREATE OR REPLACE TEMP TABLE site_rain AS
SELECT b.overflow_id, g.rain_day,
       COUNT(*) AS gauges,
       AVG(g.rain_mm) AS rain_day_mm, AVG(g.rain_prev_day) AS rain_prev_day_mm,
       AVG(g.rain_3d) AS rain_3d_mm, AVG(g.rain_7d) AS rain_7d_mm, AVG(g.rain_14d) AS rain_14d_mm,
       AVG(g.rain_30d) AS rain_30d_mm, AVG(g.rain_max_day_7d) AS rain_max_day_7d_mm,
       MIN(g.days_reported_30d) AS min_days_reported_30d
FROM w.bridge_overflow_gauge b JOIN gauge_features g USING (station_id)
GROUP BY ALL;
"""


def load_inputs(now: datetime):
    con = duckdb.connect()
    for alias, path in (("w", WAREHOUSE_PATH), ("r", RESEARCH_DB_PATH)):
        if alias == "r" and SITE_FEATURES_PATH.exists():
            continue  # cloud: site features come from the model bundle
        ro = open_readonly(path)  # wait out the collector's lock, then attach
        ro.close()
        con.execute(f"ATTACH '{path}' AS {alias} (READ_ONLY)")
    con.execute("SET TimeZone = 'UTC'")
    site_feats = (pd.read_parquet(SITE_FEATURES_PATH) if SITE_FEATURES_PATH.exists()
                  else con.execute(SITE_FEATURES_SQL.format(wet=dry_spill.WET_3D_MM, k=SITE_PRIOR_K)).df())
    con.register("site_feats", site_feats)
    start = issue_day(now) - timedelta(days=45)
    con.execute(LIVE_SQL.format(start=start.isoformat()))
    sites = con.execute("SELECT * FROM live_sites").df()
    rain = con.execute("SELECT * FROM site_rain").df()
    events = con.execute(f"""
        SELECT e.overflow_id, e.event_start, e.event_end, e.is_ongoing, e.duration_hours
        FROM w.fct_spill_events e
        WHERE COALESCE(e.event_end, now()) >= TIMESTAMP '{start}'
    """).df()
    feeds = con.execute("""
        SELECT company, MAX(last_updated) AS last_updated FROM w.raw_overflow_snapshots
        WHERE snapshot_at = (SELECT MAX(snapshot_at) FROM w.raw_overflow_snapshots) GROUP BY company
    """).df()
    con.close()
    rain["rain_day"] = pd.to_datetime(rain["rain_day"])
    return sites, rain, events, feeds


def fetch_rain_forecast(cells: list[tuple[float, float]], session: requests.Session) -> pd.DataFrame:
    """Hourly precipitation forecast per cell, aggregated to rain days (09:00-09:00 GMT)."""
    frames = []
    for i in range(0, len(cells), 50):
        batch = cells[i:i + 50]
        resp = session.get(FORECAST_URL, params={
            "latitude": ",".join(str(c[0]) for c in batch),
            "longitude": ",".join(str(c[1]) for c in batch),
            "hourly": "precipitation", "past_days": 1, "forecast_days": 3, "timezone": "GMT",
        }, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        payload = resp.json()
        payload = payload if isinstance(payload, list) else [payload]
        for (lat, lon), loc in zip(batch, payload):
            hourly = loc["hourly"]
            ends = pd.to_datetime(hourly["time"]).tz_localize("UTC") + pd.Timedelta(hours=1)
            frames.append(pd.DataFrame({"cell_lat": lat, "cell_lon": lon, "valid_hour_end": ends,
                                        "mm": pd.to_numeric(pd.Series(hourly["precipitation"]))}))
        time.sleep(1)
    hourly = pd.concat(frames)
    # Hour-ending timestamps: the hour ending 09:00 on D+1 belongs to rain day D
    hourly["rain_day"] = (hourly["valid_hour_end"] - pd.Timedelta(hours=9, seconds=1)).dt.tz_localize(None).dt.normalize()
    daily = hourly.groupby(["cell_lat", "cell_lon", "rain_day"]).agg(mm=("mm", "sum"), hours=("mm", "count"))
    return daily[daily["hours"] == 24].reset_index()


# --------------------------------------------------------------------------- scoring

def _season(days: pd.Series) -> tuple[pd.Series, pd.Series]:
    month = pd.to_datetime(days).dt.month
    return np.sin(2 * np.pi * month / 12).astype("float32"), np.cos(2 * np.pi * month / 12).astype("float32")


def suspicion_threshold() -> float:
    """Model threshold that flags the same share of real spills as the dry-day rule did
    in the held-out evaluation, so the two are compared at an equal flagging rate."""
    if THRESHOLD_PATH.exists():
        return json.loads(THRESHOLD_PATH.read_text())["p_spill_threshold"]
    scored = pd.read_parquet(dry_spill.MODEL_DIR / "test_spills_scored.parquet")
    metrics = json.loads((dry_spill.MODEL_DIR / "metrics.json").read_text())
    share = metrics["disagreement"]["real_spills_flagged_by_rule"] / metrics["detection"]["real_spills"]
    return float(scored["p_spill"].quantile(share))


def score_recent_spills(sites, rain, events, last_full_day: date) -> pd.DataFrame:
    bundle = joblib.load(dry_spill.MODEL_DIR / "model.joblib")
    ev = events.copy()
    ev["start_rain_day"] = (pd.to_datetime(ev["event_start"], utc=True) - pd.Timedelta(hours=9)).dt.tz_localize(None).dt.normalize()
    ev = ev[ev["start_rain_day"] >= pd.Timestamp(last_full_day - timedelta(days=RECENT_DAYS - 1))]
    ev = ev.merge(sites, on="overflow_id").merge(
        rain, left_on=["overflow_id", "start_rain_day"], right_on=["overflow_id", "rain_day"], how="left")
    ev["rain_complete"] = (ev["start_rain_day"] <= pd.Timestamp(last_full_day)) & ev["rain_day_mm"].notna() \
        & ev["rain_prev_day_mm"].notna() & (ev["min_days_reported_30d"] >= 27)
    ev["month_sin"], ev["month_cos"] = _season(ev["start_rain_day"])
    ev["p_spill"] = np.nan
    ok = ev["rain_complete"]
    if ok.any():
        ev.loc[ok, "p_spill"] = bundle["model"].predict_proba(ev.loc[ok, bundle["features"]])[:, 1]
    threshold = suspicion_threshold()
    ev["model_flag"] = ok & (ev["p_spill"] < threshold)
    ev["rule_flag"] = ok & dry_spill.rule_flags(ev)
    ev["explanation"] = ev.apply(_explain, axis=1)
    return ev


def _explain(r) -> str:
    start = pd.Timestamp(r["event_start"]).tz_convert("Europe/London")
    dur = "still spilling" if r["is_ongoing"] else f"spilled for {r['duration_hours']:.1f} h"
    text = f"Started {start:%a %d %b %H:%M}, {dur}."
    if not r["rain_complete"]:
        return text + " Rainfall for this day is not complete yet, so it has not been scored."
    text += (f" Nearby rain gauges recorded {r['rain_day_mm']:.1f} mm that day and "
             f"{r['rain_prev_day_mm']:.1f} mm the day before ({r['rain_7d_mm']:.1f} mm over the past week).")
    if pd.notna(r["site_wet_median_rain_3d"]):
        text += f" This overflow usually spills after about {r['site_wet_median_rain_3d']:.0f} mm of rain over 3 days."
    chance = "less than 0.1%" if r["p_spill"] < 0.001 else f"{r['p_spill']:.1%}"
    text += f" The model put the chance of a spill in this weather at {chance}."
    if r["model_flag"] and r["rule_flag"]:
        text += " Both the model and the dry-day rule flag it as unexpected."
    elif r["model_flag"]:
        text += " The model flags it as unexpected; the simple dry-day rule does not, because a little rain fell."
    elif r["rule_flag"]:
        text += (" It counts as a dry-day spill under the simple rule, but the model considers it plausible,"
                 " for example because of heavy rain earlier in the week.")
    return text


def forecast_risk(sites, rain, events, today: date, rain_fc: pd.DataFrame) -> pd.DataFrame:
    """P(spilling) for rain day `today` (h1) and the next one (h2)."""
    last = pd.Timestamp(today - timedelta(days=1))
    out = sites[["overflow_id", "latitude", "longitude", "site_wet_spill_rate", "site_wet_median_rain_3d",
                 "site_active_rate"]].copy()
    obs = rain[rain["rain_day"] == last].set_index("overflow_id")
    out = out.join(obs[["rain_day_mm", "rain_7d_mm", "rain_14d_mm", "rain_30d_mm", "rain_max_day_7d_mm",
                        "min_days_reported_30d"]], on="overflow_id")
    out = out.rename(columns={"rain_day_mm": "last_obs_rain_mm", "rain_7d_mm": "obs_rain_7d_mm",
                              "rain_14d_mm": "obs_rain_14d_mm", "rain_30d_mm": "obs_rain_30d_mm",
                              "rain_max_day_7d_mm": "obs_rain_max_day_7d_mm"})
    # Was each overflow spilling at any point during the last observed rain day?
    day_start = (last + pd.Timedelta(hours=9)).tz_localize("UTC")
    day_end = day_start + pd.Timedelta(days=1)
    ev = events.assign(event_end=pd.to_datetime(events["event_end"], utc=True).fillna(pd.Timestamp.now(tz="UTC")))
    active = ev[(pd.to_datetime(ev["event_start"], utc=True) < day_end) & (ev["event_end"] > day_start)]
    out["active_last_obs"] = out["overflow_id"].isin(active["overflow_id"]).astype("float32")
    # Unknown sites fall back to the overall rate, as in training
    out["site_active_rate"] = out["site_active_rate"].fillna(sites["site_active_rate"].median())

    cells = out.apply(lambda r: cell_of(r["latitude"], r["longitude"]), axis=1)
    out["cell_lat"], out["cell_lon"] = [c[0] for c in cells], [c[1] for c in cells]
    fc = rain_fc.set_index(["cell_lat", "cell_lon", "rain_day"])["mm"]

    def fc_for(day):
        idx = pd.MultiIndex.from_arrays([out["cell_lat"], out["cell_lon"], [pd.Timestamp(day)] * len(out)])
        return fc.reindex(idx).to_numpy()

    usable = out["min_days_reported_30d"].fillna(0) >= 27
    for h in (1, 2):
        target = today + timedelta(days=h - 1)
        bundle = joblib.load(forecast.MODEL_DIR / f"model_h{h}.joblib")
        # Observed data ends at rain day today-1 for both horizons; for horizon 2 the
        # gap day (today) comes from the forecast, exactly as in the evaluation.
        x = out.copy()
        x["rain_T_mm"] = fc_for(target)
        if h == 2:
            x["rain_Tm1_mm"] = fc_for(today)
        x["month_sin"], x["month_cos"] = _season(pd.Series([pd.Timestamp(target)] * len(x)))
        ok = usable & x[forecast.FUTURE_RAIN[h]].notna().all(axis=1)
        p = np.full(len(x), np.nan)
        if ok.any():
            p[ok.to_numpy()] = bundle["model"].predict_proba(x.loc[ok, bundle["features"]])[:, 1]
        out[f"risk_h{h}"] = p
        out[f"fc_rain_h{h}_mm"] = x["rain_T_mm"]
    return out


def risk_level(p: float) -> str | None:
    if p is None or np.isnan(p):
        return None
    return next(label for cut, label in RISK_LEVELS if p >= cut)


# --------------------------------------------------------------------------- publish

def publish(out_dir: Path, now: datetime | None = None) -> dict:
    from spillwatch.http import make_session

    now = now or datetime.now(timezone.utc)
    today = issue_day(now)
    last_full_day = today - timedelta(days=1)
    sites, rain, events, feeds = load_inputs(now)
    log.info("%d live overflows, %d with history; %d events", len(sites), int(sites["has_history"].sum()), len(events))

    spills = score_recent_spills(sites, rain, events, last_full_day)
    cells = sorted({cell_of(a, b) for a, b in zip(sites["latitude"], sites["longitude"]) if pd.notna(a)})
    rain_fc = fetch_rain_forecast(cells, make_session())
    risk = forecast_risk(sites, rain, events, today, rain_fc)

    recent = spills.groupby("overflow_id").agg(
        spills_7d=("event_start", "size"), hours_7d=("duration_hours", "sum"),
        flagged_7d=("model_flag", "sum"), rule_flagged_7d=("rule_flag", "sum"))
    last_event = events.sort_values("event_start").groupby("overflow_id").tail(1).set_index("overflow_id")
    merged = sites.set_index("overflow_id").join(recent).join(
        risk.set_index("overflow_id")[["risk_h1", "risk_h2", "fc_rain_h1_mm", "fc_rain_h2_mm"]]).join(
        last_event[["event_start", "event_end"]])

    def iso(ts):
        return None if pd.isna(ts) else pd.Timestamp(ts).tz_convert("UTC").strftime("%Y-%m-%dT%H:%MZ")

    def rnd(v, n=3):
        return None if pd.isna(v) else round(float(v), n)

    overflows = [{
        "id": oid, "company": r["company"], "site": r["site_id"], "water": r["receiving_water"],
        "lat": rnd(r["latitude"], 5), "lon": rnd(r["longitude"], 5),
        "status": int(r["current_status"]) if pd.notna(r["current_status"]) else None,
        "since": iso(r["current_status_since"]),
        "last_start": iso(r["event_start"]), "last_end": iso(r["event_end"]),
        "spills_7d": int(r["spills_7d"]) if pd.notna(r["spills_7d"]) else 0,
        "hours_7d": rnd(r["hours_7d"], 1) or 0,
        "flagged_7d": int(r["flagged_7d"]) if pd.notna(r["flagged_7d"]) else 0,
        "risk_today": rnd(r["risk_h1"]), "risk_tomorrow": rnd(r["risk_h2"]),
        "level_today": risk_level(r["risk_h1"]), "level_tomorrow": risk_level(r["risk_h2"]),
        "rain_fc_today_mm": rnd(r["fc_rain_h1_mm"], 1), "rain_fc_tomorrow_mm": rnd(r["fc_rain_h2_mm"], 1),
        "history": bool(r["has_history"]),
    } for oid, r in merged.iterrows() if pd.notna(r["latitude"])]

    flagged = spills[spills["model_flag"] | spills["rule_flag"]].sort_values("p_spill")
    suspicious = [{
        "id": r["overflow_id"], "company": r["company"], "water": r["receiving_water"],
        "lat": rnd(r["latitude"], 5), "lon": rnd(r["longitude"], 5),
        "start": iso(r["event_start"]), "hours": rnd(r["duration_hours"], 1), "ongoing": bool(r["is_ongoing"]),
        "p_spill": rnd(r["p_spill"], 4), "model_flag": bool(r["model_flag"]), "rule_flag": bool(r["rule_flag"]),
        "rain_day_mm": rnd(r["rain_day_mm"], 1), "rain_prev_day_mm": rnd(r["rain_prev_day_mm"], 1),
        "rain_7d_mm": rnd(r["rain_7d_mm"], 1), "history": bool(r["has_history"]),
        "explanation": r["explanation"],
    } for _, r in flagged.iterrows()]

    p3 = json.loads((dry_spill.MODEL_DIR / "metrics.json").read_text())
    p4 = json.loads((forecast.MODEL_DIR / "metrics.json").read_text())
    by_company = merged.groupby("company").agg(
        overflows=("site_id", "size"),
        spilling_now=("current_status", lambda s: int((s == 1).sum())),
        offline=("current_status", lambda s: int((s == -1).sum())),
        spills_7d=("spills_7d", "sum"), hours_7d=("hours_7d", "sum"), flagged_7d=("flagged_7d", "sum"),
        with_history=("has_history", "sum"),
    ).reset_index()
    summary = {
        "generated_at": iso(pd.Timestamp(now)),
        "issue_day": today.isoformat(), "last_full_rain_day": last_full_day.isoformat(),
        "overflows": len(overflows),
        "spilling_now": int((sites["current_status"] == 1).sum()),
        "offline_now": int((sites["current_status"] == -1).sum()),
        "spills_7d": int(len(spills)),
        "scored_7d": int(spills["rain_complete"].sum()),
        "model_flagged_7d": int(spills["model_flag"].sum()),
        "rule_flagged_7d": int(spills["rule_flag"].sum()),
        "high_risk_today": int(sum(o["level_today"] == "high" for o in overflows)),
        "high_risk_tomorrow": int(sum(o["level_tomorrow"] == "high" for o in overflows)),
        "by_company": json.loads(by_company.to_json(orient="records")),
        # When each company last updated its feed (the site warns if this is stale)
        "feeds": {r["company"]: iso(r["last_updated"]) for _, r in feeds.iterrows()},
        "models": {
            "suspicion_auc": round(p3["prediction"]["model"]["roc_auc"], 3),
            "suspicion_light_rain_recall": round(
                p3["detection"]["by_weather_band_at_rule_rate"]["light (<25% of site's usual spill rain)"]["model_recall"], 3),
            "forecast_auc_today": round(p4["h1"]["model + archived forecast (realistic)"]["roc_auc"], 3),
            "forecast_auc_tomorrow": round(p4["h2"]["model + archived forecast (realistic)"]["roc_auc"], 3),
        },
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    for name, payload in (("overflows", overflows), ("suspicious", suspicious), ("summary", summary)):
        (out_dir / f"{name}.json").write_text(json.dumps(payload, separators=(",", ":"), allow_nan=False))
    log.info("Published %d overflows, %d flagged spills to %s", len(overflows), len(suspicious), out_dir)
    return summary

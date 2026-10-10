import duckdb
import pandas as pd

from spillwatch.ml import forecast


def test_hourly_forecasts_map_to_0900_rain_days(tmp_path):
    raw = tmp_path / "raw"
    (raw / "forecast_archive").mkdir(parents=True)
    hours = pd.date_range("2025-07-01 10:00", "2025-07-03 09:00", freq="h", tz="UTC")  # 48 hour-endings
    fc = pd.DataFrame({"cell_lat": 54.5, "cell_lon": -1.25, "valid_hour_end": hours,
                       "fc_day1_mm": 1.0, "fc_day2_mm": 0.5})
    # 1 mm extra in the hour ending 09:00 on 2 July: it belongs to rain day 1 July
    fc.loc[fc["valid_hour_end"] == pd.Timestamp("2025-07-02 09:00", tz="UTC"), "fc_day1_mm"] = 2.0
    fc.to_parquet(raw / "forecast_archive" / "cell.parquet")

    db = tmp_path / "r.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE hist_sites AS SELECT 'c' company, 'S1' site_id, 54.57 latitude, -1.23 longitude")
    con.close()
    from spillwatch import config
    orig = config.RAW_DIR
    try:
        forecast.RAW_DIR = raw
        forecast.build_tables(db)
    finally:
        forecast.RAW_DIR = orig

    con = duckdb.connect(str(db), read_only=True)
    days = con.execute("SELECT rain_day::VARCHAR, fc1_mm, fc2_mm, hours FROM fc_rain_day ORDER BY 1").fetchall()
    cell = con.execute("SELECT cell_lat, cell_lon FROM site_fc_cell").fetchall()
    assert days == [("2025-07-01", 25.0, 12.0, 24), ("2025-07-02", 24.0, 12.0, 24)]
    assert cell == [(54.5, -1.25)]


def test_monotonic_constraints_follow_features():
    cols = forecast.features(2)
    cst = dict(zip(cols, forecast.monotonic(cols)))
    assert cst["rain_T_mm"] == 1 and cst["rain_Tm1_mm"] == 1 and cst["obs_rain_7d_mm"] == 1
    assert cst["active_last_obs"] == 1 and cst["site_wet_median_rain_3d"] == -1
    assert cst["month_sin"] == 0
    assert "rain_T_mm" not in forecast.features(1, with_future_rain=False)

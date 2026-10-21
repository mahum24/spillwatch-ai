"""Export the research warehouse as a Kaggle dataset (files + data card) in data/kaggle/.

    .venv/bin/python scripts/export_kaggle.py

Small tables are written as CSV, the two large ones as Parquet.
"""

import json
import shutil
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "kaggle"
DB = ROOT / "data" / "research.duckdb"

# file name -> (query, format, description)
FILES = {
    "spill_events.csv": (
        """select company, site_id, event_start, event_end, round(duration_hours, 3) as duration_hours,
                  start_rain_day, end_rain_day, raw_events, latitude, longitude, source
           from hist_events order by company, site_id, event_start""",
        "One row per spill. Overlapping or back-to-back records (gap under 30 minutes) are merged into one event.",
    ),
    "overflow_sites.csv": (
        "select company, site_id, latitude, longitude, events from hist_sites order by company, site_id",
        "Every overflow that has spill history, with its location and number of spill events.",
    ),
    "rain_gauges.csv": (
        "select measure_id, station_id, label, latitude, longitude from rain_gauges order by measure_id",
        "Environment Agency rain gauges used.",
    ),
    "site_gauge_links.csv": (
        """select company, site_id, measure_id, round(distance_km, 3) as distance_km, gauge_rank
           from bridge_site_gauge order by company, site_id, gauge_rank""",
        "Up to 3 nearest rain gauges within 10 km of each overflow.",
    ),
    "forecast_rain_daily.csv": (
        """select cell_lat, cell_lon, rain_day, round(fc1_mm, 2) as fc1_mm, round(fc2_mm, 2) as fc2_mm, hours
           from fc_rain_day order by cell_lat, cell_lon, rain_day""",
        "Archived Open-Meteo forecasts per 0.1 degree grid cell and rain day: fc1_mm issued 1 day ahead, fc2_mm 2 days ahead.",
    ),
    "site_forecast_cells.csv": (
        "select company, site_id, cell_lat, cell_lon from site_fc_cell order by company, site_id",
        "Forecast grid cell for each overflow (joins overflow_sites to forecast_rain_daily).",
    ),
    "rain_gauge_daily.parquet": (
        """select measure_id, rain_day, round(rain_mm, 2) as rain_mm, round(rain_prev_day, 2) as rain_prev_day_mm,
                  round(rain_3d, 2) as rain_3d_mm, round(rain_7d, 2) as rain_7d_mm, round(rain_14d, 2) as rain_14d_mm,
                  round(rain_30d, 2) as rain_30d_mm, round(rain_max_day_7d, 2) as rain_max_day_7d_mm,
                  days_reported_30d
           from rain_daily order by measure_id, rain_day""",
        "Daily rainfall per gauge with rolling totals (rain day = 09:00 to 09:00 GMT).",
    ),
    "overflow_rain_days.parquet": (
        """select company, site_id, rain_day, spill_started, round(spill_hours, 3) as spill_hours,
                  gauges, round(nearest_gauge_km, 3) as nearest_gauge_km,
                  round(rain_day_mm, 2) as rain_day_mm, round(rain_prev_day_mm, 2) as rain_prev_day_mm,
                  round(rain_3d_mm, 2) as rain_3d_mm, round(rain_7d_mm, 2) as rain_7d_mm,
                  round(rain_14d_mm, 2) as rain_14d_mm, round(rain_30d_mm, 2) as rain_30d_mm,
                  round(rain_max_day_7d_mm, 2) as rain_max_day_7d_mm, min_days_reported_30d, already_spilling
           from site_day order by company, site_id, rain_day""",
        "The modelling table: one row per overflow per rain day, with whether a spill started and the rainfall around it.",
    ),
}


def main():
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    con = duckdb.connect(str(DB), read_only=True)
    con.execute("set TimeZone = 'UTC'")
    for name, (query, _) in FILES.items():
        fmt = "(format parquet, compression zstd)" if name.endswith(".parquet") else "(header, timestampformat '%Y-%m-%dT%H:%M:%SZ')"
        con.execute(f"copy ({query}) to '{OUT / name}' {fmt}")
        rows = con.execute(f"select count(*) from '{OUT / name}'").fetchone()[0]
        print(f"{name:28} {rows:>10,} rows  {(OUT / name).stat().st_size / 1e6:7.1f} MB")
    (OUT / "dataset-metadata.json").write_text(json.dumps({
        "title": "UK Storm Overflow Spills and Rainfall 2020-2026",
        "id": "KAGGLE_USERNAME/uk-storm-overflow-spills-and-rainfall",
        "licenses": [{"name": "other"}],
        "resources": [{"path": n, "description": d} for n, (_, d) in FILES.items()],
    }, indent=2))


if __name__ == "__main__":
    main()

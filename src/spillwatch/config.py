"""Central configuration: data sources and local paths."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
WAREHOUSE_PATH = DATA_DIR / "spillwatch.duckdb"
SQL_DIR = PROJECT_ROOT / "sql"

# Near-real-time storm overflow feeds published by each water company
# (National Storm Overflow Hub / Stream). All are ArcGIS FeatureServers
# sharing a common schema, with minor differences in field-name casing.
OVERFLOW_FEEDS = {
    "united_utilities": "https://services5.arcgis.com/5eoLvR0f8HKb7HWP/arcgis/rest/services/United_Utilities_Storm_Overflow_Activity/FeatureServer/0",
    "severn_trent": "https://services1.arcgis.com/NO7lTIlnxRMMG9Gw/arcgis/rest/services/Severn_Trent_Water_Storm_Overflow_Activity/FeatureServer/0",
    "northumbrian": "https://services-eu1.arcgis.com/MSNNjkZ51iVh8yBj/arcgis/rest/services/Northumbrian_Water_Storm_Overflow_Activity_2_view/FeatureServer/0",
    "anglian": "https://services3.arcgis.com/VCOY1atHWVcDlvlJ/arcgis/rest/services/stream_service_outfall_locations_view/FeatureServer/0",
    "yorkshire": "https://services-eu1.arcgis.com/1WqkK5cDKUbF0CkH/arcgis/rest/services/Yorkshire_Water_Storm_Overflow_Activity/FeatureServer/0",
    "thames": "https://services2.arcgis.com/g6o32ZDQ33GpCIu3/arcgis/rest/services/Thames_Water_Storm_Overflow_Activity_(Production)_view/FeatureServer/0",
    "south_west": "https://services-eu1.arcgis.com/OMdMOtfhATJPcHe3/arcgis/rest/services/NEH_outlets_PROD/FeatureServer/0",
    "wessex": "https://services.arcgis.com/3SZ6e0uCvPROr4mS/arcgis/rest/services/Wessex_Water_Storm_Overflow_Activity/FeatureServer/0",
    "southern": "https://services-eu1.arcgis.com/6qJmARkS2dt2IjVA/arcgis/rest/services/SouthernWater_StormOverflowActivity_PROD_view/FeatureServer/0",
}

# Environment Agency real-time flood-monitoring API (rainfall gauges, 15-min totals in mm)
EA_BASE = "https://environment.data.gov.uk/flood-monitoring"
EA_ARCHIVE_URL = EA_BASE + "/archive/readings-full-{day}.csv"

# Status codes used by the overflow feeds
STATUS_LABELS = {-1: "offline", 0: "stopped", 1: "spilling"}

HTTP_TIMEOUT = 60
USER_AGENT = "SpillWatch-AI/0.1 (MSc research project; open data)"

# ---------------------------------------------------------------------------
# Research (Phase 3): historical spill events + daily rainfall for model training.
# Kept in a separate DuckDB file so long downloads never block the live collector.
RESEARCH_DB_PATH = DATA_DIR / "research.duckdb"

# EA Hydrology API: quality-checked daily rainfall totals per gauge, back to 2020+.
# A daily total dated D covers the "rain day" 09:00 on D to 09:00 on D+1 (GMT).
HYDRO_BASE = "https://environment.data.gov.uk/hydrology"
RAIN_HISTORY_START = "2020-01-01"

# Event-level spill history published by water companies (one row per spill).
# Field names and time formats differ per source, so each declares its own mapping.
_EDM = "https://services-eu1.arcgis.com/XxS6FebPX29TRGDJ/arcgis/rest/services"
_STD = {"site": "SiteId", "start": "EventStart", "end": "EventEnd",
        "lat": "OutfallLatitude", "lon": "OutfallLongitude", "time_format": None}
HISTORY_SOURCES = {
    "northumbrian_2020_2024": {
        "company": "northumbrian",
        "url": f"{_EDM}/EDM_Spill_Data_Northumbrian_Water_2020_2023/FeatureServer/0",
        "site": "Monitor_Unique_ID", "start": "Event_start", "end": "Event_end",
        "lat": "Outfall_Latitude", "lon": "Outfall_Longitude",
        # Assumed UK local time (not stated by the publisher)
        "time_format": "%d/%m/%Y %H:%M", "timezone": "Europe/London",
    },
    "united_utilities_2023": {"company": "united_utilities", **_STD,
        "url": f"{_EDM}/United_Utilities_Event_Duration_Monitoring_2023/FeatureServer/0"},
    "united_utilities_2024": {"company": "united_utilities", **_STD,
        "url": f"{_EDM}/United_Utilities_Event_Duration_Monitoring_2024/FeatureServer/0"},
    "united_utilities_2025": {"company": "united_utilities", **_STD,
        "url": f"{_EDM}/United_Utilities_Event_Duration_Monitoring_2025/FeatureServer/0"},
    "severn_trent_2025": {"company": "severn_trent", **_STD,
        "url": f"{_EDM}/STW_Edm_2025/FeatureServer/0"},
    "wessex_2025": {"company": "wessex", **_STD,
        "url": "https://services.arcgis.com/3SZ6e0uCvPROr4mS/arcgis/rest/services/Wessex_Water_Event_Duration_Monitoring_2025_view/FeatureServer/0"},
    "wessex_2026": {"company": "wessex", **_STD,
        "url": "https://services.arcgis.com/3SZ6e0uCvPROr4mS/arcgis/rest/services/Wessex_Water_Event_Duration_Monitoring_2026_view2/FeatureServer/0"},
    "yorkshire_2026": {"company": "yorkshire", **_STD,
        "url": "https://services-eu1.arcgis.com/1WqkK5cDKUbF0CkH/arcgis/rest/services/Event%20Duration%20Monitoring%20History/FeatureServer/0"},
}

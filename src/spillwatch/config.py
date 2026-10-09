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

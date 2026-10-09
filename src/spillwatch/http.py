"""Shared HTTP session with retries and backoff for flaky public APIs."""

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from spillwatch.config import USER_AGENT


def make_session() -> requests.Session:
    retry = Retry(
        total=5,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers["User-Agent"] = USER_AGENT
    # ArcGIS fronts feeds with a 5-minute CDN cache. A response cached while a
    # company was reloading its table (e.g. 1,000 of 1,342 sites) would otherwise be
    # served to us repeatedly, so always ask for a fresh answer.
    session.headers["Cache-Control"] = "no-cache"
    return session

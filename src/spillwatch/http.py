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
    return session

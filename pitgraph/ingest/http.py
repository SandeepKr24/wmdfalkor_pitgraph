"""Download JSON with a local file cache and polite retries.

Retries cover rate limits (429), server errors (5xx), timeouts and dropped connections.

Raw API responses are cached under data/raw/, so re-running ingestion
(e.g. after the free Cloud instance is deleted) doesn't hit the APIs again.
"""

import json
import time
from pathlib import Path

import requests

CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "raw"
DEFAULT_DELAY_S = 0.5  # stay well under Jolpica's 4 requests/second
MAX_RETRIES = 5
TIMEOUT_S = 30


def fetch_json(
    url: str,
    params: dict,
    cache_key: str,
    delay_s: float = DEFAULT_DELAY_S,
    not_found_value=None,
):
    """Return the JSON for url+params, from cache if we've fetched it before.

    delay_s is a pause before each real request, to respect the API's rate limit.
    not_found_value: if given, a 404 returns (and caches) this instead of raising.
    OpenF1 answers 404 when a query simply has no rows.
    """
    cache_file = CACHE_DIR / f"{cache_key}.json"
    if cache_file.exists():
        return json.loads(cache_file.read_text(encoding="utf-8"))

    for attempt in range(MAX_RETRIES):
        time.sleep(delay_s)
        backoff_s = 2 ** (attempt + 2)  # 4, 8, 16, 32, 64 seconds
        try:
            response = requests.get(url, params=params, timeout=TIMEOUT_S)
        except (requests.ConnectionError, requests.Timeout) as error:
            # Network blip: wait and try again.
            print(f"  {type(error).__name__} on {url}, retrying in {backoff_s}s")
            time.sleep(backoff_s)
            continue
        if response.status_code == 429 or response.status_code >= 500:
            # Rate limited or server trouble: wait as told, or back off.
            wait_s = float(response.headers.get("Retry-After", backoff_s))
            print(f"  HTTP {response.status_code} on {url}, retrying in {wait_s:.0f}s")
            time.sleep(wait_s)
            continue
        if response.status_code == 404 and not_found_value is not None:
            data = not_found_value
        else:
            response.raise_for_status()
            data = response.json()
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(data), encoding="utf-8")
        return data

    raise RuntimeError(f"Gave up after {MAX_RETRIES} attempts: {url} {params}")

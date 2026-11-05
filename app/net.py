"""Single HTTP entry point with retries. Everything goes through `request`,
which also makes the whole bot easy to test by patching this one function."""
from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger("net")

RETRY_STATUS = {429, 500, 502, 503, 504}


def request(method: str, url: str, *, retries: int = 3, timeout: int = 60, **kwargs):
    last_exc = None
    for attempt in range(retries):
        try:
            resp = requests.request(method, url, timeout=timeout, **kwargs)
        except requests.RequestException as exc:
            last_exc = exc
            if attempt == retries - 1:
                raise
            log.warning("%s %s failed (%s), retrying", method, url, exc)
            time.sleep(2 ** attempt * 2)
            continue
        if resp.status_code in RETRY_STATUS and attempt < retries - 1:
            log.warning("%s %s -> %s, retrying", method, url, resp.status_code)
            time.sleep(2 ** attempt * 2)
            continue
        return resp
    raise last_exc  # pragma: no cover

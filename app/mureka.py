"""Mureka API client: lyrics -> song (async task + polling) and mp3 download."""
from __future__ import annotations

import logging
import time
from pathlib import Path

from . import net

log = logging.getLogger("mureka")

POLL_SECONDS = 6
TIMEOUT_SECONDS = 900
FAILED = {"failed", "timeouted", "cancelled", "canceled"}


class MurekaError(RuntimeError):
    pass


def _headers(cfg) -> dict:
    return {"Authorization": f"Bearer {cfg.mureka_key}", "Content-Type": "application/json"}


def generate(cfg, lyrics: str, style: str) -> dict:
    """Create one song and wait until it is ready. Returns {"url", "duration_ms", "task_id"}."""
    body = {"lyrics": lyrics, "model": cfg.mureka_model, "prompt": style}
    if cfg.mureka_n:
        body["n"] = cfg.mureka_n  # fewer songs per request = cheaper
    if getattr(cfg, "mureka_vocal_id", ""):
        body["vocal_id"] = cfg.mureka_vocal_id  # sing with one fixed voice (own / licensed) in every song
    url = f"{cfg.mureka_base}/v1/song/generate"
    resp = net.request("POST", url, headers=_headers(cfg), json=body, timeout=90)
    if resp.status_code == 400 and "n" in body:
        log.warning("Mureka rejected 'n' (%s); retrying without it", resp.text[:200])
        body.pop("n")
        resp = net.request("POST", url, headers=_headers(cfg), json=body, timeout=90)
    if resp.status_code != 200:
        raise MurekaError(f"generate failed {resp.status_code}: {resp.text[:300]}")
    task = resp.json()
    task_id = task.get("id")
    if not task_id:
        raise MurekaError(f"no task id in response: {str(task)[:300]}")

    deadline = time.time() + TIMEOUT_SECONDS
    query_url = f"{cfg.mureka_base}/v1/song/query/{task_id}"
    while True:
        status = str(task.get("status", "")).lower()
        if status == "succeeded":
            break
        if status in FAILED:
            raise MurekaError(f"task {task_id} ended with status '{status}': {task.get('failed_reason', '')}")
        if time.time() > deadline:
            raise MurekaError(f"task {task_id} timed out after {TIMEOUT_SECONDS}s (status '{status}')")
        time.sleep(POLL_SECONDS)
        r = net.request("GET", query_url, headers=_headers(cfg), timeout=60)
        if r.status_code != 200:
            raise MurekaError(f"query failed {r.status_code}: {r.text[:300]}")
        task = r.json()

    for choice in task.get("choices") or []:
        if choice.get("url"):
            return {
                "url": choice["url"],
                "duration_ms": choice.get("duration") or 0,
                "task_id": task_id,
            }
    raise MurekaError(f"task {task_id} succeeded but has no audio url: {str(task)[:300]}")


def download(url: str, out_path: Path) -> Path:
    resp = net.request("GET", url, timeout=180)
    if resp.status_code != 200 or not resp.content:
        raise MurekaError(f"audio download failed {resp.status_code}")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(resp.content)
    return out_path

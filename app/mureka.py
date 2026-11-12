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


WAVESPEED = "https://api.wavespeed.ai/api/v3"


def _gender(style: str):
    low = (style or "").lower()
    if "duet" in low:
        return None
    if "female" in low:
        return "female"
    if "male" in low:
        return "male"
    return None


def _generate_wavespeed(cfg, lyrics: str, style: str) -> dict:
    """Same Mureka V9.5 model, sold by WaveSpeedAI (one song per request, async prediction + polling)."""
    hdr = {"Authorization": f"Bearer {cfg.wavespeed_key}", "Content-Type": "application/json"}
    body = {"lyrics": lyrics[:5000], "prompt": style[:1000], "output_format": "mp3"}
    gender = _gender(style)
    if gender:
        body["gender"] = gender
    if getattr(cfg, "mureka_vocal_id", ""):
        body["vocal_id"] = cfg.mureka_vocal_id
    resp = net.request("POST", f"{WAVESPEED}/mureka-ai/mureka-v9.5/generate-song", headers=hdr, json=body, timeout=90)
    if resp.status_code != 200:
        raise MurekaError(f"generate failed {resp.status_code}: {resp.text[:300]}")
    data = (resp.json() or {}).get("data") or {}
    task_id = data.get("id")
    if not task_id:
        raise MurekaError(f"no task id in response: {resp.text[:300]}")
    deadline = time.time() + TIMEOUT_SECONDS
    time.sleep(8)
    while True:
        r = net.request("GET", f"{WAVESPEED}/predictions/{task_id}/result", headers=hdr, timeout=60)
        if r.status_code != 200:
            raise MurekaError(f"query failed {r.status_code}: {r.text[:300]}")
        d = (r.json() or {}).get("data") or {}
        status = str(d.get("status", "")).lower()
        if status == "completed":
            outs = d.get("outputs") or []
            first = outs[0] if outs else None
            url = first.get("url") if isinstance(first, dict) else first
            if not url:
                raise MurekaError(f"task {task_id} completed but has no audio url: {str(d)[:300]}")
            return {"url": url, "duration_ms": 0, "task_id": task_id}
        if status in ("failed", "cancelled", "canceled", "timeout", "deleted"):
            raise MurekaError(f"task {task_id} ended with status '{status}': {d.get('error', '')}")
        if time.time() > deadline:
            raise MurekaError(f"task {task_id} timed out after {TIMEOUT_SECONDS}s (status '{status}')")
        time.sleep(POLL_SECONDS)


def generate(cfg, lyrics: str, style: str) -> dict:
    """Create one song and wait until it is ready. Returns {"url", "duration_ms", "task_id"}."""
    if getattr(cfg, "song_provider", "mureka") == "wavespeed":
        return _generate_wavespeed(cfg, lyrics, style)
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

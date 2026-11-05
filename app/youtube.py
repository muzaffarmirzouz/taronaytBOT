"""YouTube Data API v3 upload (resumable) using a stored OAuth refresh token."""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

from . import net

log = logging.getLogger("youtube")

TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
THUMB_URL = "https://www.googleapis.com/upload/youtube/v3/thumbnails/set"

_token = {"value": "", "expires": 0.0}


class YouTubeError(RuntimeError):
    pass


def access_token(cfg) -> str:
    if _token["value"] and time.time() < _token["expires"] - 60:
        return _token["value"]
    resp = net.request("POST", TOKEN_URL, data={
        "client_id": cfg.yt_client_id,
        "client_secret": cfg.yt_client_secret,
        "refresh_token": cfg.yt_refresh_token,
        "grant_type": "refresh_token",
    }, timeout=30)
    if resp.status_code != 200:
        raise YouTubeError(
            f"token refresh failed {resp.status_code}: {resp.text[:300]} "
            "(if it says invalid_grant: the refresh token expired or was revoked; run auth_youtube.py again)"
        )
    data = resp.json()
    _token["value"] = data["access_token"]
    _token["expires"] = time.time() + int(data.get("expires_in", 3600))
    return _token["value"]


def clean_text(text: str, limit: int) -> str:
    return re.sub(r"[<>]", "", text or "").strip()[:limit]


def clean_tags(tags: list, budget: int = 450) -> list:
    out, used = [], 0
    for tag in tags:
        tag = re.sub(r"[<>,\"]", "", str(tag)).strip()[:30]
        if not tag or tag.lower() in (t.lower() for t in out):
            continue
        cost = len(tag) + (2 if " " in tag else 0) + 1  # YouTube counts quotes around multi-word tags + comma
        if used + cost > budget:
            break
        out.append(tag)
        used += cost
    return out


def upload(cfg, video: Path, title: str, description: str, tags: list, thumbnail: Path | None = None) -> str:
    """Upload a video and return its YouTube video id."""
    token = access_token(cfg)
    video = Path(video)
    size = video.stat().st_size
    metadata = {
        "snippet": {
            "title": clean_text(title, 100) or "New song",
            "description": clean_text(description, 4900),
            "tags": clean_tags(tags),
            "categoryId": cfg.yt_category,
            "defaultLanguage": "en",
            "defaultAudioLanguage": "en",
        },
        "status": {
            "privacyStatus": cfg.yt_privacy,
            "selfDeclaredMadeForKids": False,
            "embeddable": True,
            "publicStatsViewable": True,
        },
    }
    if cfg.yt_synthetic:
        metadata["status"]["containsSyntheticMedia"] = True

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=UTF-8",
        "X-Upload-Content-Length": str(size),
        "X-Upload-Content-Type": "video/mp4",
    }
    init = net.request("POST", UPLOAD_URL, params={"uploadType": "resumable", "part": "snippet,status"},
                       headers=headers, data=json.dumps(metadata), timeout=60)
    if init.status_code != 200:
        raise YouTubeError(f"upload init failed {init.status_code}: {init.text[:400]}")
    location = init.headers.get("Location")
    if not location:
        raise YouTubeError("upload init returned no Location header")

    with open(video, "rb") as fh:
        # No automatic retry: the body is a stream and a half-sent upload must not be repeated blindly.
        put = net.request("PUT", location, data=fh, retries=1, timeout=1800, headers={
            "Content-Length": str(size), "Content-Type": "video/mp4",
        })
    if put.status_code not in (200, 201):
        raise YouTubeError(f"upload failed {put.status_code}: {put.text[:400]}")
    video_id = put.json().get("id")
    if not video_id:
        raise YouTubeError(f"upload response has no video id: {put.text[:300]}")

    if thumbnail and Path(thumbnail).exists():
        try:
            r = net.request("POST", THUMB_URL, params={"videoId": video_id},
                            headers={"Authorization": f"Bearer {token}", "Content-Type": "image/jpeg"},
                            data=Path(thumbnail).read_bytes(), timeout=60)
            if r.status_code != 200:
                log.warning("thumbnail not set (%s): %s", r.status_code, r.text[:200])
        except Exception as exc:  # thumbnails need a verified channel; never fail the upload for it
            log.warning("thumbnail error: %s", exc)
    return video_id

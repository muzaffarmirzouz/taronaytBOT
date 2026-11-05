"""OpenAI image generation for the video background."""
from __future__ import annotations

import base64
from pathlib import Path

from . import net

API_URL = "https://api.openai.com/v1/images/generations"

STYLE_SUFFIX = (
    " No text, no letters, no words, no logos, no watermark, no real people. "
    "Wide landscape composition with calm space in the middle."
)


def generate(cfg, prompt: str, out_path: Path) -> Path:
    body = {
        "model": cfg.image_model,
        "prompt": (prompt + STYLE_SUFFIX)[:3800],
        "size": cfg.image_size,
        "n": 1,
    }
    if cfg.image_model.startswith("gpt-image") and cfg.image_quality:
        body["quality"] = cfg.image_quality
    headers = {"Authorization": f"Bearer {cfg.openai_key}", "Content-Type": "application/json"}
    resp = net.request("POST", API_URL, headers=headers, json=body, timeout=180)
    if resp.status_code != 200:
        raise RuntimeError(f"OpenAI images {resp.status_code}: {resp.text[:300]}")
    item = (resp.json().get("data") or [{}])[0]
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if item.get("b64_json"):
        out_path.write_bytes(base64.b64decode(item["b64_json"]))
    elif item.get("url"):
        img = net.request("GET", item["url"], timeout=120)
        if img.status_code != 200:
            raise RuntimeError(f"image download failed {img.status_code}")
        out_path.write_bytes(img.content)
    else:
        raise RuntimeError("OpenAI returned no image data")
    return out_path

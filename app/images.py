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


_working = {"model": ""}
FALLBACK_MODELS = ["gpt-image-2", "gpt-image-1.5", "gpt-image-1-mini", "gpt-image-1"]


def _post(cfg, model: str, prompt: str, with_quality: bool):
    body = {"model": model, "prompt": (prompt + STYLE_SUFFIX)[:3800], "size": cfg.image_size, "n": 1}
    if with_quality and model.startswith("gpt-image") and cfg.image_quality:
        body["quality"] = cfg.image_quality
    headers = {"Authorization": f"Bearer {cfg.openai_key}", "Content-Type": "application/json"}
    return net.request("POST", API_URL, headers=headers, json=body, timeout=180)


def generate(cfg, prompt: str, out_path: Path) -> Path:
    # Model names get retired; try the configured one, then known alternatives.
    candidates = []
    for m in [_working["model"], cfg.image_model] + FALLBACK_MODELS:
        if m and m not in candidates:
            candidates.append(m)
    resp, errors = None, []
    for model in candidates:
        for with_quality in (True, False):
            resp = _post(cfg, model, prompt, with_quality)
            if resp.status_code == 200:
                _working["model"] = model
                break
            errors.append(f"{model}: {resp.status_code} {resp.text[:160]}")
            if "quality" not in resp.text.lower():
                break  # retry without quality only when quality was the complaint
        if resp.status_code == 200:
            break
        if resp.status_code in (401, 429) or "billing" in resp.text.lower():
            break  # key/limit problem: other models will not help
    if resp.status_code != 200:
        raise RuntimeError("OpenAI images failed: " + " | ".join(errors[-3:]))
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

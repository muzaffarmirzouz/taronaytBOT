"""ffmpeg helpers: still image + mp3 -> 1080p mp4 with a slow pan, thumbnail, fallback art."""
from __future__ import annotations

import logging
import os
import random
import subprocess
from pathlib import Path

log = logging.getLogger("video")


def _run(cmd: list, timeout: int = 1800) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {proc.stderr[-600:]}")


def probe_duration(path: Path) -> float:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, timeout=60,
    )
    try:
        return float(proc.stdout.strip())
    except ValueError:
        raise RuntimeError(f"cannot read duration of {path}: {proc.stderr[-300:]}")


def fallback_image(out_path: Path, seed: int = 0) -> Path:
    """A soft gradient, used only if image generation fails, so the pipeline never stalls."""
    rnd = random.Random(seed)
    def color() -> str:
        return "0x%02x%02x%02x" % (rnd.randint(30, 220), rnd.randint(30, 220), rnd.randint(30, 220))
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    _run(["ffmpeg", "-y", "-f", "lavfi",
          "-i", f"gradients=s=1536x1024:c0={color()}:c1={color()}:x0=0:y0=0:x1=1536:y1=1024:d=1",
          "-frames:v", "1", str(out_path)], timeout=120)
    return out_path


def make_video(image: Path, audio: Path, out_path: Path, visualizer: bool | None = None) -> Path:
    """1920x1080 mp4: the picture drifts slowly left to right, edges are darkened, and
    (optionally) music-reactive bars dance along the bottom."""
    if visualizer is None:
        visualizer = os.environ.get("VIDEO_VISUALIZER", "0") == "1"  # opt-in via Railway variable
    duration = probe_duration(audio)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    bg = (
        "[0:v]scale=2112:1188:force_original_aspect_ratio=increase,crop=2112:1188,"
        f"crop=1920:1080:x='(in_w-out_w)*t/{duration:.2f}':y='(in_h-out_h)/2',"
        "vignette=PI/5,format=yuv420p"
    )
    if visualizer:
        graph = (
            bg + "[bg];"
            "[1:a]showfreqs=s=1920x230:mode=bar:fscale=log:ascale=sqrt:win_size=2048:rate=24"
            ":colors=white|0xffd9a0,format=rgba,colorkey=0x000000:0.12:0.08,"
            "colorchannelmixer=aa=0.8[viz];"
            "[bg][viz]overlay=0:H-h-30:format=auto,format=yuv420p[v]"
        )
    else:
        graph = bg + "[v]"
    _run([
        "ffmpeg", "-y", "-loop", "1", "-framerate", "24", "-i", str(image), "-i", str(audio),
        "-filter_complex", graph, "-map", "[v]", "-map", "1:a",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
        "-c:a", "aac", "-b:a", "192k",
        "-t", f"{duration:.2f}", "-movflags", "+faststart", str(out_path),
    ])
    return out_path


def make_thumbnail(image: Path, out_path: Path) -> Path:
    out_path = Path(out_path)
    _run(["ffmpeg", "-y", "-i", str(image),
          "-vf", "scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720",
          "-frames:v", "1", "-q:v", "3", str(out_path)], timeout=120)
    return out_path

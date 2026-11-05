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


STYLES = ("pulse", "bars", "wave")
FPS = 24


def pick_style(exclude: str | None = None) -> str:
    """Random look for each video. VIDEO_STYLE=pulse|bars|wave|plain pins one look; default is random."""
    forced = os.environ.get("VIDEO_STYLE", "random").strip().lower()
    if forced in STYLES or forced == "plain":
        return forced
    options = [x for x in STYLES if x != exclude] or list(STYLES)
    return random.choice(options)


def beat_times(audio: Path) -> list:
    """Times (seconds) of strong bass hits, found with ffmpeg only. Empty list if anything goes wrong."""
    import re
    import tempfile
    try:
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "env.txt"
            _run(["ffmpeg", "-y", "-i", str(audio), "-af",
                  "lowpass=f=160,asetnsamples=n=1838:p=0,astats=metadata=1:reset=1,"
                  f"ametadata=print:key=lavfi.astats.Overall.RMS_level:file={env_file}",
                  "-f", "null", "-"], timeout=300)
            vals = []
            for line in env_file.read_text().splitlines():
                m = re.search(r"RMS_level=(-?[\d.]+|-inf)", line)
                if m:
                    vals.append(-90.0 if m.group(1) == "-inf" else float(m.group(1)))
        if len(vals) < 10:
            return []
        lo, hi = min(vals), max(vals)
        env = [(v - lo) / (hi - lo + 1e-9) for v in vals]
        beats, last = [], -100
        for i in range(1, len(env)):
            if env[i] - env[i - 1] > 0.18 and env[i] > 0.45 and (i - last) / FPS > 0.25:
                beats.append(i / FPS)
                last = i
        return beats
    except Exception as exc:
        log.warning("beat detection failed (%s); using slow zoom only", exc)
        return []


def _zoom_expr(beats: list, frames: int) -> str:
    terms = "+".join(f"between(ld(0),{b:.2f},{b + 0.6:.2f})*exp(-9*(ld(0)-{b:.2f}))" for b in beats)
    pulse = f"+0.05*({terms})" if terms else ""
    return f"st(0,in/{FPS});1.0+0.05*ld(0)/{max(frames / FPS, 1):.1f}{pulse}"


def make_video(image: Path, audio: Path, out_path: Path, style: str | None = None) -> Path:
    """1920x1080 mp4. The picture "breathes" with the bass; style adds an optional music visual:
    pulse = nothing else, bars = mirrored glowing bars, wave = thin glowing waveform, plain = old slow pan."""
    style = style or pick_style()
    duration = probe_duration(audio)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(duration * FPS) + 1
    if style == "plain":
        graph = (
            "[0:v]scale=2112:1188:force_original_aspect_ratio=increase,crop=2112:1188,"
            f"crop=1920:1080:x='(in_w-out_w)*t/{duration:.2f}':y='(in_h-out_h)/2',format=yuv420p[v]"
        )
    else:
        zoom = _zoom_expr(beat_times(audio), frames)
        bg = (
            "[0:v]scale=2112:1188:force_original_aspect_ratio=increase,crop=2112:1188,setpts=N/24/TB,"
            f"zoompan=z='{zoom}':d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=1920x1080:fps={FPS},"
            "vignette=PI/5,format=yuv420p"
        )
        if style == "bars":
            graph = (
                bg + "[bg];"
                "[1:a]showfreqs=s=960x220:mode=bar:fscale=log:ascale=sqrt:win_size=2048:rate=24"
                ":colors=0xfff2d6|0xffc58a,format=rgba,colorkey=0x000000:0.12:0.1,split[l][r];"
                "[l]hflip[lf];[lf][r]hstack,split[b1][b2];[b2]gblur=sigma=14[glow];"
                "[b1][glow]overlay=format=auto,colorchannelmixer=aa=0.9[viz];"
                "[bg][viz]overlay=0:H-h-40:format=auto,format=yuv420p[v]"
            )
        elif style == "wave":
            graph = (
                bg + "[bg];"
                "[1:a]showwaves=s=1600x160:mode=cline:rate=24:colors=0xffffff:scale=sqrt,"
                "format=rgba,colorkey=0x000000:0.1:0.1,split[w1][w2];[w2]gblur=sigma=10[g];"
                "[w1][g]overlay=format=auto[viz];"
                "[bg][viz]overlay=(W-w)/2:H-h-120:format=auto,format=yuv420p[v]"
            )
        else:  # pulse
            graph = bg + "[v]"
    _run([
        "ffmpeg", "-y", "-loop", "1", "-framerate", str(FPS), "-t", f"{duration:.2f}", "-i", str(image),
        "-i", str(audio), "-filter_complex", graph, "-map", "[v]", "-map", "1:a",
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

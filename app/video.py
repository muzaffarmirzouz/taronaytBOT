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


def pick_style(exclude: str | None = None, allowed: tuple = STYLES) -> str:
    """Random look for each video. VIDEO_STYLE=pulse|bars|wave|plain pins one look; default is random."""
    forced = os.environ.get("VIDEO_STYLE", "random").strip().lower()
    if forced in allowed or (forced == "plain" and allowed == STYLES):
        return forced
    options = [x for x in allowed if x != exclude] or list(allowed)
    return random.choice(options)


FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
)


def _font() -> str | None:
    return next((f for f in FONT_CANDIDATES if os.path.exists(f)), None)


def frame_from(video_path: Path, out_path: Path) -> Path:
    """Grab one frame of a background clip (used for the YouTube thumbnail)."""
    _run(["ffmpeg", "-y", "-ss", "1", "-i", str(video_path), "-frames:v", "1", str(out_path)], timeout=120)
    return out_path


def make_loop(src: Path, out_path: Path, max_seconds: int = 30) -> Path:
    """Normalise an owner's clip to 1080p/24fps and blend its end into its start so that,
    played on repeat, there is no visible jump. Works in two steps so odd inputs (audio longer
    than video, variable fps, rotation, 10-bit) cannot break the crossfade timing."""
    import tempfile
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        norm = Path(tmp) / "norm.mp4"
        _run(["ffmpeg", "-y", "-i", str(src), "-t", str(max_seconds), "-map", "0:v:0", "-an",
              "-vf", "scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,setsar=1,fps=24,format=yuv420p",
              "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", str(norm)], timeout=600)
        d = probe_duration(norm) - 0.08  # a little margin: the container can be longer than the last frame
        if d < 2:
            raise ValueError("Video juda qisqa (kamida 2 soniya bo'lsin)")
        f = min(1.0, d / 4)
        graph = (
            "[0:v]split[a][b];"
            f"[a]trim=start={f:.3f}:end={d:.3f},setpts=PTS-STARTPTS[tail];[b]trim=end={f:.3f},setpts=PTS-STARTPTS[head];"
            f"[tail][head]xfade=transition=fade:duration={f:.3f}:offset={d - 2 * f:.3f},format=yuv420p[v]"
        )
        try:
            _run(["ffmpeg", "-y", "-i", str(norm), "-filter_complex", graph, "-map", "[v]", "-an",
                  "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-movflags", "+faststart", str(out_path)],
                 timeout=600)
            if probe_duration(out_path) < 1:
                raise RuntimeError("empty loop")
        except Exception:
            log.exception("crossfade failed; storing the plain normalised clip")
            _run(["ffmpeg", "-y", "-i", str(norm), "-c", "copy", "-movflags", "+faststart", str(out_path)])
    return out_path


def master_audio(src: Path, out_path: Path) -> Path:
    """Light 'radio' mastering: high-pass, gentle compression, loudness to -14 LUFS (YouTube's reference level)."""
    _run(["ffmpeg", "-y", "-i", str(src), "-af",
          "highpass=f=25,acompressor=threshold=-18dB:ratio=2.5:attack=20:release=250:makeup=2,"
          "loudnorm=I=-14:TP=-1.0:LRA=9",
          "-ar", "44100", "-c:a", "libmp3lame", "-b:a", "256k", str(out_path)], timeout=600)
    return out_path


def make_preview(src: Path, out_path: Path, max_mb: float = 40.0) -> Path:
    """Small, low-quality copy for showing in Telegram (bots can send <=50 MB). The full-quality file is what YouTube gets."""
    dur = max(probe_duration(src), 1.0)
    audio_k = 96
    video_k = int(max(150, min(1500, (max_mb * 8192 / dur) - audio_k)))
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    _run(["ffmpeg", "-y", "-i", str(src), "-vf", "scale=640:-2,fps=20", "-c:v", "libx264", "-preset", "veryfast",
          "-b:v", f"{video_k}k", "-maxrate", f"{int(video_k * 1.4)}k", "-bufsize", f"{video_k * 2}k",
          "-c:a", "aac", "-b:a", f"{audio_k}k", "-movflags", "+faststart", str(out_path)], timeout=900)
    return out_path


def concat_clips(srcs: list, out_path: Path) -> Path:
    """Join several clips (any size/fps) into one 1080p/24fps clip, in the given order."""
    if len(srcs) < 2:
        raise ValueError("Kamida 2 ta video kerak")
    cmd = ["ffmpeg", "-y"]
    parts = []
    for i, s in enumerate(srcs):
        cmd += ["-i", str(s)]
        parts.append(f"[{i}:v]scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,"
                     f"fps=24,format=yuv420p,setsar=1,setpts=PTS-STARTPTS[v{i}]")
    graph = ";".join(parts) + ";" + "".join(f"[v{i}]" for i in range(len(srcs))) + f"concat=n={len(srcs)}:v=1:a=0[v]"
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    _run(cmd + ["-filter_complex", graph, "-map", "[v]", "-an", "-c:v", "libx264", "-preset", "veryfast",
                "-crf", "20", str(out_path)], timeout=600)
    return out_path


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


def _title_filter(title: str, tmp: Path) -> str:
    """drawtext filter that writes the song name in the top-left corner (empty if no font/title)."""
    font = _font()
    if not title.strip() or not font:
        return ""
    tf = tmp / "title.txt"
    tf.write_text("\u266a  " + title.strip()[:60], encoding="utf-8")
    return (f"drawtext=fontfile={font}:textfile={tf}:expansion=none:fontsize=58:fontcolor=white"
            ":shadowcolor=black@0.7:shadowx=3:shadowy=3:x=64:y=54:alpha='min(t/1.5,1)'")


def make_video(image: Path, audio: Path, out_path: Path, style: str | None = None,
               bg_video: Path | None = None, title: str = "") -> Path:
    """1920x1080 mp4.
    With bg_video: the owner's clip loops until the song ends, the song title is written on it and a
    music visualizer (bars or wave) runs along the bottom.
    Without: the cover picture "breathes" with the bass (pulse) and may get bars/wave on top."""
    import tempfile
    duration = probe_duration(audio)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(duration * FPS) + 1
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        titlef = _title_filter(title, tmp) if bg_video else ""
        if bg_video:
            style = style if style in ("bars", "wave") else "none"
            inputs = ["-stream_loop", "-1", "-i", str(bg_video), "-i", str(audio)]
            bg = ("[0:v]scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,fps=24,"
                  "vignette=PI/5,format=yuv420p" + ("," + titlef if titlef else ""))
        else:
            style = style or pick_style()
            inputs = ["-loop", "1", "-framerate", str(FPS), "-t", f"{duration:.2f}", "-i", str(image),
                      "-i", str(audio)]
            if style == "plain":
                bg = ("[0:v]scale=2112:1188:force_original_aspect_ratio=increase,crop=2112:1188,"
                      f"crop=1920:1080:x='(in_w-out_w)*t/{duration:.2f}':y='(in_h-out_h)/2',format=yuv420p")
            else:
                zoom = _zoom_expr(beat_times(audio), frames)
                bg = ("[0:v]scale=2112:1188:force_original_aspect_ratio=increase,crop=2112:1188,setpts=N/24/TB,"
                      f"zoompan=z='{zoom}':d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=1920x1080:fps={FPS},"
                      "vignette=PI/5,format=yuv420p")
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
        else:  # pulse / plain / none: no overlay
            graph = bg + "[v]"
        cmd = ["ffmpeg", "-y", *inputs, "-filter_complex", graph, "-map", "[v]", "-map", "1:a",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-c:a", "aac", "-b:a", "192k",
               "-t", f"{duration:.2f}", "-movflags", "+faststart", str(out_path)]
        try:
            _run(cmd)
        except RuntimeError:
            if not titlef:
                raise
            log.warning("title overlay failed; rendering without the title")
            return make_video(image, audio, out_path, style=style, bg_video=bg_video, title="")
    return out_path


def make_thumbnail(image: Path, out_path: Path, title: str = "") -> Path:
    import tempfile
    import textwrap
    out_path = Path(out_path)
    base = "scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720"
    font = _font()
    if title.strip() and font:
        with tempfile.TemporaryDirectory() as tmp:
            tf = Path(tmp) / "t.txt"
            tf.write_text(textwrap.fill(title.strip()[:60], 20), encoding="utf-8")
            vf = (base + f",drawtext=fontfile={font}:textfile={tf}:expansion=none:fontsize=88:fontcolor=white"
                  ":box=1:boxcolor=black@0.35:boxborderw=22:shadowcolor=black@0.8:shadowx=4:shadowy=4"
                  ":x=70:y=h-text_h-90")
            try:
                _run(["ffmpeg", "-y", "-i", str(image), "-vf", vf, "-frames:v", "1", "-q:v", "3", str(out_path)],
                     timeout=120)
                return out_path
            except RuntimeError:
                log.warning("thumbnail title failed; making a plain thumbnail")
    _run(["ffmpeg", "-y", "-i", str(image), "-vf", base, "-frames:v", "1", "-q:v", "3", str(out_path)], timeout=120)
    return out_path

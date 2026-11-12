"""One long mix from many finished songs.

A week of three-minute songs becomes a single hour-long video: the tracks are crossfaded into one stream and the
picture changes with every track, cycling through the logo themes and the owner's photo scenes. People search for
this kind of video by mood rather than by artist name, which is the one place an unknown channel can be found.

The picture is built from short seamless loops rather than rendered frame by frame for the full hour, so an hour
of video costs a few minutes of work instead of a few hours.
"""
from __future__ import annotations

import logging
import math
import subprocess
import tempfile
from pathlib import Path

from . import brand, photofx, video

log = logging.getLogger("longmix")

CROSSFADE = 2.0  # seconds of overlap between tracks
LOOP_SECONDS = 20  # length of each background loop before it repeats


def _fmt(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def join_audio(tracks: list, out_mp3: Path) -> list:
    """Crossfade the songs into one file. Returns [(title, start_seconds), ...] for the chapter list."""
    if len(tracks) < 2:
        raise ValueError("Kamida 2 ta qo'shiq kerak")
    cmd = ["ffmpeg", "-y", "-v", "error"]
    for _, path in tracks:
        cmd += ["-i", str(path)]
    steps, label = [], "[0:a]"
    for i in range(1, len(tracks)):
        nxt = f"[m{i}]"
        steps.append(f"{label}[{i}:a]acrossfade=d={CROSSFADE}:c1=tri:c2=tri{nxt}")
        label = nxt
    graph = ";".join(steps) if steps else ""
    Path(out_mp3).parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(cmd + ["-filter_complex", graph, "-map", label, "-c:a", "libmp3lame", "-b:a", "256k", str(out_mp3)],
                   check=True, timeout=3600, capture_output=True)

    chapters, at = [], 0.0
    for i, (title, path) in enumerate(tracks):
        chapters.append((title, max(0.0, at)))
        at += video.probe_duration(path) - (CROSSFADE if i < len(tracks) - 1 else 0)
    return chapters


def loop_name(look) -> str:
    return f"brand{look[1]}" if look[0] == "brand" else f"photo_{Path(look[1][0]).stem}_{look[1][1]}"


def _loop_for(look, audio: Path, cache: Path, work: Path) -> Path:
    """A short seamless background clip, rendered once and then kept: later mixes reuse it in seconds.

    look is ('brand', n) or ('photo', (path, effects))."""
    cache.mkdir(parents=True, exist_ok=True)
    out = cache / f"{loop_name(look)}.mp4"
    raw = work / f"raw_{loop_name(look)}.mp4"
    if out.exists() and out.stat().st_size > 10_000:
        return out
    if look[0] == "photo":
        photofx.render(audio, raw, look[1][0], look[1][1], "", max_seconds=LOOP_SECONDS + 4)
    else:
        brand.render(audio, raw, look[1], "", max_seconds=LOOP_SECONDS + 4)
    video.make_loop(raw, out, max_seconds=LOOP_SECONDS)
    raw.unlink(missing_ok=True)
    return out


def build(tracks: list, looks: list, out_mp4: Path, work: Path, cover_path: Path | None = None,
          title: str = "", progress=None, cache: Path | None = None) -> dict:
    """tracks: [(title, mp3_path)]. looks: [('brand', n) | ('photo', (path, fx))]. Returns mix facts."""
    if not looks:
        raise ValueError("Fon yo'q")
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    cache = Path(cache) if cache else work / "loops"
    mixed = work / "mix.mp3"
    if progress:
        progress(f"🎚 {len(tracks)} ta qo'shiq birlashtirilmoqda...")
    chapters = join_audio(tracks, mixed)
    total = video.probe_duration(mixed)

    # one background per track, cycling through the pool; each is rendered once and then reused
    per_track = [looks[i % len(looks)] for i in range(len(tracks))]
    wanted = list(dict.fromkeys(loop_name(l) for l in per_track))
    loops: dict = {}
    for look in per_track:
        key = loop_name(look)
        if key in loops:
            continue
        if progress:
            fresh = not (cache / f"{key}.mp4").exists()
            progress(f"🎬 Fon {len(loops) + 1}/{len(wanted)}{' chizilmoqda...' if fresh else ' (tayyor)'}")
        loops[key] = _loop_for(look, mixed, cache, work)
    loop_len = video.probe_duration(next(iter(loops.values())))

    # repeat each loop enough to cover its track, keeping the picture roughly in step with the audio
    lines, shown, target = [], 0.0, 0.0
    for i, (track, look) in enumerate(zip(tracks, per_track)):
        key = loop_name(look)
        target += video.probe_duration(track[1]) - (CROSSFADE if i < len(tracks) - 1 else 0)
        repeats = max(1, round((target - shown) / loop_len))
        lines += [f"file '{loops[key].resolve()}'"] * repeats
        shown += repeats * loop_len
    while shown < total + loop_len:  # never end before the music does
        lines.append(f"file '{loops[next(iter(loops))].resolve()}'")
        shown += loop_len

    listfile = work / "parts.txt"
    listfile.write_text("\n".join(lines) + "\n")
    silent = work / "bg_all.mp4"
    if progress:
        progress(f"🧩 {_fmt(total)} lik fon yig'ilmoqda...")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(listfile),
                    "-c", "copy", str(silent)], check=True, timeout=3600, capture_output=True)

    Path(out_mp4).parent.mkdir(parents=True, exist_ok=True)
    if progress:
        progress("🎞 Video va ovoz birlashtirilmoqda...")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(silent), "-i", str(mixed), "-map", "0:v", "-map", "1:a",
                    "-t", f"{total:.2f}", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
                    str(out_mp4)], check=True, timeout=3600, capture_output=True)

    if cover_path:
        first = per_track[0]
        if first[0] == "photo":
            photofx.cover_frame(cover_path, first[1][0], first[1][1], title)
        else:
            brand.cover_frame(cover_path, first[1], title)

    for leftover in (silent, listfile):  # the loops are kept: the next mix reuses them
        Path(leftover).unlink(missing_ok=True)
    return {
        "mp3": mixed,
        "duration": total,
        "tracks": len(tracks),
        "backgrounds": len(loops),
        "chapters": [(name, _fmt(at)) for name, at in chapters],
    }


def description(info: dict, chapters: list) -> str:
    """YouTube text with a timestamped track list, which YouTube turns into chapters."""
    lines = ["Full mix — every track made with AI. Press play and let it run.", "", "Tracklist:"]
    lines += [f"{at} {name}" for name, at in chapters]
    return "\n".join(lines)

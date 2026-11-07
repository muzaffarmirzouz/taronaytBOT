"""The owner's library of short looping background clips (made in Flow etc.)."""
from __future__ import annotations

import os
import random
import re
import time
import threading
from pathlib import Path

from . import video

_lock = threading.Lock()


def folder(cfg) -> Path:
    d = Path(cfg.data_dir) / "backgrounds"
    d.mkdir(parents=True, exist_ok=True)
    return d


def list_all(cfg) -> list:
    """[(id, path), ...] sorted by id."""
    items = []
    for f in folder(cfg).glob("bg_*.mp4"):
        m = re.fullmatch(r"bg_(\d+)\.mp4", f.name)
        if m:
            items.append((int(m.group(1)), f))
    return sorted(items)


def add(cfg, src: Path) -> tuple:
    """Convert an uploaded clip into a seamless loop and store it. Returns (id, seconds)."""
    with _lock:
        new_id = max([i for i, _ in list_all(cfg)], default=0) + 1
        out = folder(cfg) / f"bg_{new_id}.mp4"
        try:
            video.make_loop(Path(src), out)
        except Exception:
            out.unlink(missing_ok=True)
            raise
        os.utime(out, (0, 0))  # never used yet: the next song picks it first
        return new_id, video.probe_duration(out)


def add_many(cfg, srcs: list) -> tuple:
    """Join several uploaded clips (in order) into one background. Returns (id, seconds)."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        joined = Path(tmp) / "joined.mp4"
        video.concat_clips([Path(x) for x in srcs], joined)
        return add(cfg, joined)


def remove(cfg, bg_id: int) -> bool:
    for i, path in list_all(cfg):
        if i == bg_id:
            path.unlink(missing_ok=True)
            return True
    return False


def pick(cfg, exclude: int | None = None):
    """Next background (id, path): the least recently used one, so every clip gets its turn and a
    newly added clip is used first. `exclude` is avoided when there is a choice. None if empty.
    Last use is kept in each file's modification time."""
    items = list_all(cfg)
    if not items:
        return None
    options = [x for x in items if x[0] != exclude] or items
    oldest = min(x[1].stat().st_mtime for x in options)
    choice = random.choice([x for x in options if x[1].stat().st_mtime <= oldest + 1e-6])
    try:
        os.utime(choice[1], (time.time(), time.time()))
    except OSError:
        pass
    return choice

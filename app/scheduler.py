"""Posting-slot logic (pure functions, easy to test)."""
from __future__ import annotations

from datetime import datetime, timedelta


def parse_times(times: list) -> list:
    out = []
    for t in times:
        hour, minute = t.split(":")
        h, m = int(hour), int(minute)
        if not (0 <= h < 24 and 0 <= m < 60):
            raise ValueError(f"bad time: {t}")
        out.append((h, m))
    return sorted(out)


def slot_key(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


def due_slot(now_local: datetime, times: list, done: set, window_hours: int = 3):
    """The earliest posting slot that has already passed (within the catch-up window)
    and is not done yet, or None. After a restart we only catch up on recent slots,
    so the bot never dumps a pile of videos at once."""
    for hour, minute in parse_times(times):
        slot = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if now_local >= slot and now_local - slot <= timedelta(hours=window_hours):
            key = slot_key(slot)
            if key not in done:
                return key
    return None


def next_slot(now_local: datetime, times: list) -> datetime:
    parsed = parse_times(times)
    for hour, minute in parsed:
        slot = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if slot > now_local:
            return slot
    hour, minute = parsed[0]
    tomorrow = now_local + timedelta(days=1)
    return tomorrow.replace(hour=hour, minute=minute, second=0, microsecond=0)

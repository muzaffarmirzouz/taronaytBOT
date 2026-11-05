"""Environment-driven configuration. No secrets live in the code."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _bool(name: str, default: bool) -> bool:
    return _env(name, "1" if default else "0").lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


@dataclass
class Config:
    telegram_token: str
    admin_id: int
    mureka_key: str
    mureka_base: str
    mureka_model: str
    mureka_n: int
    anthropic_key: str
    claude_model: str
    openai_key: str
    image_model: str
    image_size: str
    image_quality: str
    yt_client_id: str
    yt_client_secret: str
    yt_refresh_token: str
    yt_privacy: str
    yt_synthetic: bool
    yt_category: str
    timezone: str
    post_times: list
    stock_target: int
    max_mureka_per_day: int
    data_dir: Path
    channel_footer: str

    @classmethod
    def from_env(cls) -> "Config":
        times = [t.strip() for t in _env("POST_TIMES", "08:00,13:00,19:00").split(",") if t.strip()]
        return cls(
            telegram_token=_env("TELEGRAM_BOT_TOKEN"),
            admin_id=_int("ADMIN_ID", 0),
            mureka_key=_env("MUREKA_API_KEY"),
            mureka_base=_env("MUREKA_BASE_URL", "https://api.mureka.ai").rstrip("/"),
            mureka_model=_env("MUREKA_MODEL", "auto"),
            mureka_n=_int("MUREKA_N", 1),  # 1 song per request = half the cost of the default 2
            anthropic_key=_env("ANTHROPIC_API_KEY"),
            claude_model=_env("CLAUDE_MODEL", "claude-sonnet-5-5"),
            openai_key=_env("OPENAI_API_KEY"),
            image_model=_env("OPENAI_IMAGE_MODEL", "gpt-image-2"),
            image_size=_env("OPENAI_IMAGE_SIZE", "1536x1024"),
            image_quality=_env("OPENAI_IMAGE_QUALITY", "medium"),
            yt_client_id=_env("YT_CLIENT_ID"),
            yt_client_secret=_env("YT_CLIENT_SECRET"),
            yt_refresh_token=_env("YT_REFRESH_TOKEN"),
            yt_privacy=_env("YT_PRIVACY", "public"),
            yt_synthetic=_bool("YT_SYNTHETIC_MEDIA", True),
            yt_category=_env("YT_CATEGORY", "10"),  # 10 = Music
            timezone=_env("TIMEZONE", "America/New_York"),
            post_times=times,
            stock_target=_int("STOCK_TARGET", max(2 * len(times), 3)),
            max_mureka_per_day=_int("MAX_MUREKA_PER_DAY", 12),
            data_dir=Path(_env("DATA_DIR", "./data")),
            channel_footer=_env("CHANNEL_FOOTER", ""),
        )

    def missing(self) -> list:
        required = {
            "TELEGRAM_BOT_TOKEN": self.telegram_token,
            "ADMIN_ID": self.admin_id,
            "MUREKA_API_KEY": self.mureka_key,
            "ANTHROPIC_API_KEY": self.anthropic_key,
            "OPENAI_API_KEY": self.openai_key,
            "YT_CLIENT_ID": self.yt_client_id,
            "YT_CLIENT_SECRET": self.yt_client_secret,
            "YT_REFRESH_TOKEN": self.yt_refresh_token,
        }
        return [name for name, value in required.items() if not value]

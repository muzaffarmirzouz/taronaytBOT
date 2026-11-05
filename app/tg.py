"""Minimal Telegram Bot API client (long polling) built on `requests`."""
from __future__ import annotations

import logging

from . import net

log = logging.getLogger("telegram")


class Telegram:
    def __init__(self, token: str, admin_id: int):
        self.base = f"https://api.telegram.org/bot{token}"
        self.admin_id = admin_id

    def call(self, method: str, timeout: int = 30, retries: int = 3, **params) -> dict:
        resp = net.request("POST", f"{self.base}/{method}", json=params, timeout=timeout, retries=retries)
        try:
            data = resp.json()
        except ValueError:
            raise RuntimeError(f"Telegram {method}: non-JSON reply ({resp.status_code})")
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method} error: {data.get('description')}")
        return data["result"]

    def send(self, text: str, chat_id: int | None = None) -> None:
        chat_id = chat_id or self.admin_id
        text = text or "(bo'sh)"
        for i in range(0, len(text), 4000):
            try:
                self.call("sendMessage", chat_id=chat_id, text=text[i:i + 4000],
                          disable_web_page_preview=False)
            except Exception as exc:
                log.warning("sendMessage failed: %s", exc)

    def updates(self, offset: int | None) -> list:
        params = {"timeout": 50, "allowed_updates": ["message"]}
        if offset is not None:
            params["offset"] = offset
        return self.call("getUpdates", timeout=65, retries=1, **params)

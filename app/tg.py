"""Minimal Telegram Bot API client (long polling) built on `requests`."""
from __future__ import annotations

import logging

from . import net

log = logging.getLogger("telegram")


class Telegram:
    def __init__(self, token: str, admin_id: int):
        self.base = f"https://api.telegram.org/bot{token}"
        self.admin_id = admin_id

    def call(self, method: str, http_timeout: int = 30, retries: int = 3, **params) -> dict:
        resp = net.request("POST", f"{self.base}/{method}", json=params, timeout=http_timeout, retries=retries)
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

    def send_audio(self, path, title: str = "", caption: str = "", chat_id: int | None = None) -> bool:
        """Upload an audio file (<=50 MB). Never raises; returns False on failure."""
        try:
            with open(path, "rb") as fh:
                resp = net.request(
                    "POST", f"{self.base}/sendAudio", retries=1, timeout=180,
                    data={"chat_id": chat_id or self.admin_id, "title": title[:64],
                          "caption": caption[:1000]},
                    files={"audio": fh})
            return bool(resp.json().get("ok"))
        except Exception as exc:
            log.warning("sendAudio failed: %s", exc)
            return False

    def send_buttons(self, text: str, buttons: list, chat_id: int | None = None) -> None:
        """Message with inline buttons: a list of (label, callback_data), or a list of such rows."""
        rows = buttons if buttons and isinstance(buttons[0], list) else [buttons]
        try:
            self.call("sendMessage", chat_id=chat_id or self.admin_id, text=text,
                      reply_markup={"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row]
                                                        for row in rows]})
        except Exception as exc:
            log.warning("sendMessage(buttons) failed: %s", exc)

    def send_video(self, path, caption: str = "", buttons: list | None = None, chat_id: int | None = None) -> bool:
        """Upload a video (<=50 MB) with optional inline buttons [(text, callback_data), ...]. Never raises."""
        import json as _json
        try:
            data = {"chat_id": chat_id or self.admin_id, "caption": caption[:1000], "supports_streaming": "true"}
            if buttons:
                data["reply_markup"] = _json.dumps({"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in buttons]]})
            with open(path, "rb") as fh:
                resp = net.request("POST", f"{self.base}/sendVideo", retries=1, timeout=300,
                                   data=data, files={"video": fh})
            return bool(resp.json().get("ok"))
        except Exception as exc:
            log.warning("sendVideo failed: %s", exc)
            return False

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        try:
            self.call("answerCallbackQuery", callback_query_id=callback_id, text=text[:180])
        except Exception as exc:
            log.warning("answerCallbackQuery failed: %s", exc)

    def updates(self, offset: int | None) -> list:
        params = {"timeout": 50, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            params["offset"] = offset
        return self.call("getUpdates", http_timeout=65, retries=1, **params)

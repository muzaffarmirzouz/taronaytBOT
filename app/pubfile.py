"""A tiny web server that hands one file to one outside service for a few minutes.

WaveSpeed's upload endpoint fetches the audio over the open web, so a song sitting on the Railway disk has to be
reachable for a moment. Each file gets a long random address that expires, and nothing else is served: no listing,
no other path, no second life for the address once it is stale.

Set PUBLIC_BASE_URL (or let Railway's RAILWAY_PUBLIC_DOMAIN do it) to turn this on. Without it, nothing is served
and the caller falls back to a Telegram file link.
"""
from __future__ import annotations

import logging
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger("pubfile")

TTL_SECONDS = 1200  # 20 minutes is plenty for the other side to fetch the file once
_lock = threading.Lock()
_served: dict = {}  # token -> (path, expires_at)
_server = None


def base_url() -> str:
    """The address the outside world reaches this bot on, or '' when there is none."""
    base = (os.environ.get("PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if not base:
        domain = (os.environ.get("RAILWAY_PUBLIC_DOMAIN") or "").strip().rstrip("/")
        base = f"https://{domain}" if domain else ""
    if base and "://" not in base:
        base = "https://" + base
    return base


def available() -> bool:
    return bool(base_url())


def _sweep(now: float) -> None:
    for token in [t for t, (_, exp) in _served.items() if exp < now]:
        _served.pop(token, None)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # the default handler prints to stderr on every hit
        pass

    def _lookup(self):
        token = self.path.lstrip("/").split("/")[-1].split("?")[0]
        token = token[:-4] if token.endswith(".mp3") else token
        now = time.time()
        with _lock:
            _sweep(now)
            entry = _served.get(token)
        if not entry:
            return None
        path = Path(entry[0])
        return path if path.exists() else None

    def do_HEAD(self):
        path = self._lookup()
        if path is None:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Content-Length", str(path.stat().st_size))
        self.end_headers()

    def do_GET(self):
        if self.path.split("?")[0] in ("/", "/health"):  # Railway's health check
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        path = self._lookup()
        if path is None:
            self.send_error(404)
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def start(port: int | None = None) -> None:
    """Run the server in the background. Safe to call twice."""
    global _server
    if _server is not None:
        return
    port = port or int(os.environ.get("PORT", "8080"))
    try:
        _server = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    except OSError as exc:
        log.warning("file server could not start on port %s: %s", port, exc)
        return
    threading.Thread(target=_server.serve_forever, daemon=True, name="pubfile").start()
    log.info("file server listening on port %s (base %s)", port, base_url() or "-")


def publish(path, ttl: int = TTL_SECONDS) -> str:
    """Return a temporary public URL for this file, or '' when no public address is configured."""
    base = base_url()
    if not base:
        return ""
    start()
    token = secrets.token_urlsafe(24)
    with _lock:
        _sweep(time.time())
        _served[token] = (str(Path(path).resolve()), time.time() + ttl)
    return f"{base}/f/{token}.mp3"


def withdraw(url: str) -> None:
    """Stop serving a file as soon as the other side has taken it."""
    token = url.rstrip("/").split("/")[-1]
    token = token[:-4] if token.endswith(".mp3") else token
    with _lock:
        _served.pop(token, None)

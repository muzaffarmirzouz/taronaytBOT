"""One-time helper: get a YouTube refresh token for the bot.

Run this on YOUR computer (not on the server):
    python3 auth_youtube.py
It opens a browser, you pick the YouTube channel's Google account and allow access,
and the script prints YT_REFRESH_TOKEN. Uses only the standard library + requests.
"""
import http.server
import os
import threading
import urllib.parse
import webbrowser

import requests

PORT = 8765
REDIRECT = f"http://localhost:{PORT}"
SCOPE = "https://www.googleapis.com/auth/youtube.upload"


def main() -> None:
    client_id = os.environ.get("YT_CLIENT_ID") or input("YT_CLIENT_ID: ").strip()
    client_secret = os.environ.get("YT_CLIENT_SECRET") or input("YT_CLIENT_SECRET: ").strip()

    result = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            result["code"] = (query.get("code") or [""])[0]
            result["error"] = (query.get("error") or [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<h3>Tayyor! Bu oynani yopib, terminalga qayting.</h3>".encode())
            threading.Thread(target=self.server.shutdown, daemon=True).start()

        def log_message(self, *args):
            pass

    params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
    }
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)
    server = http.server.HTTPServer(("localhost", PORT), Handler)
    print("Brauzer ochilmasa, bu havolani oching:\n" + url + "\n")
    webbrowser.open(url)
    server.serve_forever()

    if not result.get("code"):
        raise SystemExit(f"Ruxsat olinmadi: {result.get('error') or 'kod kelmadi'}")

    resp = requests.post("https://oauth2.googleapis.com/token", data={
        "code": result["code"],
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": REDIRECT,
        "grant_type": "authorization_code",
    }, timeout=30)
    data = resp.json()
    if "refresh_token" not in data:
        raise SystemExit(f"Refresh token kelmadi: {data}")
    print("\nYT_REFRESH_TOKEN (buni Railway'ga qo'ying, hech kimga bermang):\n")
    print(data["refresh_token"])


if __name__ == "__main__":
    main()

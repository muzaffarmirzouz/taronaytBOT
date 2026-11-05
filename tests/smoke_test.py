"""Offline end-to-end test: every network call is faked, ffmpeg runs for real.
Run:  python3 -I tests/smoke_test.py   (from the project folder)
"""
import base64
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import claude_api, mureka, net, scheduler, youtube  # noqa: E402
from app.config import Config  # noqa: E402
from app.db import DB  # noqa: E402
from app.pipeline import Pipeline  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="musicbot_test_"))


def sh(*cmd):
    subprocess.run(cmd, check=True, capture_output=True)


# real media used by the fakes
SONG = TMP / "fake.mp3"
PNG = TMP / "fake.png"
sh("ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=6", "-c:a", "libmp3lame", str(SONG))
sh("ffmpeg", "-y", "-f", "lavfi", "-i", "gradients=s=1536x1024:d=1", "-frames:v", "1", str(PNG))

PACKAGE = {
    "title": "Take Your Pain",
    "theme": "playful love",
    "lyrics": "[Verse]\nIf the world throws thorns your way\nI'll take the sting\n\n[Chorus]\nI'll take your pain, darling",
    "style": "upbeat playful pop, male vocal, handclaps, acoustic guitar, 110 bpm",
    "image_prompt": "a cozy sunny garden table with steaming soup, watercolor",
    "yt_title": "Take Your Pain (Official Lyric Video) | Upbeat Pop Love Song",
    "yt_description": "A playful love song about taking care of someone.\n\nLyrics:\n[Verse]\n...",
    "yt_tags": ["pop", "love song", "new song 2026", "lyric video", "pop", "feel good"],
}

calls = []
poll_state = {"n": 0}


class Resp:
    def __init__(self, status=200, js=None, content=b"", headers=None):
        self.status_code, self._js, self.content, self.headers = status, js, content, headers or {}
        self.text = json.dumps(js) if js is not None else content[:50].decode("latin1")

    def json(self):
        return self._js


def fake_request(method, url, **kw):
    calls.append((method, url))
    if "api.anthropic.com" in url:
        return Resp(js={"content": [{"type": "text", "text": "```json\n" + json.dumps(PACKAGE) + "\n```"}]})
    if url.endswith("/v1/song/generate"):
        assert kw["json"]["lyrics"] and kw["json"]["prompt"]
        return Resp(js={"id": "task1", "status": "preparing"})
    if "/v1/song/query/" in url:
        poll_state["n"] += 1
        if poll_state["n"] < 2:
            return Resp(js={"id": "task1", "status": "running"})
        return Resp(js={"id": "task1", "status": "succeeded",
                        "choices": [{"url": "https://cdn.example/song.mp3", "duration": 6000}]})
    if url == "https://cdn.example/song.mp3":
        return Resp(content=SONG.read_bytes())
    if "api.openai.com" in url:
        return Resp(js={"data": [{"b64_json": base64.b64encode(PNG.read_bytes()).decode()}]})
    if "oauth2.googleapis.com" in url:
        return Resp(js={"access_token": "tok", "expires_in": 3600})
    if "youtube/v3/videos" in url and method == "POST":
        meta = json.loads(kw["data"])
        assert meta["status"]["containsSyntheticMedia"] is True
        assert "<" not in meta["snippet"]["title"]
        assert len(meta["snippet"]["tags"]) == 5, meta["snippet"]["tags"]  # duplicate "pop" removed
        return Resp(headers={"Location": "https://upload.example/session1"})
    if url == "https://upload.example/session1":
        assert hasattr(kw["data"], "read")
        return Resp(js={"id": "VID123"})
    if "api.telegram.org" in url:
        if url.endswith("/getUpdates"):
            assert kw["json"]["timeout"] == 50 and kw["timeout"] == 65, kw
            return Resp(js={"ok": True, "result": [{"update_id": 7}]})
        return Resp(js={"ok": True, "result": {}})
    if "thumbnails/set" in url:
        return Resp(js={})
    raise AssertionError(f"unexpected request {method} {url}")


net.request = fake_request
mureka.POLL_SECONDS = 0

os.environ.update({
    "DATA_DIR": str(TMP / "data"), "MUREKA_API_KEY": "k", "ANTHROPIC_API_KEY": "k", "OPENAI_API_KEY": "k",
    "YT_CLIENT_ID": "i", "YT_CLIENT_SECRET": "s", "YT_REFRESH_TOKEN": "r",
    "TELEGRAM_BOT_TOKEN": "t", "ADMIN_ID": "1", "MAX_MUREKA_PER_DAY": "2",
})
cfg = Config.from_env()
assert cfg.missing() == [], cfg.missing()

db = DB(cfg.data_dir / "bot.sqlite3")
sent = []
pipe = Pipeline(cfg, db, sent.append)

# 1) user job: produce -> ready -> publish
job_id = db.add_job("user", raw_text="Qulab ketar ko'ksimga to'g'lar, senga tikan kirar bo'lsa ham...")
pipe.produce(job_id)
job = db.get(job_id)
assert job["status"] == "ready", job
assert job["title"] == "Take Your Pain" and Path(job["mp3_path"]).exists()

out = subprocess.run(
    ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height,codec_name",
     "-show_entries", "format=duration", "-of", "json", job["mp4_path"]],
    capture_output=True, text=True, check=True)
info = json.loads(out.stdout)
assert (info["streams"][0]["width"], info["streams"][0]["height"]) == (1920, 1080), info
assert 5.5 < float(info["format"]["duration"]) < 6.6, info

vid = pipe.publish(job_id)
job = db.get(job_id)
assert vid == "VID123" and job["status"] == "posted" and job["youtube_id"] == "VID123", job
desc = pipe.build_description(job, json.loads(job["yt_tags"]))
assert "created with AI" in desc and "#Pop" in desc, desc

# 2) resume: a job with lyrics + mp3 already present must not call Mureka again
before = len([c for c in calls if c[1].endswith("/v1/song/generate")])
job2 = db.add_job("auto", theme="lo-fi chill | late night")
pipe.produce(job2)
assert db.get(job2)["status"] == "ready"
assert pipe.mureka_used_today() == 2
# 3) daily Mureka cap stops a third paid request
job3 = db.add_job("auto", theme="x | y")
try:
    pipe.produce(job3)
    raise SystemExit("budget cap did not trigger")
except Exception as exc:
    assert exc.__class__.__name__ == "BudgetExceeded", exc

# 4) queue priority: user jobs first
db.update(job3, status="queued")
ujob = db.add_job("user", raw_text="x" * 30)
assert db.next_to_produce()["id"] == ujob

# 5) crash recovery
db.update(job2, status="producing")
db2 = DB(cfg.data_dir / "bot.sqlite3")
assert db2.get(job2)["status"] == "queued"

# 6) scheduler slots
tz = ZoneInfo("America/New_York")
times = ["08:00", "13:00", "19:00"]
now = datetime(2026, 11, 6, 13, 20, tzinfo=tz)
assert scheduler.due_slot(now, times, set()) == "2026-11-06 13:00"
assert scheduler.due_slot(now, times, {"2026-11-06 13:00"}) is None
assert scheduler.due_slot(datetime(2026, 11, 6, 17, 0, tzinfo=tz), times, {"2026-11-06 13:00"}) is None  # 08:00 too old
assert scheduler.next_slot(now, times).strftime("%H:%M") == "19:00"
assert scheduler.next_slot(datetime(2026, 11, 6, 20, 0, tzinfo=tz), times).strftime("%d %H:%M") == "07 08:00"

# 7) youtube text cleaning
assert youtube.clean_text("a<b>c", 10) == "abc"
assert len(",".join(youtube.clean_tags(["tag%d" % i + "x" * 25 for i in range(40)]))) <= 500

# 8) Claude reply parsing + validation
assert claude_api._extract_json('noise {"a": 1} tail') == {"a": 1}
try:
    claude_api._validate({"title": "x"})
    raise SystemExit("validation should fail")
except ValueError:
    pass

# 9) Telegram client: long-poll and audio upload
from app.tg import Telegram  # noqa: E402
tgc = Telegram("t", 1)
assert tgc.updates(None) == [{"update_id": 7}]
assert tgc.send_audio(SONG, title="x", caption="y") is True

print("ALL SMOKE TESTS PASSED", "| tmp:", TMP)

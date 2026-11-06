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
last_claude = {}
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
        last_claude["body"] = kw["json"]
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
assert job["status"] == "review", job  # the owner's own song waits for a yes/no
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

# 3b) the owner's own song may exceed the automatic cap
job_u = db.add_job("user", raw_text="y" * 30)
pipe.produce(job_u)
assert db.get(job_u)["status"] == "review"

# 3c) image model fallback: first model "does not exist" -> next one works
from app import images  # noqa: E402
_orig = net.request
def _fake_img(method, url, **kw):
    if "images/generations" in url:
        if kw["json"]["model"] != "gpt-image-1.5":
            return Resp(status=400, js={"error": {"message": "The model does not exist"}})
        return Resp(js={"data": [{"b64_json": base64.b64encode(PNG.read_bytes()).decode()}]})
    return _orig(method, url, **kw)
net.request = _fake_img
images._working["model"] = ""
images.generate(cfg, "a cat", TMP / "x.png")
assert images._working["model"] == "gpt-image-1.5" and (TMP / "x.png").exists()
net.request = _orig
images._working["model"] = ""

# 3d) /rasm: rebuild a ready job's cover + video, keep the song
job_r = db.add_job("auto", theme="x | y")
db.update(job_r, status="ready", title="T", lyrics="[Verse]\nhi", style="s", image_prompt="a hill at dusk",
          yt_title="T", yt_description="d", yt_tags="[]", mp3_path=str(SONG))
from app.main import Bot  # noqa: E402
bot = Bot.__new__(Bot)
import threading as _th  # noqa: E402
bot.db, bot.pipeline, bot.produce_lock = db, pipe, _th.Lock()
bot.merge_lock, bot.merge_timer = _th.RLock(), None
class _TG:
    def send(self, *a, **k): sent.append(a)
bot.tg = _TG()
before = pipe.mureka_used_today()
bot.redo_images([job_r], 1)
assert db.get(job_r)["status"] == "ready" and Path(db.get(job_r)["mp4_path"]).exists()
assert pipe.mureka_used_today() == before  # no new paid song

# 3e) every visual style renders a valid 1080p video; "another version" keeps the song, changes style/cover
from app import video  # noqa: E402
for st in video.STYLES + ("plain",):
    out = TMP / f"style_{st}.mp4"
    video.make_video(PNG, SONG, out, style=st)
    pr = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                         "-of", "csv=p=0", str(out)], capture_output=True, text=True, check=True)
    assert pr.stdout.strip() == "1920,1080", (st, pr.stdout)
assert all(video.pick_style() in video.STYLES for _ in range(20))
assert video.pick_style(exclude="bars") != "bars"
old_mp3 = db.get(job_u)["mp3_path"]
runs_before = len([c for c in calls if c[1].endswith("/v1/song/generate")])
pipe.new_look(job_u)
j = db.get(job_u)
assert j["status"] == "review" and j["mp3_path"] == old_mp3 and "[variation 1" in j["image_prompt"], j
assert len([c for c in calls if c[1].endswith("/v1/song/generate")]) == runs_before  # no new paid song

# 3f) review message + button callbacks
vids = []
btns = []
class _TG2:
    admin_id = 1
    def send(self, *a, **k): sent.append(a)
    def send_audio(self, *a, **k): return True
    def send_buttons(self, text, buttons, chat_id=None): btns.append((text, buttons))
    def download_file(self, file_id, dest):
        import shutil
        shutil.copy(CLIP, dest)
    def send_video(self, path, caption="", buttons=None, chat_id=None):
        vids.append((path, buttons)); return True
    def answer_callback(self, *a, **k): pass
bot.tg = _TG2()
bot.cfg = cfg
bot.publish_lock = _th.Lock()
bot.send_review(job_u)
assert vids and [b[1] for b in vids[0][1]] == [f"post:{job_u}", f"redo:{job_u}", f"del:{job_u}"]
assert bot.latest_review() == job_u
bot.do_post(job_u, 1)
assert db.get(job_u)["status"] == "posted", db.get(job_u)["status"]

# 3g) owner sends a poem -> bot asks for the language -> chosen language reaches Claude
bot.wake = _th.Event()
bot.handle({"chat": {"id": 1}, "from": {"id": 1}, "text": "Qulab ketar ko'ksimga to'g'lar, darding olay"})
pend = [r for r in db.recent(5) if r["status"] == "pending"]
assert len(pend) == 1 and btns and any(b[1].startswith("lang:uz:") for b in btns[-1][1]), btns
pid = pend[0]["id"]
assert db.next_to_produce() is None or db.next_to_produce()["id"] != pid  # not produced before the choice
btns.clear()
bot.handle_callback({"id": "c1", "from": {"id": 1}, "message": {"chat": {"id": 1}}, "data": f"lang:uz:{pid}"})
assert db.get(pid)["status"] == "pending" and db.kv_get(f"lang:{pid}") == "uz"  # genre still to be chosen
assert btns and any(b[1] == f"gen:club:{pid}" for row in btns[-1][1] for b in row), btns
bot.handle_callback({"id": "c2", "from": {"id": 1}, "message": {"chat": {"id": 1}}, "data": f"gen:club:{pid}"})
assert db.get(pid)["status"] == "queued" and db.kv_get(f"genre:{pid}") == "club" and bot.wake.is_set()
bot.handle_callback({"id": "c3", "from": {"id": 1}, "message": {"chat": {"id": 1}}, "data": f"gen:trap:{pid}"})
assert db.kv_get(f"genre:{pid}") == "club"  # a second tap changes nothing
pipe.produce(pid)
prompt = last_claude["body"]["messages"][0]["content"]
assert "UZBEK" in prompt and "lyrics in UZBEK" in prompt and "NOT in English" in prompt
assert "club house" in prompt and "GENRE requested" in prompt and "CLUB STRUCTURE" in prompt
# English choice keeps the old behaviour
from app import claude_api as _ca  # noqa: E402
assert "ENGLISH song" in _ca.build_user_message("some poem text here", "", [], "en")
assert "lyrics in ENGLISH" in _ca.build_user_message("some poem text here", "", [], "en")
db.update(pid, status="failed")

# 3h) owner's looping background clips
from app import backgrounds  # noqa: E402
CLIP = TMP / "clip.mp4"
sh("ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=10", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(CLIP))
LONG = TMP / "long.mp3"
sh("ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=330:duration=25", "-c:a", "libmp3lame", str(LONG))
assert backgrounds.pick(cfg) is None  # empty library -> normal AI cover
bot.handle({"chat": {"id": 1}, "from": {"id": 1}, "video": {"file_id": "f1", "file_size": 3_000_000}})
assert not backgrounds.list_all(cfg)  # waits for Tayyor
bot.merge_finish(1)  # a single clip is stored as it is
assert len(backgrounds.list_all(cfg)) == 1, backgrounds.list_all(cfg)
bid, secs = backgrounds.add(cfg, CLIP)
assert bid == 2 and 8.5 < secs < 9.5, (bid, secs)  # 10 s clip -> 9 s seamless loop
bot.add_background({"file_id": "big", "file_size": 30_000_000}, 1)  # too big: refused politely
assert len(backgrounds.list_all(cfg)) == 2
assert backgrounds.pick(cfg, exclude=1)[0] == 2

# preview for big videos + video list + queue while YouTube is blocked
_pv = TMP / "preview_test.mp4"
video.make_preview(TMP / "style_pulse.mp4", _pv)
assert _pv.exists() and _pv.stat().st_size > 1000
_w = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width", "-of", "csv=p=0", str(_pv)],
                    capture_output=True, text=True).stdout.strip()
assert _w == "640", _w
_calls = []
class _TG2:
    def send(self, *a, **k): _calls.append(("send", a))
    def send_buttons(self, text, rows, chat_id=None): _calls.append(("buttons", text, rows))
    def send_video(self, path, caption="", buttons=None, chat_id=None): _calls.append(("video", str(path))); return True
    def answer_callback(self, *a, **k): pass
_old_tg = bot.tg; bot.tg = _TG2()
_jr = db.add_job("user", raw_text="q" * 30)
db.update(_jr, status="review", title="Preview Song", yt_title="T", mp4_path=str(TMP / "style_pulse.mp4"), mp3_path=str(SONG))
bot.list_videos(1)
assert any(c[0] == "buttons" and f"show:{_jr}" in str(c[2]) for c in _calls), _calls
bot.handle_callback({"id": "s1", "from": {"id": 1}, "message": {"chat": {"id": 1}}, "data": f"show:{_jr}"})
import time as _t2
for _ in range(50):
    if any(c[0] == "video" for c in _calls): break
    _t2.sleep(0.1)
assert any(c[0] == "video" for c in _calls)
db.kv_set("post_block_until", str(_t2.time() + 3600))
bot.do_post(_jr, 1)
assert db.get(_jr)["status"] == "ready"  # queued, not an error
db.kv_set("post_block_until", "0")
db.update(_jr, status="cancelled")
bot.tg = _old_tg

# collect mode: videos wait, Tayyor joins them into ONE background
n_before = len(backgrounds.list_all(cfg))
bot.handle({"chat": {"id": 1}, "from": {"id": 1}, "video": {"file_id": "m1", "file_size": 3_000_000}})
bot.handle({"chat": {"id": 1}, "from": {"id": 1}, "video": {"file_id": "m2", "file_size": 3_000_000}})
assert len(backgrounds.list_all(cfg)) == n_before  # nothing stored until Tayyor
bot.handle_callback({"id": "c1", "from": {"id": 1}, "message": {"chat": {"id": 1}}, "data": "mg:done"})
for _ in range(300):
    if bot.db.kv_get("merge_mode") == "0" and len(backgrounds.list_all(cfg)) > n_before and not bot.merge_dir().exists():
        break
    import time as _t; _t.sleep(0.2)
assert len(backgrounds.list_all(cfg)) == n_before + 1
mid, mpath = backgrounds.list_all(cfg)[-1]
assert video.probe_duration(mpath) > 15, video.probe_duration(mpath)  # ~2 x 10 s joined
assert bot.db.kv_get("merge_mode") == "0"
# existing backgrounds can be joined too
ids_now = [i for i, _ in backgrounds.list_all(cfg)]
bot.join_existing([1, 2], 1)
assert 1 not in dict(backgrounds.list_all(cfg)) and 2 not in dict(backgrounds.list_all(cfg))
assert len(backgrounds.list_all(cfg)) == len(ids_now) - 1  # two clips became one

job_bg = db.add_job("user", raw_text="z" * 30)
db.update(job_bg, lyrics="[Verse]\nhi", title="Samarkand Smile", style="s", yt_title="T", yt_description="d",
          yt_tags=json.dumps(["pop", "love song", "new song 2026", "lyric video", "feel good"]), mp3_path=str(LONG))
openai_before = len([c for c in calls if "api.openai.com" in c[1]])
pipe.produce(job_bg)
jb = db.get(job_bg)
assert jb["status"] == "review" and db.kv_get(f"bg:{job_bg}") in [str(i) for i, _ in backgrounds.list_all(cfg)]
assert len([c for c in calls if "api.openai.com" in c[1]]) == openai_before  # no paid image
assert db.kv_get(f"style:{job_bg}") in ("bars", "wave")
dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                            "default=nw=1:nk=1", jb["mp4_path"]], capture_output=True, text=True, check=True).stdout)
assert 24 < dur < 26, dur  # 9 s clip repeated until the 25 s song ends
first_bg = db.kv_get(f"bg:{job_bg}")
pipe.new_look(job_bg)
assert db.get(job_bg)["status"] == "review" and db.kv_get(f"bg:{job_bg}") != first_bg  # another clip next time
pipe.publish(job_bg)  # thumbnail with the title is rendered inside publish
assert (pipe.job_dir(job_bg) / "thumb.jpg").exists()
db.kv_set("bg_mode", "rasm")
job_img = db.add_job("user", raw_text="q" * 30)
db.update(job_img, lyrics="[Verse]\nhi", title="T2", style="s", image_prompt="a hill", yt_title="T", yt_description="d",
          yt_tags="[]", mp3_path=str(SONG))
pipe.produce(job_img)
assert len([c for c in calls if "api.openai.com" in c[1]]) == openai_before + 1  # picture mode draws a cover again
db.kv_set("bg_mode", "video")

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

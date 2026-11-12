"""Offline end-to-end test: every network call is faked, ffmpeg runs for real.
Run:  python3 -I tests/smoke_test.py   (from the project folder)
"""
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import claude_api, mureka, net, scheduler, youtube  # noqa: E402
from app.claude_api import GENRES as GENRES_ALL  # noqa: E402
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
    if "api.wavespeed.ai" in url:
        if "create-upload-id" in url:
            calls.append(("WS_UPLOAD", json.dumps(kw["json"])))
            return Resp(js={"code": 200, "data": {"id": "up1", "status": "created"}})
        if "/predictions/up1/" in url:
            return Resp(js={"code": 200, "data": {"id": "up1", "status": "completed",
                                                  "outputs": [{"melody_id": "mel-42", "purpose": "melody"}]}})
        if method == "POST":
            assert kw["headers"]["Authorization"] == "Bearer wk" and kw["json"]["lyrics"] and kw["json"].get("gender") in (None, "male", "female")
            calls.append(("WS_BODY", json.dumps(kw["json"])))
            return Resp(js={"code": 200, "data": {"id": "ws1", "status": "created"}})
        return Resp(js={"code": 200, "data": {"id": "ws1", "status": "completed", "outputs": ["https://cdn.example/ws1.mp3"]}})
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
    if url in ("https://cdn.example/song.mp3", "https://cdn.example/ws1.mp3"):
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
db.kv_set("bg_mode", "video")  # the older tests below are about clips / pictures; brand mode has its own block
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
assert video.pick_style(exclude="bars") == "pulse"
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
assert vids and [b[1] for b in vids[0][1][0]] == [f"post:{job_u}", f"redo:{job_u}", f"del:{job_u}"] and vids[0][1][1][0][1] == f"bgs:{job_u}"
assert bot.latest_review() == job_u
bot.do_post(job_u, 1)
assert db.get(job_u)["status"] == "posted", db.get(job_u)["status"]

# 3g) owner sends a poem -> bot asks for the language -> chosen language reaches Claude
bot.wake = _th.Event()
bot.handle({"chat": {"id": 1}, "from": {"id": 1}, "text": "Qulab ketar ko'ksimga to'g'lar, darding olay"})
pend = [r for r in db.recent(5) if r["status"] == "pending"]
assert len(pend) == 1 and btns and any(b[1].startswith("lang:uz:") for row in btns[-1][1] for b in row) and any(b[1].startswith("lang:tr:") for row in btns[-1][1] for b in row), btns
pid = pend[0]["id"]
assert db.next_to_produce() is None or db.next_to_produce()["id"] != pid  # not produced before the choice
btns.clear()
bot.handle_callback({"id": "c1", "from": {"id": 1}, "message": {"chat": {"id": 1}}, "data": f"lang:uz:{pid}"})
assert db.get(pid)["status"] == "pending" and db.kv_get(f"lang:{pid}") == "uz"  # genre still to be chosen
assert btns and any(b[1] == f"gen:club:{pid}" for row in btns[-1][1] for b in row), btns
btns.clear()
bot.handle_callback({"id": "c2", "from": {"id": 1}, "message": {"chat": {"id": 1}}, "data": f"gen:club:{pid}"})
assert db.get(pid)["status"] == "pending" and db.kv_get(f"genre:{pid}") == "club"  # voice still to be chosen
assert btns and any(b[1] == f"voc:male:{pid}" for row in btns[-1][1] for b in row), btns
bot.handle_callback({"id": "c2b", "from": {"id": 1}, "message": {"chat": {"id": 1}}, "data": f"voc:male:{pid}"})
assert db.get(pid)["status"] == "queued" and db.kv_get(f"voice:{pid}") == "male" and bot.wake.is_set()
bot.handle_callback({"id": "c3", "from": {"id": 1}, "message": {"chat": {"id": 1}}, "data": f"gen:trap:{pid}"})
assert db.kv_get(f"genre:{pid}") == "club"  # a second tap changes nothing
pipe.produce(pid)
prompt = last_claude["body"]["messages"][0]["content"]
# Uzbek text is sung exactly as the owner wrote it (Claude only packages it), so no genre restructuring either
assert "Uzbek-language vocals" in prompt and "EXACTLY as written" in prompt and "NOT in English" not in prompt
assert "club house" in prompt and "GENRE requested" in prompt and "CLUB STRUCTURE" not in prompt
assert "male vocal" in prompt and "VOCALS requested" in prompt
assert "male vocal" in db.get(pid)["style"] and "female" not in db.get(pid)["style"].lower().replace("male and female", "")
from app import claude_api as _ca  # noqa: E402
_tr = _ca.build_user_message("Qulab ketar ko'ksimga to'g'lar, darding olay", "", [], "tr")
assert "TURKISH" in _tr and "NOT in English" in _tr and "lyrics in TURKISH" in _tr and "Turkish-language vocals" in _tr
_tr_auto = _ca.build_user_message("", "club | night city", [], "tr")
assert "TURKISH" in _tr_auto and "Turkish-language vocals" in _tr_auto and "brand-new original song" in _tr_auto
cfg.auto_turkish_percent = 100
_aj = pipe.add_auto_job("pop | sunny day")
assert db.kv_get(f"lang:{_aj}") == "tr"
cfg.auto_turkish_percent = 0
_aj2 = pipe.add_auto_job("pop | rainy day")
assert db.kv_get(f"lang:{_aj2}", "") == ""
db.update(_aj, status="failed"); db.update(_aj2, status="failed")
# the owner's own English text is sung exactly as sent: Claude only packages it
from app import claude_api as _ca  # noqa: E402
_en = _ca.build_user_message("some poem text here", "", [], "en")
assert "EXACTLY as written" in _en and "ENGLISH song" not in _en
assert _ca._enforce_owner_lyrics("a b c", "[Verse]\na b c") == "[Verse]\na b c"   # only tags added: accepted
assert _ca._enforce_owner_lyrics("a b c", "[Verse]\nx y z") == "a b c"             # words changed: owner's text wins
# automatic songs (no owner text) are still written by Claude
assert "brand-new original song" in _ca.build_user_message("", "pop | rainy day", [], "en")
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

# export for a distributor
_ex = []
class _TG3:
    def send(self, *a, **k): _ex.append(("send", a))
    def send_document(self, path, caption="", chat_id=None): _ex.append(("doc", str(path))); return True
_old = bot.tg; bot.tg = _TG3()
_jx = db.add_job("user", raw_text="e" * 30)
db.update(_jx, title="Export Song", lyrics="[Verse]\nhello", mp3_path=str(SONG), image_path=str(PNG))
bot.export_job(_jx, 1)
assert sum(1 for k, _ in _ex if k == "doc") == 2 and any(k == "send" and "Export Song" in str(a) for k, a in _ex), _ex
_sq = pipe.job_dir(_jx) / "cover_3000.jpg"
_dim = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height", "-of", "csv=p=0", str(_sq)],
                      capture_output=True, text=True).stdout.strip()
assert _dim == "3000,3000", _dim
db.update(_jx, status="cancelled")
bot.tg = _old

# rotation: every clip gets a turn; a new clip goes first
_seen = []
for _ in range(len(backgrounds.list_all(cfg))):
    _seen.append(backgrounds.pick(cfg)[0])
assert sorted(_seen) == sorted(i for i, _ in backgrounds.list_all(cfg)), _seen
_new_id, _ = backgrounds.add(cfg, CLIP)
assert backgrounds.pick(cfg)[0] == _new_id
backgrounds.remove(cfg, _new_id)

# owner uploads a finished mp3: a menu first, then each of the three routes
class _TG5:
    def __init__(self): self.sent = []; self.rows = []
    def send(self, *a, **k): self.sent.append(a)
    def send_buttons(self, text, rows, chat_id=None): self.rows = rows; self.sent.append((text,))
    def answer_callback(self, *a, **k): pass
    def download_file(self, file_id, dest): shutil.copy(LONG, dest)
_old5 = bot.tg; bot.tg = _TG5(); bot.wake = _th.Event()
_mureka_calls = len([c for c in calls if "mureka" in c[1]])
bot.handle({"chat": {"id": 1}, "from": {"id": 1}, "audio": {"file_id": "a1", "file_size": 900_000, "title": "Night Ride"},
            "caption": "Night Ride\nclub house, dark and driving"})
for _ in range(150):
    if bot.tg.rows: break
    import time as _t5; _t5.sleep(0.1)
_actions = [b[1] for row in bot.tg.rows for b in row]
assert any(a.endswith(":video") for a in _actions) and any(a.endswith(":ai") for a in _actions) \
    and any(a.endswith(":dj_club") for a in _actions), _actions
_n = int(_actions[0].split(":")[1])
assert not [r for r in db.recent(5) if r["style"] == "__upload__"]  # nothing happens until he picks

# 1) plain video, as before: no song generation, no cost
bot.audio_action(_n, "video", 1)
_up = [r for r in db.recent(5) if r["style"] == "__upload__"]
assert _up and _up[0]["status"] == "queued" and _up[0]["title"] == "Night Ride", _up
pipe.produce(_up[0]["id"])
_uj = db.get(_up[0]["id"])
assert _uj["status"] == "review" and _uj["yt_title"] and _uj["mp4_path"], _uj["status"]
assert len([c for c in calls if "mureka" in c[1]]) == _mureka_calls
db.update(_uj["id"], status="failed")

# 2) free DJ edit: a new job with its own mp3, still no generator call
_dj_before = len([c for c in calls if "wavespeed" in c[1]])
_dj = pipe.add_dj_job(bot.pending_audio(_n), "club", "Night Ride")
assert _dj["bpm"] > 0 and db.get(_dj["job_id"])["style"] == "__upload__"
assert Path(db.get(_dj["job_id"])["mp3_path"]).exists()
assert len([c for c in calls if "wavespeed" in c[1]]) == _dj_before  # free
db.update(_dj["job_id"], status="failed")

# 3) AI remix: the source is uploaded once and its id rides along with the song request
import dataclasses as _dc2  # noqa: E402
_pipe_ws = type(pipe)(_dc2.replace(cfg, song_provider="wavespeed", wavespeed_key="wk"), db, lambda *a, **k: None)
_rx = _pipe_ws.add_remix_job(bot.pending_audio(_n), "melody", genre="club", language="en", raw_text="night drive")
assert db.kv_get(f"remix_kind:{_rx}") == "melody" and Path(db.kv_get(f"remix_src:{_rx}")).exists()
os.environ["PUBLIC_BASE_URL"] = "http://127.0.0.1:8111"
os.environ["PORT"] = "8111"
_pipe_ws.produce(_rx)
assert db.get(_rx)["status"] == "review", db.get(_rx)["error"]
assert json.loads([c for c in calls if c[0] == "WS_BODY"][-1][1])["melody_id"] == "mel-42"
assert db.kv_get(f"remix_upload:{_rx}") == "mel-42"
_ws_up = json.loads([c for c in calls if c[0] == "WS_UPLOAD"][-1][1])
assert _ws_up["purpose"] == "melody" and _ws_up["audio"].startswith("http://127.0.0.1:8111/f/")
db.update(_rx, status="failed")
bot.pending_audio(_n).unlink(missing_ok=True)
bot.tg = _old5

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
assert db.kv_get(f"style:{job_bg}") == "clip"
dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                            "default=nw=1:nk=1", jb["mp4_path"]], capture_output=True, text=True, check=True).stdout)
assert 24 < dur < 26, dur  # 9 s clip repeated until the 25 s song ends
first_bg = db.kv_get(f"bg:{job_bg}")
pipe.new_look(job_bg)
assert db.get(job_bg)["status"] == "review" and db.kv_get(f"bg:{job_bg}") != first_bg  # another clip next time
# the owner can pick the background clip himself
_ids = [i for i, _ in backgrounds.list_all(cfg)]
_target = next(i for i in _ids if str(i) != db.kv_get(f"bg:{job_bg}"))
_sent = []
_old_b = bot.tg
class _TG4:
    def send(self, *a, **k): _sent.append(("send", a))
    def send_buttons(self, text, rows, chat_id=None): _sent.append(("buttons", text, rows))
    def send_video(self, *a, **k): _sent.append(("video", a)); return True
    def answer_callback(self, *a, **k): pass
bot.tg = _TG4()
bot.handle_callback({"id": "b1", "from": {"id": 1}, "message": {"chat": {"id": 1}}, "data": f"bgs:{job_bg}"})
assert any(c[0] == "buttons" and f"bgp:{job_bg}:{_target}" in str(c[2]) for c in _sent), _sent
bot.do_redo(job_bg, 1, _target)
assert db.kv_get(f"bg:{job_bg}") == str(_target) and db.get(job_bg)["status"] == "review"
bot.tg = _old_b
pipe.publish(job_bg)  # thumbnail with the title is rendered inside publish
assert (pipe.job_dir(job_bg) / "thumb.jpg").exists()
db.kv_set("bg_mode", "rasm")
job_img = db.add_job("user", raw_text="q" * 30)
db.update(job_img, lyrics="[Verse]\nhi", title="T2", style="s", image_prompt="a hill", yt_title="T", yt_description="d",
          yt_tags="[]", mp3_path=str(SONG))
pipe.produce(job_img)
assert len([c for c in calls if "api.openai.com" in c[1]]) == openai_before + 1  # picture mode draws a cover again
db.kv_set("bg_mode", "video")

# brand mode: logo + circular spectrum on one of 11 themes, no paid picture
db.kv_set("bg_mode", "brand")
from app import brand as _brand  # noqa: E402
job_br = db.add_job("user", raw_text="b" * 30)
db.update(job_br, lyrics="[Verse]\nhi", title="Brand Song", style="s", yt_title="T", yt_description="d",
          yt_tags=json.dumps(["pop", "love song", "new song 2026", "lyric video", "feel good"]), mp3_path=str(SONG))
openai_n = len([c for c in calls if "api.openai.com" in c[1]])
_orig_render, _orig_cover = _brand.render, _brand.cover_frame
_brand.render = lambda audio, out, theme, title="", **k: _orig_render(audio, out, theme, title, size=(640, 360), max_seconds=3)
_brand.cover_frame = lambda out, theme, title="", **k: _orig_cover(out, theme, title, size=(640, 360))
pipe.produce(job_br)
assert db.get(job_br)["status"] == "review" and db.kv_get(f"style:{job_br}").startswith("brand")
assert db.kv_get(f"bg:{job_br}").startswith("b") and len([c for c in calls if "api.openai.com" in c[1]]) == openai_n
first_theme = db.kv_get(f"bg:{job_br}")
pipe.new_look(job_br)
assert db.kv_get(f"bg:{job_br}") != first_theme  # another theme
pipe.new_look(job_br, "b5")
assert db.kv_get(f"bg:{job_br}") == "b5" and db.kv_get(f"style:{job_br}") == "brand5"
for _n in range(1, _brand.THEME_COUNT + 1):  # every theme renders a still without errors
    _orig_cover(TMP / f"theme_{_n}.png", _n, "T", size=(320, 180))
_brand.render, _brand.cover_frame = _orig_render, _orig_cover
db.update(job_br, status="failed")
# WaveSpeed provider: same song, one request + polling, gender passed on
import dataclasses as _dc  # noqa: E402
_cfg_ws = _dc.replace(cfg, song_provider="wavespeed", wavespeed_key="wk")
_r = mureka.generate(_cfg_ws, "[Verse]\nhello", "deep male vocal, pop, 120 BPM")
assert _r["url"].endswith("ws1.mp3") and _r["task_id"] == "ws1"
assert json.loads([c for c in calls if c[0] == "WS_BODY"][-1][1])["gender"] == "male"
mureka.generate(_cfg_ws, "[Verse]\nhello", "female duet pop")
assert "gender" not in json.loads([c for c in calls if c[0] == "WS_BODY"][-1][1])
assert "WAVESPEED_API_KEY" in _dc.replace(cfg, song_provider="wavespeed", wavespeed_key="").missing()

# language choice beats "sing it verbatim": an Uzbek poem + English chosen must be rewritten in English
_uz_poem = "Yuragimda bir armon bor,\nKo'zlarimda yosh izlari,\nSeni kutib o'tdi yillar."
_en_poem = "I was walking through the city lights and I never thought that I would lose you tonight."
assert claude_api.detect_language(_uz_poem) == "uz" and claude_api.detect_language(_en_poem) == "en"
assert not claude_api.is_verbatim(_uz_poem, "en") and claude_api.is_verbatim(_uz_poem, "uz")
assert claude_api.is_verbatim(_en_poem, "en") and not claude_api.is_verbatim(_en_poem, "uz")
assert "Turn it into an ENGLISH song" in claude_api.build_user_message(_uz_poem, "", [], language="en")
assert "sung EXACTLY as written" in claude_api.build_user_message(_uz_poem, "", [], language="uz")
assert "sung EXACTLY as written" in claude_api.build_user_message(_en_poem, "", [], language="en")
_m = claude_api.build_user_message(_en_poem, "", [], language="uz")
assert "UZBEK" in _m and "sung EXACTLY as written" not in _m
assert "TURKISH" in claude_api.build_user_message(_uz_poem, "", [], language="tr")

# lo-fi and ambient: new calm genres, and the calm ones keep their intro
assert "lofi" in GENRES_ALL and "chill" in GENRES_ALL
assert claude_api.genre_key("lo-fi chillhop with vinyl crackle") == "lofi"
assert claude_api.genre_key("ambient downtempo, felt piano") == "chill"
_pkg = {"title": "T", "lyrics": "[Intro]\nmmm\n[Verse]\nhi", "style": "lo-fi chillhop, 75 BPM",
        "image_prompt": "x", "yt_title": "t", "yt_description": "d", "yt_tags": ["a"]}
assert "[Intro]" in claude_api._validate(dict(_pkg), "male", calm=True)["lyrics"]
assert "[Intro]" not in claude_api._validate(dict(_pkg), "male", calm=False)["lyrics"]
assert "vocals start within" not in claude_api._validate(dict(_pkg), "male", calm=True)["style"]

# photo mode: the owner's picture + weather effects (rain, tears, wind, snow)
from PIL import Image as _PI  # noqa: E402
from app import photofx as _pfx  # noqa: E402
_PI.new("RGB", (900, 1200), (90, 110, 150)).save(TMP / "photo.jpg")
import shutil as _sh  # noqa: E402
bot.merge_dir().mkdir(parents=True, exist_ok=True)
_sh.copy(TMP / "photo.jpg", bot.merge_dir() / "pending_99.img")
_np = len(backgrounds.list_photos(cfg))
bot.handle_callback({"id": "p1", "from": {"id": 1}, "message": {"chat": {"id": 1}}, "data": "pfx:99:snow"})
for _ in range(100):
    if len(backgrounds.list_photos(cfg)) > _np: break
    import time as _t6; _t6.sleep(0.2)
assert len(backgrounds.list_photos(cfg)) == _np + 1 and backgrounds.list_photos(cfg)[-1][2] == "snow"
pid = backgrounds.add_photo(cfg, TMP / "photo.jpg", "rain+wind+junk")
assert dict((i, f) for i, _, f in backgrounds.list_photos(cfg))[pid] == "rain+wind"
for _fx in _pfx.CHOICES:  # every effect draws frames without errors
    _sc = _pfx.Scene(TMP / "photo.jpg", _fx, "T", size=(320, 180), duration=10)
    for _t in (0.0, 3.3):
        _sc.frame(_t, 0.7)
job_ph = db.add_job("user", raw_text="p" * 30)
db.update(job_ph, lyrics="[Verse]\nhi", title="Rain Song", style="s", yt_title="T", yt_description="d",
          yt_tags=json.dumps(["pop", "love song", "new song 2026", "lyric video", "feel good"]), mp3_path=str(SONG))
openai_n = len([c for c in calls if "api.openai.com" in c[1]])
_orig_prender, _orig_pcover = _pfx.render, _pfx.cover_frame
_pfx.render = lambda audio, out, photo, fx, title="", **k: _orig_prender(audio, out, photo, fx, title, size=(640, 360), max_seconds=3)
_pfx.cover_frame = lambda out, photo, fx, title="", **k: _orig_pcover(out, photo, fx, title, size=(640, 360))
db.kv_set(f"bgforce:{job_ph}", f"p{pid}")
pipe.produce(job_ph)
assert db.get(job_ph)["status"] == "review" and db.kv_get(f"style:{job_ph}") == "photo:rain+wind", db.kv_get(f"style:{job_ph}")
assert db.kv_get(f"bg:{job_ph}") == f"p{pid}" and len([c for c in calls if "api.openai.com" in c[1]]) == openai_n
db.kv_set("bg_mode", "foto")
db.kv_set(f"bgforce:{job_ph}", "")
assert pipe._choose_look(job_ph)[0] == "photo"
_pfx.render, _pfx.cover_frame = _orig_prender, _orig_pcover
db.update(job_ph, status="failed")
assert backgrounds.remove_photo(cfg, pid) and pid not in [i for i, _, _ in backgrounds.list_photos(cfg)]
db.kv_set("bg_mode", "brand")
# long mix: many finished songs become one video, the picture changing with each track
import app.brand as _B, app.photofx as _PF, app.longmix as _LM  # noqa: E402
_mix_tracks = []
for _i in range(3):
    _p = TMP / f"mixsrc{_i}.mp3"
    shutil.copy(LONG, _p)
    _mix_tracks.append((f"Track {_i + 1}", _p))
_ob, _op, _ol = _B.render, _PF.render, _LM.LOOP_SECONDS
_B.render = lambda a, o, th, t="", **k: _ob(a, o, th, t, size=(320, 180), fps=8, max_seconds=k.get("max_seconds"))
_PF.render = lambda a, o, ph, fx, t="", **k: _op(a, o, ph, fx, t, size=(320, 180), fps=8, max_seconds=k.get("max_seconds"))
_LM.LOOP_SECONDS = 5
_mix_calls = len(calls)
_pid2 = backgrounds.add_photo(cfg, TMP / "photo.jpg", "snow")
_looks = pipe.mix_looks(limit=0)
assert len(_looks) == _B.THEME_COUNT + len(backgrounds.list_photos(cfg)), len(_looks)
assert len(pipe.mix_looks(limit=3)) == 3  # a mix does not draw 18 backgrounds before it can start
_info = pipe.add_mix_job(_mix_tracks, "Weekly Chill Mix")
_mj = db.get(_info["job_id"])
assert _mj["status"] == "review" and Path(_mj["mp4_path"]).exists() and Path(_mj["image_path"]).exists()
assert _info["tracks"] == 3 and _info["backgrounds"] >= 1
assert [c for c, _ in _info["chapters"]] == ["Track 1", "Track 2", "Track 3"]
assert _info["chapters"][0][1] == "0:00" and _info["chapters"][1][1] != "0:00"
_expect = sum(video.probe_duration(p) for _, p in _mix_tracks) - 2 * _LM.CROSSFADE
assert abs(_info["duration"] - _expect) < 1.5, (_info["duration"], _expect)
assert abs(video.probe_duration(Path(_mj["mp4_path"])) - _info["duration"]) < 1.5
assert "Tracklist:" in _mj["yt_description"] and "0:00 Track 1" in _mj["yt_description"]
assert len(calls) == _mix_calls  # a mix costs nothing: no generator, no Claude, no image API
assert pipe.mix_sources(days=7) and all(Path(t[1]).exists() for t in pipe.mix_sources(days=7))
assert not pipe.mix_sources(days=0)
# songs already used are skipped next time, and a mix never feeds another mix
_src = pipe.mix_sources(days=9999, reuse=True)
assert _info["job_id"] not in [t[2] for t in _src], "a mix must not be a source for the next one"
_pick = _src[:2]
_info2 = pipe.add_mix_job([(t[0], t[1], t[2]) for t in _pick], "Used Test")
_left = [t[2] for t in pipe.mix_sources(days=9999)]
assert all(t[2] not in _left for t in _pick), _left
assert all(t[2] in [x[2] for x in pipe.mix_sources(days=9999, reuse=True)] for t in _pick)
_c = pipe.mix_candidates(9999)
assert _c["used"] >= 2 and _c["with_used"] > _c["fresh"]
db.update(_info2["job_id"], status="failed")
# background loops are kept and reused, so the second mix does not redraw them
_cached = sorted(p.name for p in pipe.mix_loop_cache().glob("*.mp4"))
assert _cached, "loops must be cached for the next mix"
_stamps = {p.name: p.stat().st_mtime_ns for p in pipe.mix_loop_cache().glob("*.mp4")}
_info3 = pipe.add_mix_job([(t[0], t[1], t[2]) for t in pipe.mix_sources(days=9999, reuse=True)[:2]], "Reuse Test")
assert {p.name: p.stat().st_mtime_ns for p in pipe.mix_loop_cache().glob("*.mp4")}[_cached[0]] == _stamps[_cached[0]]
db.update(_info3["job_id"], status="failed")
db.update(_info["job_id"], status="failed")
backgrounds.remove_photo(cfg, _pid2)
_B.render, _PF.render, _LM.LOOP_SECONDS = _ob, _op, _ol

# automatic songs ignore the mode and always get a logo background
for _mode in ("video", "rasm", "aralash"):
    db.kv_set("bg_mode", _mode)
    _ja = db.add_job("auto", theme="pop | test")
    assert pipe._choose_look(_ja)[0] == "brand", _mode
    db.update(_ja, status="failed")
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

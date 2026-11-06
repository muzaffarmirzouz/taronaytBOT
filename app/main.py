"""Entry point: Telegram control bot + production worker + posting scheduler."""
from __future__ import annotations

import json
import logging
import sys
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from . import backgrounds, scheduler
from .config import Config
from .claude_api import GENRES
from .db import DB, STOCK_STATUSES
from .pipeline import BudgetExceeded, Pipeline
from .tg import Telegram

log = logging.getLogger("bot")

HELP = """🎵 Kunlik qo'shiq boti

Oddiy matn (sher/qo'shiq so'zlari) yuboring: bot tilni (🇺🇿 o'zbekcha / 🇬🇧 inglizcha) va janrni (klub, remix, afro, pop...) so'raydi, keyin navbatsiz darhol qo'shiq qiladi va videoni tasdiqlash uchun yuboradi.

/holat — navbat, keyingi joylash vaqti, bugungi sarf
/navbat — oxirgi 10 ta qo'shiq
/yarat [mavzu] — hoziroq yangi qo'shiq yaratish (mavzu ixtiyoriy)
/joyla [id] — tasdiqlanayotgan (yoki keyingi tayyor) videoni YouTube'ga joylash
/boshqa [id] — videoni boshqacha qilish (yangi rasm va uslub, qo'shiq o'sha)
/bekor <id> — qo'shiqni bekor qilish
/rasm — tayyor videolarga yangi rasm chizib, videoni qayta yig'ish
🎞 Fon videolar: botga 10–15 soniyalik videolarni yuboring (Telegram'da "video" qilib, fayl emas). Har qo'shiq uchun ulardan biri tasodifiy tanlanib, qo'shiq tugaguncha takrorlanadi; ustida qo'shiq nomi va vizualizatsiya bo'ladi.
/birlash — bir nechta videoni (masalan 2 ta 8 soniyalik) bitta fon qilish: avval shuni yozing, videolarni ketma-ket yuboring, so'ng /tayyor
/tayyor — yuborilgan videolarni birlashtirib, bitta fon sifatida saqlash
/fonlar — saqlangan fon videolar ro'yxati
/fon_ochir <id> — fon videoni o'chirish
/fon_rejim video|rasm — fon: sizning videolaringiz yoki AI rasm
/toxtat — avtomatik ishlashni to'xtatish (yaratish va joylash)
/davom — qayta yoqish"""


class Bot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.db = DB(cfg.data_dir / "bot.sqlite3")
        self.tg = Telegram(cfg.telegram_token, cfg.admin_id)
        self.pipeline = Pipeline(cfg, self.db, self.tg.send)
        self.tz = ZoneInfo(cfg.timezone)
        self.produce_lock = threading.Lock()
        self.wake = threading.Event()  # set when the owner sends a poem, so work starts at once
        self.publish_lock = threading.Lock()
        self.stop = threading.Event()
        scheduler.parse_times(cfg.post_times)  # fail fast on bad POST_TIMES

    # --- state --------------------------------------------------------
    def paused(self) -> bool:
        return self.db.kv_get("paused", "0") == "1"

    # --- worker: keeps the stock filled --------------------------------
    def worker_loop(self) -> None:
        while not self.stop.is_set():
            try:
                if not self.paused():
                    self.fill_stock()
                self.produce_next()
            except Exception:
                log.exception("worker loop error")
            self.wake.wait(15)
            self.wake.clear()

    def fill_stock(self) -> None:
        stock = self.db.count(*STOCK_STATUSES)
        if stock < self.cfg.stock_target and self.pipeline.mureka_used_today() < self.cfg.max_mureka_per_day:
            job_id = self.pipeline.add_auto_job()
            log.info("stock %s < %s: queued auto job %s", stock, self.cfg.stock_target, job_id)

    def produce_next(self) -> None:
        job = self.db.next_to_produce()
        if not job:
            return
        if self.paused() and job["source"] != "user":
            return  # pause stops automatic songs only; the owner's own songs still get made
        with self.produce_lock:
            try:
                self.pipeline.produce(job["id"])
                done = self.db.get(job["id"])
                if done["status"] == "review":
                    self.send_review(job["id"])
                else:
                    self.tg.send(f"✅ Tayyor #{job['id']}: {done['title']} (navbatda {self.db.count('ready')} ta video)")
                    self.tg.send_audio(done["mp3_path"], title=done["title"],
                                       caption=f"#{job['id']} {done['yt_title'] or done['title']}\n{done['style'][:300]}")
            except BudgetExceeded as exc:
                self.db.update(job["id"], status="queued")
                key = "budget_notified:" + self.pipeline._budget_key()
                if not self.db.kv_get(key):
                    self.db.kv_set(key, "1")
                    self.tg.send(f"⏸ Kunlik limit: {exc}. Ertaga davom etadi (MAX_MUREKA_PER_DAY ni o'zgartirish mumkin).")
                self.stop.wait(1800)
            except Exception as exc:
                log.exception("job %s failed", job["id"])
                attempts = (job.get("attempts") or 0) + 1
                final = attempts >= 3
                self.db.update(job["id"], status="failed" if final else "queued",
                               attempts=attempts, error=str(exc)[:500])
                self.tg.send(
                    f"⚠️ #{job['id']} xatolik ({attempts}/3): {str(exc)[:300]}"
                    + ("\n❌ 3 marta urinildi, bu qo'shiq to'xtatildi." if final else "\nQayta uriniladi.")
                )
                self.stop.wait(60 * attempts)

    # --- scheduler: posts at the set times -------------------------------
    def scheduler_loop(self) -> None:
        while not self.stop.is_set():
            try:
                if not self.paused():
                    self.check_slot()
            except Exception:
                log.exception("scheduler loop error")
            self.stop.wait(30)

    def check_slot(self) -> None:
        now = datetime.now(self.tz)
        done = set(json.loads(self.db.kv_get("done_slots", "[]")))
        key = scheduler.due_slot(now, self.cfg.post_times, done)
        if not key:
            return
        job = self.db.next_ready()
        if not job:
            if self.db.kv_get("no_video_notified") != key:
                self.db.kv_set("no_video_notified", key)
                self.tg.send(f"⏳ {key} uchun tayyor video yo'q. Tayyor bo'lishi bilan joylanadi (3 soat ichida).")
            return
        if self.post_job(job["id"]):
            done.add(key)
            self.db.kv_set("done_slots", json.dumps(sorted(done)[-30:]))

    # --- owner review: video + [Post] [Another version] [Cancel] --------
    def send_review(self, job_id: int) -> None:
        job = self.db.get(job_id)
        caption = (f"🎬 #{job_id} {job['title']}\n{job['yt_title']}\n\n"
                   f"Yoqdimi? YouTube'ga joylaymi?")
        buttons = [("✅ Joylash", f"post:{job_id}"), ("🔄 Boshqa qil", f"redo:{job_id}"),
                   ("❌ Bekor", f"del:{job_id}")]
        if not self.tg.send_video(job["mp4_path"], caption, buttons):
            # video too big / upload failed: fall back to audio + text commands
            self.tg.send_audio(job["mp3_path"], title=job["title"], caption=caption)
            self.tg.send(f"Video yuborilmadi (hajmi katta bo'lishi mumkin).\n"
                         f"/joyla {job_id} — joylash · /boshqa {job_id} — boshqacha qilish · /bekor {job_id}")

    def genre_rows(self, job_id: int) -> list:
        items = [(label, f"gen:{key}:{job_id}") for key, (label, _) in GENRES.items()]
        rows = [items[i:i + 2] for i in range(0, len(items), 2)]
        rows.append([("❌ Bekor", f"del:{job_id}")])
        return rows

    def latest_review(self) -> int | None:
        rows = [r for r in self.db.recent(50) if r["status"] == "review"]
        return rows[0]["id"] if rows else None

    def do_post(self, job_id: int, chat_id: int) -> None:
        job = self.db.get(job_id)
        if not job:
            self.tg.send(f"#{job_id} topilmadi.", chat_id)
        elif job["status"] == "posted":
            self.tg.send(f"#{job_id} allaqachon joylangan: https://youtu.be/{job['youtube_id']}", chat_id)
        elif job["status"] not in ("review", "ready"):
            self.tg.send(f"#{job_id} hozir joylab bo'lmaydi (holati: {job['status']}).", chat_id)
        else:
            self.tg.send(f"📤 Joylanmoqda: #{job_id} {job['title']}", chat_id)
            if not self.post_job(job_id):
                self.tg.send("Boshqa joylash davom etyapti, birozdan keyin qayta urining.", chat_id)

    def do_redo(self, job_id: int, chat_id: int) -> None:
        job = self.db.get(job_id)
        if not job or job["status"] not in ("review", "ready"):
            self.tg.send(f"#{job_id} ni boshqacha qilib bo'lmaydi.", chat_id)
            return
        back = job["status"]
        self.tg.send(f"🔄 #{job_id}: yangi rasm va uslub bilan qayta yig'ilmoqda (1–3 daqiqa)...", chat_id)
        with self.produce_lock:
            try:
                self.pipeline.new_look(job_id)
            except Exception as exc:
                log.exception("redo failed")
                self.db.update(job_id, status=back)
                self.tg.send(f"⚠️ Qayta yig'ishda xatolik: {str(exc)[:300]}", chat_id)
                return
        self.send_review(job_id)

    def do_cancel(self, job_id: int, chat_id: int) -> None:
        job = self.db.get(job_id)
        if job and job["status"] in ("pending", "queued", "ready", "review"):
            self.db.update(job_id, status="failed", error="bekor qilindi")
            self.tg.send(f"#{job_id} bekor qilindi.", chat_id)
        else:
            self.tg.send("Foydalanish: /bekor <id> (faqat navbatdagi, tayyor yoki tasdiq kutayotgan qo'shiq).", chat_id)

    def handle_callback(self, cb: dict) -> None:
        chat_id = cb["message"]["chat"]["id"]
        if cb.get("from", {}).get("id") != self.cfg.admin_id:
            self.tg.answer_callback(cb["id"], "Bu bot yopiq.")
            return
        data = cb.get("data") or ""
        if data.startswith("lang:"):
            _, lang, raw_id = (data.split(":") + ["", ""])[:3]
            job = self.db.get(int(raw_id)) if raw_id.isdigit() and lang in ("uz", "en") else None
            if not job or job["status"] != "pending":
                self.tg.answer_callback(cb["id"], "Bu allaqachon boshlangan yoki bekor qilingan.")
                return
            self.db.kv_set(f"lang:{job['id']}", lang)
            name = "o'zbekcha" if lang == "uz" else "inglizcha"
            self.tg.answer_callback(cb["id"], f"Til: {name}")
            self.tg.send_buttons(f"🎼 #{job['id']} ({name}). Qaysi janrda bo'lsin?", self.genre_rows(job["id"]), chat_id)
            return
        if data.startswith("gen:"):
            _, key, raw_id = (data.split(":") + ["", ""])[:3]
            job = self.db.get(int(raw_id)) if raw_id.isdigit() and key in GENRES else None
            if not job or job["status"] != "pending":
                self.tg.answer_callback(cb["id"], "Bu allaqachon boshlangan yoki bekor qilingan.")
                return
            self.db.kv_set(f"genre:{job['id']}", key)
            self.db.update(job["id"], status="queued")
            self.wake.set()
            self.tg.answer_callback(cb["id"], "Boshlandi")
            self.tg.send(f"🎶 #{job['id']} tayyorlanmoqda: {GENRES[key][0]}. Tayyor bo'lganda videoni yuboraman "
                         f"va joylash-joylamasligini so'rayman (2–5 daqiqa).", chat_id)
            return
        action, _, raw = data.partition(":")
        if not raw.isdigit():
            self.tg.answer_callback(cb["id"])
            return
        job_id = int(raw)
        self.tg.answer_callback(cb["id"], {"post": "Joylanmoqda...", "redo": "Qayta yig'ilmoqda...",
                                           "del": "Bekor qilindi"}.get(action, ""))
        target = {"post": self.do_post, "redo": self.do_redo, "del": self.do_cancel}.get(action)
        if target:
            threading.Thread(target=target, args=(job_id, chat_id), daemon=True).start()

    def post_job(self, job_id: int) -> bool:
        if not self.publish_lock.acquire(blocking=False):
            return False
        try:
            video_id = self.pipeline.publish(job_id)
            title = self.db.get(job_id)["title"]
            self.tg.send(f"🚀 YouTube'ga joylandi: {title}\nhttps://youtu.be/{video_id}")
            return True
        except Exception as exc:
            log.exception("publish failed")
            self.tg.send(f"⚠️ Joylashda xatolik: {str(exc)[:400]}")
            return False
        finally:
            self.publish_lock.release()

    # --- telegram -------------------------------------------------------
    def status_text(self) -> str:
        db = self.db
        nxt = scheduler.next_slot(datetime.now(self.tz), self.cfg.post_times)
        return (
            f"📊 Holat {'(⏸ to‘xtatilgan)' if self.paused() else '(▶️ ishlayapti)'}\n"
            f"Tayyor video: {db.count('ready')}\n"
            f"Tasdiq kutayotgan (sizniki): {db.count('review')}\n"
            f"Til tanlashni kutayotgan: {db.count('pending')}\n"
            f"Fon videolar: {len(backgrounds.list_all(self.cfg))} ta (rejim: {db.kv_get('bg_mode', 'video')})\n"
            f"Tayyorlanmoqda/navbatda: {db.count('queued', 'producing')}\n"
            f"Joylangan: {db.count('posted')} · Xato: {db.count('failed')}\n"
            f"Keyingi joylash: {nxt.strftime('%Y-%m-%d %H:%M')} ({self.cfg.timezone})\n"
            f"Joylash vaqtlari: {', '.join(self.cfg.post_times)}\n"
            f"Bugungi Mureka so'rovlari: {self.pipeline.mureka_used_today()}/{self.cfg.max_mureka_per_day}"
        )

    def queue_text(self) -> str:
        rows = self.db.recent(10)
        if not rows:
            return "Hozircha qo'shiqlar yo'q."
        icons = {"queued": "🕓", "producing": "⚙️", "ready": "✅", "posting": "📤", "posted": "🚀", "failed": "❌"}
        return "\n".join(
            f"{icons.get(r['status'], '•')} #{r['id']} [{r['source']}] {r['status']} — {r['title'] or '(matn kutilmoqda)'}"
            for r in rows
        )

    def add_background(self, media: dict, chat_id: int) -> None:
        """Owner sent a clip: turn it into a seamless loop and keep it in the library."""
        import tempfile
        from pathlib import Path
        if int(media.get("file_size") or 0) > 20 * 1024 * 1024:
            self.tg.send("⚠️ Video 20 MB dan katta: Telegram botlarga shundan kattasini bermaydi. "
                         "Uni Telegram'da \"fayl\" emas, oddiy \"video\" qilib yuboring (Telegram o'zi siqadi) "
                         "yoki qisqaroq/siqilgan qilib qayta yuboring.", chat_id)
            return
        try:
            with tempfile.TemporaryDirectory() as tmp:
                src = Path(tmp) / "upload.mp4"
                self.tg.download_file(media["file_id"], src)
                bg_id, seconds = backgrounds.add(self.cfg, src)
            total = len(backgrounds.list_all(self.cfg))
            self.tg.send(f"✅ Fon #{bg_id} qo'shildi ({seconds:.0f} soniya, takrorlanishi silliq qilindi). "
                         f"Jami fon videolar: {total} ta.", chat_id)
        except ValueError as exc:
            self.tg.send(f"⚠️ {exc}", chat_id)
        except Exception as exc:
            log.exception("background upload failed")
            self.tg.send(f"⚠️ Videoni qabul qilib bo'lmadi: {str(exc)[:300]}", chat_id)

    def merge_dir(self):
        d = self.cfg.data_dir / "merge_tmp"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def merge_collect(self, media: dict, chat_id: int) -> None:
        if int(media.get("file_size") or 0) > 20 * 1024 * 1024:
            self.tg.send("⚠️ Video 20 MB dan katta. Oddiy \"video\" qilib yuboring.", chat_id)
            return
        n = len(list(self.merge_dir().glob("part_*.mp4"))) + 1
        try:
            self.tg.download_file(media["file_id"], self.merge_dir() / f"part_{n:02d}.mp4")
            self.tg.send(f"➕ {n}-video qabul qilindi. Yana yuboring yoki /tayyor deb yozing.", chat_id)
        except Exception as exc:
            log.exception("merge download failed")
            self.tg.send(f"⚠️ Videoni olib bo'lmadi: {str(exc)[:300]}", chat_id)

    def merge_finish(self, chat_id: int) -> None:
        import shutil
        parts = sorted(self.merge_dir().glob("part_*.mp4"))
        self.db.kv_set("merge_mode", "0")
        if len(parts) < 2:
            self.tg.send("⚠️ Birlashtirish uchun kamida 2 ta video kerak. /birlash dan qayta boshlang.", chat_id)
            shutil.rmtree(self.merge_dir(), ignore_errors=True)
            return
        self.tg.send(f"⏳ {len(parts)} ta video birlashtirilmoqda...", chat_id)
        try:
            bg_id, seconds = backgrounds.add_many(self.cfg, parts)
            total = len(backgrounds.list_all(self.cfg))
            self.tg.send(f"✅ Fon #{bg_id} tayyor ({seconds:.0f} soniya, {len(parts)} ta videodan). "
                         f"Jami fon videolar: {total} ta.", chat_id)
        except Exception as exc:
            log.exception("merge failed")
            self.tg.send(f"⚠️ Birlashtirib bo'lmadi: {str(exc)[:300]}", chat_id)
        finally:
            shutil.rmtree(self.merge_dir(), ignore_errors=True)

    def redo_images(self, ids: list, chat_id: int) -> None:
        """Draw a fresh cover for already-finished songs and rebuild their videos (the song is kept)."""
        done = 0
        for job_id in ids:
            with self.produce_lock:
                job = self.db.get(job_id)
                if not job or job["status"] != "ready":
                    continue
                self.db.update(job_id, status="producing", image_path="", mp4_path="")
                try:
                    self.pipeline.produce(job_id)
                    done += 1
                except Exception as exc:
                    log.exception("redo image failed for %s", job_id)
                    self.db.update(job_id, status="queued", error=str(exc)[:300])
        self.tg.send(f"🎨 Tayyor: {done}/{len(ids)} ta videoning rasmi yangilandi.", chat_id)

    def handle(self, msg: dict) -> None:
        chat_id = msg["chat"]["id"]
        if msg.get("from", {}).get("id") != self.cfg.admin_id:
            self.tg.send("Bu bot yopiq.", chat_id)
            return
        doc = msg.get("document") or {}
        media = msg.get("video") or msg.get("animation") or (doc if str(doc.get("mime_type", "")).startswith("video/") else None)
        if media and self.db.kv_get("merge_mode", "0") == "1":
            self.merge_collect(media, chat_id)
            return
        if media:
            threading.Thread(target=self.add_background, args=(media, chat_id), daemon=True).start()
            return
        text = (msg.get("text") or "").strip()
        if not text:
            self.tg.send("Matn yuboring (sher yoki qo'shiq so'zlari) yoki fon uchun video yuboring.", chat_id)
            return
        if text.startswith("/"):
            cmd, _, arg = text.partition(" ")
            cmd = cmd.split("@")[0].lower()
            arg = arg.strip()
            if cmd in ("/start", "/help"):
                self.tg.send(HELP, chat_id)
            elif cmd == "/birlash":
                import shutil
                shutil.rmtree(self.merge_dir(), ignore_errors=True)
                self.db.kv_set("merge_mode", "1")
                self.tg.send("🎞 Birlashtirish rejimi yoqildi. Videolarni ketma-ket yuboring (\"video\" qilib), "
                             "tugagach /tayyor deb yozing.", chat_id)
            elif cmd == "/tayyor":
                if self.db.kv_get("merge_mode", "0") == "1":
                    threading.Thread(target=self.merge_finish, args=(chat_id,), daemon=True).start()
                else:
                    self.tg.send("Avval /birlash deb yozing, so'ng videolarni yuboring.", chat_id)
            elif cmd == "/holat":
                self.tg.send(self.status_text(), chat_id)
            elif cmd == "/navbat":
                self.tg.send(self.queue_text(), chat_id)
            elif cmd == "/yarat":
                job_id = self.pipeline.add_auto_job(arg and f"{arg} | {arg}")
                self.tg.send(f"Navbatga qo'shildi #{job_id}. Tayyor bo'lganda xabar beraman.", chat_id)
            elif cmd == "/joyla":
                target = int(arg) if arg.isdigit() else (self.latest_review() or (self.db.next_ready() or {}).get("id"))
                if not target:
                    self.tg.send("Tayyor video yo'q.", chat_id)
                else:
                    threading.Thread(target=self.do_post, args=(target, chat_id), daemon=True).start()
            elif cmd == "/boshqa":
                target = int(arg) if arg.isdigit() else self.latest_review()
                if not target:
                    self.tg.send("Tasdiq kutayotgan video yo'q. Foydalanish: /boshqa <id>", chat_id)
                else:
                    threading.Thread(target=self.do_redo, args=(target, chat_id), daemon=True).start()
            elif cmd == "/bekor":
                if arg.isdigit():
                    self.do_cancel(int(arg), chat_id)
                else:
                    self.tg.send("Foydalanish: /bekor <id>", chat_id)
            elif cmd == "/fonlar":
                items = backgrounds.list_all(self.cfg)
                mode = self.db.kv_get("bg_mode", "video")
                if not items:
                    self.tg.send("Fon video yo'q. 10–15 soniyalik videolarni shu yerga yuboring.", chat_id)
                else:
                    lines = [f"#{i} ({path.stat().st_size / 1e6:.1f} MB)" for i, path in items]
                    self.tg.send(f"🎞 Fon videolar: {len(items)} ta (rejim: {mode})\n" + "\n".join(lines)
                                 + "\n\nO'chirish: /fon_ochir <id>", chat_id)
            elif cmd == "/fon_ochir":
                ok = arg.isdigit() and backgrounds.remove(self.cfg, int(arg))
                self.tg.send(f"🗑 Fon #{arg} o'chirildi." if ok else "Topilmadi. Foydalanish: /fon_ochir <id>", chat_id)
            elif cmd == "/fon_rejim":
                if arg in ("video", "rasm"):
                    self.db.kv_set("bg_mode", arg)
                    self.tg.send("Fon: sizning videolaringiz 🎞" if arg == "video"
                                 else "Fon: AI chizgan rasm 🖼", chat_id)
                else:
                    self.tg.send("Foydalanish: /fon_rejim video  yoki  /fon_rejim rasm", chat_id)
            elif cmd == "/rasm":
                ids = [r["id"] for r in self.db.recent(200) if r["status"] == "ready"]
                if not ids:
                    self.tg.send("Tayyor video yo'q.", chat_id)
                else:
                    self.tg.send(f"🎨 {len(ids)} ta videoning rasmi qayta chizilmoqda. Tugagach xabar beraman.", chat_id)
                    threading.Thread(target=self.redo_images, args=(ids, chat_id), daemon=True).start()
            elif cmd == "/toxtat":
                self.db.kv_set("paused", "1")
                self.tg.send("⏸ To'xtatildi. /davom bilan qayta yoqiladi.", chat_id)
            elif cmd == "/davom":
                self.db.kv_set("paused", "0")
                self.tg.send("▶️ Davom etmoqda.", chat_id)
            else:
                self.tg.send(HELP, chat_id)
            return
        if len(text) < 20:
            self.tg.send("Matn juda qisqa. Sher yoki qo'shiq so'zlarini to'liq yuboring (kamida 20 belgi).", chat_id)
            return
        job_id = self.db.add_job("user", raw_text=text, status="pending")  # waits for the language choice
        self.tg.send_buttons(
            f"🎤 Matn qabul qilindi #{job_id}. Qo'shiq qaysi tilda bo'lsin?",
            [("🇺🇿 O'zbekcha", f"lang:uz:{job_id}"), ("🇬🇧 Inglizcha", f"lang:en:{job_id}"),
             ("❌ Bekor", f"del:{job_id}")], chat_id)

    def poll_loop(self) -> None:
        offset = None
        while not self.stop.is_set():
            try:
                for upd in self.tg.updates(offset):
                    offset = upd["update_id"] + 1
                    try:
                        if "message" in upd:
                            self.handle(upd["message"])
                        elif "callback_query" in upd:
                            self.handle_callback(upd["callback_query"])
                    except Exception:
                        log.exception("handler error")
            except Exception:
                log.exception("polling error")
                self.stop.wait(5)

    def run(self) -> None:
        threading.Thread(target=self.worker_loop, daemon=True, name="worker").start()
        threading.Thread(target=self.scheduler_loop, daemon=True, name="scheduler").start()
        self.tg.send(f"🤖 Bot ishga tushdi. Joylash vaqtlari ({self.cfg.timezone}): {', '.join(self.cfg.post_times)}\n/help")
        self.poll_loop()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = Config.from_env()
    missing = cfg.missing()
    if missing:
        print("Missing environment variables: " + ", ".join(missing), file=sys.stderr)
        sys.exit(1)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    Bot(cfg).run()


if __name__ == "__main__":
    main()

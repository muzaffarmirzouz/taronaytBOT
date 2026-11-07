"""Entry point: Telegram control bot + production worker + posting scheduler."""
from __future__ import annotations

import json
import logging
import sys
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from . import backgrounds, brand, scheduler, video
from .config import Config
from .claude_api import GENRES, VOICES
from .db import DB, STOCK_STATUSES
from .pipeline import BudgetExceeded, Pipeline
from .tg import Telegram

log = logging.getLogger("bot")

HELP = """🎵 Kunlik qo'shiq boti

Oddiy matn (sher/qo'shiq so'zlari) yuboring: bot tilni (🇺🇿 o'zbekcha / 🇬🇧 inglizcha) va janrni (klub, remix, afro, pop...) so'raydi, keyin navbatsiz darhol qo'shiq qiladi va videoni tasdiqlash uchun yuboradi.

/holat — navbat, keyingi joylash vaqti, bugungi sarf
/navbat — oxirgi 10 ta qo'shiq
/eksport <id> — Spotify distribyutori uchun: mp3, kvadrat muqova, matn
Tayyor mp3 yuborsangiz (izoh bilan: nomi, janr) — bot muqova+video qilib, tasdiqlash uchun yuboradi
/videolar — tayyor/tasdiq kutayotgan videolar ro'yxati; bosib ko'rish (katta videolar past sifatda ko'rsatiladi)
/yarat [mavzu] — hoziroq yangi qo'shiq yaratish (mavzu ixtiyoriy)
/joyla [id] — tasdiqlanayotgan (yoki keyingi tayyor) videoni YouTube'ga joylash
/boshqa [id] — videoni boshqacha qilish (yangi rasm va uslub, qo'shiq o'sha)
/bekor <id> — qo'shiqni bekor qilish
/rasm — tayyor videolarga yangi rasm chizib, videoni qayta yig'ish
🎞 Fon videolar: botga 10–15 soniyalik videolarni yuboring (Telegram'da "video" qilib, fayl emas). Har qo'shiq uchun ulardan biri tasodifiy tanlanib, qo'shiq tugaguncha takrorlanadi; ustida qo'shiq nomi va vizualizatsiya bo'ladi.
/tayyor — yuborilgan videolarni bitta fon qilib saqlash (✅ Tayyor tugmasi bilan bir xil)
/fon_qosh 1 2 — allaqachon saqlangan fonlarni bitta qilib birlashtirish
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
        self.merge_lock = threading.RLock()
        self.merge_timer = None
        self.post_busy = False
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
        if time.time() < float(self.db.kv_get("post_block_until", "0") or 0):
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
    def show_video(self, job: dict, caption: str, buttons: list, chat_id: int | None = None) -> bool:
        """Show a video in Telegram. Big files are sent as a small low-quality preview; YouTube still gets the full file."""
        from pathlib import Path
        path = Path(job["mp4_path"] or "")
        if not path.exists():
            return False
        if path.stat().st_size > 45 * 1024 * 1024:
            prev = self.pipeline.job_dir(job["id"]) / "preview.mp4"
            try:
                if not prev.exists() or prev.stat().st_mtime < path.stat().st_mtime:
                    video.make_preview(path, prev)
                path = prev
                caption += "\n(ko'rsatish uchun past sifat; YouTube'ga to'liq sifatda ketadi)"
            except Exception:
                log.exception("preview failed")
                return False
        return self.tg.send_video(path, caption, buttons, chat_id)

    def send_review(self, job_id: int) -> None:
        job = self.db.get(job_id)
        caption = (f"🎬 #{job_id} {job['title']}\n{job['yt_title']}\n\n"
                   f"Yoqdimi? YouTube'ga joylaymi?")
        buttons = [[("✅ Joylash", f"post:{job_id}"), ("🔄 Boshqa qil", f"redo:{job_id}"), ("❌ Bekor", f"del:{job_id}")],
                   [("🎞 Fon tanlash", f"bgs:{job_id}")]]
        if not self.show_video(job, caption, buttons):
            # video too big / upload failed: fall back to audio + text commands
            self.tg.send_audio(job["mp3_path"], title=job["title"], caption=caption)
            self.tg.send(f"Video yuborilmadi (hajmi katta bo'lishi mumkin).\n"
                         f"/joyla {job_id} — joylash · /boshqa {job_id} — boshqacha qilish · /bekor {job_id}")

    def list_videos(self, chat_id: int) -> None:
        rows = [r for r in self.db.recent(60) if r["status"] in ("ready", "review") and r["mp4_path"]]
        if not rows:
            self.tg.send("Tayyor video yo'q. /yarat bilan yangisini tayyorlang.", chat_id)
            return
        blocked = time.time() < float(self.db.kv_get("post_block_until", "0") or 0)
        head = "🎬 Tayyor videolar (bosib ko'ring)" + (" — YouTube limiti hozir yopiq, videolar saqlanadi" if blocked else "")
        icons = {"ready": "⏳", "review": "👀"}
        self.tg.send_buttons(head, [[(f"{icons[r['status']]} #{r['id']} {r['title'][:40]}", f"show:{r['id']}")] for r in rows[:20]], chat_id)

    def export_job(self, job_id: int, chat_id: int) -> None:
        """Everything a distributor (DistroKid etc.) asks for: mastered mp3, square 3000x3000 cover, title, genre, lyrics."""
        from pathlib import Path
        job = self.db.get(job_id)
        if not job or not job["mp3_path"] or not Path(job["mp3_path"]).exists():
            self.tg.send(f"#{job_id} topilmadi yoki qo'shiq fayli yo'q.", chat_id)
            return
        cover_src = Path(job["image_path"] or "")
        sent_cover = False
        if cover_src.exists():
            try:
                sq = self.pipeline.job_dir(job_id) / "cover_3000.jpg"
                video.make_square_cover(cover_src, sq)
                sent_cover = self.tg.send_document(sq, f"🖼 #{job_id} kvadrat muqova 3000×3000", chat_id)
            except Exception:
                log.exception("square cover failed")
        mp3_ok = self.tg.send_document(job["mp3_path"], f"🎵 #{job_id} {job['title']} (mp3, mastering qilingan)", chat_id)
        genre = self.db.kv_get(f"genre:{job_id}", "") or "—"
        self.tg.send(
            f"📦 Distribyutor uchun #{job_id}\n"
            f"Nomi: {job['title']}\n"
            f"Janr (taklif): {GENRES[genre][0] if genre in GENRES else genre}\n"
            f"Til: {({'uz': 'o‘zbekcha', 'tr': 'turkcha'}).get(self.db.kv_get(f'lang:{job_id}', 'en'), 'inglizcha')}\n"
            f"SI ishtirokini belgilang: vokal, cholg'u, matn — SI yordamida.\n"
            f"Fayllar: mp3 {'✅' if mp3_ok else '⚠️ yuborilmadi'} · muqova {'✅' if sent_cover else '⚠️ yuborilmadi'}\n\n"
            f"Matn:\n{job['lyrics'][:3000]}", chat_id)

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
        elif time.time() < float(self.db.kv_get("post_block_until", "0") or 0) and job["status"] == "review":
            self.db.update(job_id, status="ready")
            self.tg.send(f"📥 #{job_id} «{job['title']}» navbatga qo'yildi. YouTube limiti ochilgach jadval bo'yicha o'zi joylanadi "
                         f"(yoki /joyla {job_id}).", chat_id)
        else:
            self.tg.send(f"📤 Joylanmoqda: #{job_id} {job['title']}", chat_id)
            self.post_busy = False
            if not self.post_job(job_id) and self.post_busy:
                self.tg.send("Boshqa joylash davom etyapti, birozdan keyin qayta urining.", chat_id)

    def do_redo(self, job_id: int, chat_id: int, bg_id=None) -> None:
        job = self.db.get(job_id)
        if not job or job["status"] not in ("review", "ready"):
            self.tg.send(f"#{job_id} ni boshqacha qilib bo'lmaydi.", chat_id)
            return
        back = job["status"]
        self.tg.send(f"🔄 #{job_id}: " + ((f"🎨 {bg_id[1:]}-fon bilan" if str(bg_id).startswith("b") else f"fon #{bg_id} bilan") if bg_id else "yangi fon va uslub bilan")
                     + " qayta yig'ilmoqda (1–3 daqiqa)...", chat_id)
        with self.produce_lock:
            try:
                self.pipeline.new_look(job_id, bg_id)
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
        if data in ("mg:done", "mg:cancel"):
            self.tg.answer_callback(cb["id"], "Ok")
            target = self.merge_finish if data == "mg:done" else self.merge_cancel
            threading.Thread(target=target, args=(chat_id,), daemon=True).start()
            return
        if data.startswith("bgs:") or data.startswith("bgp:"):
            parts = data.split(":")
            job = self.db.get(int(parts[1])) if len(parts) > 1 and parts[1].isdigit() else None
            if not job or job["status"] not in ("review", "ready"):
                self.tg.answer_callback(cb["id"], "Bu videoni endi o'zgartirib bo'lmaydi.")
                return
            items = backgrounds.list_all(self.cfg)
            if parts[0] == "bgs":
                self.tg.answer_callback(cb["id"], "Fonlar")
                now = self.db.kv_get(f"bg:{job['id']}", "")
                rows, row = [], []
                for n in range(1, brand.THEME_COUNT + 1):  # logo + circular spectrum on animated themes
                    row.append((("✔ " if now == f"b{n}" else "") + f"🎨 {n}", f"bgp:{job['id']}:b{n}"))
                    if len(row) == 4:
                        rows.append(row); row = []
                if row:
                    rows.append(row)
                row = []
                for i, _ in items:  # the owner's own looping clips
                    row.append((("✔ " if str(i) == now else "") + f"🎞 #{i}", f"bgp:{job['id']}:{i}"))
                    if len(row) == 4:
                        rows.append(row); row = []
                if row:
                    rows.append(row)
                rows.append([("🎲 Tasodifiy", f"bgp:{job['id']}:0")])
                shown = brand.THEME_NAMES[int(now[1:]) - 1] if now.startswith("b") and now[1:].isdigit() else (f"klip #{now}" if now else "—")
                self.tg.send_buttons(f"🎨 #{job['id']} uchun qaysi fon? 🎨 = logo + doira spektr, 🎞 = sizning klipingiz (hozirgi: {shown})", rows, chat_id)
                return
            token = parts[2] if len(parts) > 2 else "0"
            if token.startswith("b") and token[1:].isdigit() and 1 <= int(token[1:]) <= brand.THEME_COUNT:
                bg_id = token
            elif token.isdigit():
                bg_id = int(token) or None
                if bg_id and bg_id not in dict(items):
                    self.tg.answer_callback(cb["id"], "Bu fon topilmadi.")
                    return
            else:
                self.tg.answer_callback(cb["id"], "Noma'lum fon.")
                return
            self.tg.answer_callback(cb["id"], "Qayta yig'ilmoqda...")
            threading.Thread(target=self.do_redo, args=(job["id"], chat_id, bg_id or None), daemon=True).start()
            return
        if data.startswith("show:"):
            raw = data.split(":")[1]
            job = self.db.get(int(raw)) if raw.isdigit() else None
            if not job or job["status"] not in ("ready", "review", "posted"):
                self.tg.answer_callback(cb["id"], "Topilmadi.")
                return
            self.tg.answer_callback(cb["id"], "Yuborilmoqda...")
            jid = job["id"]
            btns = [] if job["status"] == "posted" else [
                [("✅ Joylash", f"post:{jid}"), ("🔄 Boshqa qil", f"redo:{jid}"), ("❌ Bekor", f"del:{jid}")],
                [("🎞 Fon tanlash", f"bgs:{jid}")]]
            threading.Thread(target=lambda: self.show_video(job, f"🎬 #{jid} {job['title']}\n{job['yt_title']}", btns, chat_id)
                             or self.tg.send("Videoni yuborib bo'lmadi.", chat_id), daemon=True).start()
            return
        if data.startswith("lang:"):
            _, lang, raw_id = (data.split(":") + ["", ""])[:3]
            job = self.db.get(int(raw_id)) if raw_id.isdigit() and lang in ("uz", "en", "tr") else None
            if not job or job["status"] != "pending":
                self.tg.answer_callback(cb["id"], "Bu allaqachon boshlangan yoki bekor qilingan.")
                return
            self.db.kv_set(f"lang:{job['id']}", lang)
            name = {"uz": "o'zbekcha", "tr": "turkcha"}.get(lang, "inglizcha")
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
            self.tg.answer_callback(cb["id"], GENRES[key][0])
            self.tg.send_buttons(
                f"🎤 #{job['id']} ({GENRES[key][0]}). Kim aytsin?",
                [[(VOICES[k][0], f"voc:{k}:{job['id']}") for k in VOICES],
                 [("🎲 Tasodifiy", f"voc:rnd:{job['id']}"), ("❌ Bekor", f"del:{job['id']}")]], chat_id)
            return
        if data.startswith("voc:"):
            _, key, raw_id = (data.split(":") + ["", ""])[:3]
            job = self.db.get(int(raw_id)) if raw_id.isdigit() and (key in VOICES or key == "rnd") else None
            if not job or job["status"] != "pending":
                self.tg.answer_callback(cb["id"], "Bu allaqachon boshlangan yoki bekor qilingan.")
                return
            if key != "rnd":
                self.db.kv_set(f"voice:{job['id']}", key)
            self.db.update(job["id"], status="queued")
            self.wake.set()
            self.tg.answer_callback(cb["id"], "Boshlandi")
            self.tg.send(f"🎶 #{job['id']} tayyorlanmoqda. Tayyor bo'lganda videoni yuboraman "
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
            self.post_busy = True
            return False
        try:
            video_id = self.pipeline.publish(job_id)
            title = self.db.get(job_id)["title"]
            self.tg.send(f"🚀 YouTube'ga joylandi: {title}\nhttps://youtu.be/{video_id}")
            return True
        except Exception as exc:
            log.exception("publish failed")
            if "uploadLimitExceeded" in str(exc):
                # YouTube's daily upload cap: stop retrying every 30 s, try again in an hour
                self.db.kv_set("post_block_until", str(time.time() + 3600))
                self.tg.send("⚠️ YouTube bugungi yuklash limitiga yetdi (uploadLimitExceeded). Video saqlanib turibdi, "
                             "yo'qolmaydi. Odatda limit ~24 soatda tiklanadi; avtomatik rejim 1 soatdan keyin qayta uriniladi. "
                             "Kanalni telefon raqam bilan tasdiqlasangiz (youtube.com/verify) limit ancha oshadi.")
            else:
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
            f"Fon videolar: {len(backgrounds.list_all(self.cfg))} ta (rejim: {db.kv_get('bg_mode', 'brand')})\n"
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

    def add_uploaded_song(self, audio: dict, caption: str, chat_id: int) -> None:
        """Owner sent a finished mp3: make the cover/video + YouTube metadata around it (no song generation)."""
        import subprocess
        import tempfile
        from pathlib import Path
        if int(audio.get("file_size") or 0) > 20 * 1024 * 1024:
            self.tg.send("⚠️ Audio 20 MB dan katta (Telegram botlar shundan kattasini olmaydi). Qisqaroq yoki siqilgan mp3 yuboring.", chat_id)
            return
        try:
            first = caption.splitlines()[0].strip() if caption else ""
            title = (first if 0 < len(first) <= 45 else "") or (audio.get("title") or "").strip() \
                or Path(audio.get("file_name") or "").stem.replace("_", " ").strip()
            job_id = self.db.add_job("user", caption or "", "", status="pending")
            folder = self.pipeline.job_dir(job_id)
            folder.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory() as tmp:
                src = Path(tmp) / "in_audio"
                self.tg.download_file(audio["file_id"], src)
                mp3 = folder / "song.mp3"
                subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-vn", "-c:a", "libmp3lame", "-b:a", "256k", str(mp3)],
                                check=True, timeout=300)
            if video.probe_duration(mp3) < 20:
                raise ValueError("Audio juda qisqa")
            self.db.update(job_id, title=title[:45], style="__upload__", mp3_path=str(mp3), lyrics="")
            self.db.update(job_id, status="queued")
            self.wake.set()
            self.tg.send(f"📥 #{job_id} qabul qilindi{(' — ' + title) if title else ''}. Muqova va videoni yig'ib, tasdiqlash uchun yuboraman "
                         f"(1–3 daqiqa). Mureka'ga pul sarflanmaydi.\n"
                         f"Maslahat: audio bilan birga izoh yozsangiz (nomi, janr, nima haqida), YouTube matni shunga moslanadi.", chat_id)
        except Exception as exc:
            log.exception("uploaded song failed")
            self.tg.send(f"⚠️ Audioni qabul qilib bo'lmadi: {str(exc)[:300]}", chat_id)

    def merge_dir(self):
        d = self.cfg.data_dir / "merge_tmp"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def merge_collect(self, media: dict, chat_id: int) -> None:
        """Owner sent a clip: keep it aside; when they press Tayyor all clips become ONE background."""
        if int(media.get("file_size") or 0) > 20 * 1024 * 1024:
            self.tg.send("⚠️ Video 20 MB dan katta: Telegram botlarga shundan kattasini bermaydi. "
                         "Uni \"fayl\" emas, oddiy \"video\" qilib yuboring yoki qisqaroq/siqilgan qilib qayta yuboring.", chat_id)
            return
        with self.merge_lock:
            n = len(list(self.merge_dir().glob("part_*.mp4"))) + 1
            try:
                self.tg.download_file(media["file_id"], self.merge_dir() / f"part_{n:02d}.mp4")
            except Exception as exc:
                log.exception("merge download failed")
                self.tg.send(f"⚠️ Videoni olib bo'lmadi: {str(exc)[:300]}", chat_id)
                return
        self.db.kv_set("merge_mode", "1")
        if self.merge_timer:
            self.merge_timer.cancel()
        self.merge_timer = threading.Timer(300, self.merge_finish, args=(chat_id,))
        self.merge_timer.daemon = True
        self.merge_timer.start()
        self.tg.send_buttons(
            f"➕ {n}-video qabul qilindi. Yana video yuborsangiz, hammasi ketma-ket BITTA fon bo'lib ulanadi.\n"
            "Tugagach ✅ Tayyor ni bosing (5 daqiqada o'zi saqlaydi).",
            [("✅ Tayyor", "mg:done"), ("🗑 Bekor", "mg:cancel")], chat_id)

    def merge_finish(self, chat_id: int) -> None:
        import shutil
        if self.merge_timer:
            self.merge_timer.cancel()
            self.merge_timer = None
        with self.merge_lock:
            parts = sorted(self.merge_dir().glob("part_*.mp4"))
            self.db.kv_set("merge_mode", "0")
            if not parts:
                self.tg.send("Hozircha video yo'q. Videolarni yuboring.", chat_id)
                return
            self.tg.send(f"⏳ {len(parts)} ta video {'birlashtirilmoqda' if len(parts) > 1 else 'tayyorlanmoqda'}...", chat_id)
            try:
                if len(parts) == 1:
                    bg_id, seconds = backgrounds.add(self.cfg, parts[0])
                else:
                    bg_id, seconds = backgrounds.add_many(self.cfg, parts)
                total = len(backgrounds.list_all(self.cfg))
                self.tg.send(f"✅ Fon #{bg_id} tayyor ({seconds:.0f} soniya, {len(parts)} ta videodan). "
                             f"Jami fon videolar: {total} ta.", chat_id)
            except Exception as exc:
                log.exception("background build failed")
                self.tg.send(f"⚠️ Fonni tayyorlab bo'lmadi: {str(exc)[:300]}", chat_id)
            finally:
                shutil.rmtree(self.merge_dir(), ignore_errors=True)

    def merge_cancel(self, chat_id: int) -> None:
        import shutil
        if self.merge_timer:
            self.merge_timer.cancel()
            self.merge_timer = None
        with self.merge_lock:
            shutil.rmtree(self.merge_dir(), ignore_errors=True)
        self.db.kv_set("merge_mode", "0")
        self.tg.send("🗑 Yuborilgan videolar bekor qilindi.", chat_id)

    def join_existing(self, ids: list, chat_id: int) -> None:
        """Join already-saved backgrounds into one new background and remove the originals."""
        have = dict(backgrounds.list_all(self.cfg))
        paths = [have[i] for i in ids if i in have]
        if len(paths) < 2 or len(paths) != len(ids):
            self.tg.send("Foydalanish: /fon_qosh 1 2  (mavjud fon raqamlari, kamida 2 ta; /fonlar ro'yxatiga qarang)", chat_id)
            return
        self.tg.send("⏳ Birlashtirilmoqda...", chat_id)
        try:
            bg_id, seconds = backgrounds.add_many(self.cfg, paths)
            for i in ids:
                backgrounds.remove(self.cfg, i)
            self.tg.send(f"✅ Fon #{bg_id} tayyor ({seconds:.0f} soniya). #{', #'.join(map(str, ids))} o'rniga.", chat_id)
        except Exception as exc:
            log.exception("join existing failed")
            self.tg.send(f"⚠️ Birlashtirib bo'lmadi: {str(exc)[:300]}", chat_id)

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
        if media:
            self.merge_collect(media, chat_id)
            return
        audio = msg.get("audio") or (doc if str(doc.get("mime_type", "")).startswith("audio/") else None)
        if audio:
            threading.Thread(target=self.add_uploaded_song, args=(audio, (msg.get("caption") or "").strip(), chat_id),
                             daemon=True).start()
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
            elif cmd == "/tayyor":
                threading.Thread(target=self.merge_finish, args=(chat_id,), daemon=True).start()
            elif cmd == "/fon_qosh":
                ids = [int(x) for x in arg.replace(",", " ").split() if x.isdigit()]
                threading.Thread(target=self.join_existing, args=(ids, chat_id), daemon=True).start()
            elif cmd == "/holat":
                self.tg.send(self.status_text(), chat_id)
            elif cmd == "/navbat":
                self.tg.send(self.queue_text(), chat_id)
            elif cmd == "/yarat":
                job_id = self.pipeline.add_auto_job(arg and f"{arg} | {arg}")
                self.tg.send(f"Navbatga qo'shildi #{job_id}. Tayyor bo'lganda xabar beraman.", chat_id)
            elif cmd in ("/videolar", "/tayyorlar"):
                self.list_videos(chat_id)
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
                mode = self.db.kv_get("bg_mode", "brand")
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
                names = {"brand": "Fon: logo + doira spektr, 11 xil animatsion fon 🎨",
                         "video": "Fon: faqat sizning klip videolaringiz 🎞 (ustida bitta chiziq)",
                         "aralash": "Fon: logo-spektr va sizning kliplaringiz tasodifiy aralash 🎲",
                         "rasm": "Fon: AI chizgan rasm 🖼"}
                if arg in names:
                    self.db.kv_set("bg_mode", arg)
                    self.tg.send(names[arg], chat_id)
                else:
                    self.tg.send("Foydalanish: /fon_rejim brand (logo + doira, 11 fon) | video (klipingiz) | aralash | rasm", chat_id)
            elif cmd == "/eksport":
                target = int(arg) if arg.isdigit() else None
                if target:
                    threading.Thread(target=self.export_job, args=(target, chat_id), daemon=True).start()
                else:
                    self.tg.send("Foydalanish: /eksport <id> — qo'shiq fayli, kvadrat muqova va ma'lumotni distribyutor uchun yuboradi (id ni /videolar yoki /navbat dan oling).", chat_id)
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
            [[("🇺🇿 O'zbekcha", f"lang:uz:{job_id}"), ("🇹🇷 Turkcha", f"lang:tr:{job_id}"),
              ("🇬🇧 Inglizcha", f"lang:en:{job_id}")], [("❌ Bekor", f"del:{job_id}")]], chat_id)

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

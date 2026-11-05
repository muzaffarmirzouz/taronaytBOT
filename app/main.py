"""Entry point: Telegram control bot + production worker + posting scheduler."""
from __future__ import annotations

import json
import logging
import sys
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from . import scheduler
from .config import Config
from .db import DB, STOCK_STATUSES
from .pipeline import BudgetExceeded, Pipeline
from .tg import Telegram

log = logging.getLogger("bot")

HELP = """🎵 Kunlik qo'shiq boti

Oddiy matn (sher/qo'shiq so'zlari) yuboring: bot uni inglizcha qo'shiqqa aylantirib, navbatning BOSHIGA qo'yadi.

/holat — navbat, keyingi joylash vaqti, bugungi sarf
/navbat — oxirgi 10 ta qo'shiq
/yarat [mavzu] — hoziroq yangi qo'shiq yaratish (mavzu ixtiyoriy)
/joyla — keyingi tayyor videoni hoziroq YouTube'ga joylash
/bekor <id> — navbatdagi qo'shiqni bekor qilish
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
            self.stop.wait(15)

    def fill_stock(self) -> None:
        stock = self.db.count(*STOCK_STATUSES)
        if stock < self.cfg.stock_target and self.pipeline.mureka_used_today() < self.cfg.max_mureka_per_day:
            job_id = self.pipeline.add_auto_job()
            log.info("stock %s < %s: queued auto job %s", stock, self.cfg.stock_target, job_id)

    def produce_next(self) -> None:
        job = self.db.next_to_produce()
        if not job:
            return
        with self.produce_lock:
            try:
                self.pipeline.produce(job["id"])
                done = self.db.get(job["id"])
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

    def handle(self, msg: dict) -> None:
        chat_id = msg["chat"]["id"]
        if msg.get("from", {}).get("id") != self.cfg.admin_id:
            self.tg.send("Bu bot yopiq.", chat_id)
            return
        text = (msg.get("text") or "").strip()
        if not text:
            self.tg.send("Matn yuboring (sher yoki qo'shiq so'zlari).", chat_id)
            return
        if text.startswith("/"):
            cmd, _, arg = text.partition(" ")
            cmd = cmd.split("@")[0].lower()
            arg = arg.strip()
            if cmd in ("/start", "/help"):
                self.tg.send(HELP, chat_id)
            elif cmd == "/holat":
                self.tg.send(self.status_text(), chat_id)
            elif cmd == "/navbat":
                self.tg.send(self.queue_text(), chat_id)
            elif cmd == "/yarat":
                job_id = self.pipeline.add_auto_job(arg and f"{arg} | {arg}")
                self.tg.send(f"Navbatga qo'shildi #{job_id}. Tayyor bo'lganda xabar beraman.", chat_id)
            elif cmd == "/joyla":
                job = self.db.next_ready()
                if not job:
                    self.tg.send("Tayyor video yo'q.", chat_id)
                else:
                    self.tg.send(f"Joylanmoqda: #{job['id']} {job['title']}", chat_id)
                    threading.Thread(target=self.post_job, args=(job["id"],), daemon=True).start()
            elif cmd == "/bekor":
                if arg.isdigit() and self.db.get(int(arg)) and self.db.get(int(arg))["status"] in ("queued", "ready"):
                    self.db.update(int(arg), status="failed", error="bekor qilindi")
                    self.tg.send(f"#{arg} bekor qilindi.", chat_id)
                else:
                    self.tg.send("Foydalanish: /bekor <id> (faqat navbatdagi yoki tayyor qo'shiq).", chat_id)
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
        job_id = self.pipeline.db.add_job("user", raw_text=text)
        self.tg.send(
            f"🎤 Qabul qilindi #{job_id}. Inglizcha qo'shiqqa aylantirib, navbatning boshiga qo'yaman. "
            f"Tayyor bo'lganda xabar beraman.", chat_id)

    def poll_loop(self) -> None:
        offset = None
        while not self.stop.is_set():
            try:
                for upd in self.tg.updates(offset):
                    offset = upd["update_id"] + 1
                    if "message" in upd:
                        try:
                            self.handle(upd["message"])
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

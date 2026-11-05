"""The production line: text package -> song -> picture -> video -> YouTube.
Each step saves its result in the DB/disk, so a crashed job resumes where it stopped."""
from __future__ import annotations

import json
import logging
import random
import time
from datetime import datetime, timezone
from pathlib import Path

from . import claude_api, images, mureka, video, youtube

log = logging.getLogger("pipeline")

# (genre/vibe, topic) pairs. Rotated randomly so the channel never feels templated.
IDEAS = [
    ("upbeat pop", "a summer road trip with friends"),
    ("acoustic folk", "missing home and family"),
    ("lo-fi chill", "late night city lights and quiet thoughts"),
    ("indie pop", "first love and butterflies"),
    ("cinematic ballad", "a long journey along the Silk Road"),
    ("country pop", "small-town dreams and big hearts"),
    ("soft rock", "starting over after a heartbreak"),
    ("dance pop", "feeling unstoppable on a Friday night"),
    ("slow R&B", "a quiet rainy evening together"),
    ("folk pop with dutar and rubab flavor", "sunset over an ancient Central Asian city"),
    ("motivational pop", "never giving up on your dream"),
    ("dreamy synth-pop", "stargazing in the desert"),
    ("feel-good funk", "dancing in the kitchen on a Sunday morning"),
    ("gentle piano ballad", "a letter to a friend far away"),
    ("reggae-pop", "slow beach days and sun on your skin"),
    ("acoustic singer-songwriter", "growing up and looking back"),
    ("club dance, four-on-the-floor EDM with catchy vocal hook", "losing yourself on the dance floor all night"),
    ("afro house with warm percussion and smooth vocals", "sunset rooftop party and good vibes"),
    ("deep house club, female vocal", "a stranger's eyes across the crowded room"),
    ("amapiano-style dance groove", "weekend freedom and dancing with friends"),
    ("future bass club anthem", "chasing the night until sunrise"),
    ("tropical house", "summer festival and neon lights"),
]


class BudgetExceeded(Exception):
    pass


class Pipeline:
    def __init__(self, cfg, db, notify):
        self.cfg = cfg
        self.db = db
        self.notify = notify

    # --- helpers --------------------------------------------------------
    def job_dir(self, job_id: int) -> Path:
        d = self.cfg.data_dir / "media" / f"{job_id:05d}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _budget_key(self) -> str:
        return "mureka:" + datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def mureka_used_today(self) -> int:
        return int(self.db.kv_get(self._budget_key(), "0"))

    def _check_budget(self) -> None:
        if self.mureka_used_today() >= self.cfg.max_mureka_per_day:
            raise BudgetExceeded(f"daily Mureka limit reached ({self.cfg.max_mureka_per_day})")

    def _count_mureka(self) -> None:
        self.db.kv_set(self._budget_key(), str(self.mureka_used_today() + 1))

    def pick_idea(self) -> str:
        recent = set(self.db.recent_themes(8))
        options = [f"{g} | {t}" for g, t in IDEAS if f"{g} | {t}" not in recent] or [f"{g} | {t}" for g, t in IDEAS]
        return random.choice(options)

    def add_auto_job(self, theme: str = "") -> int:
        return self.db.add_job("auto", theme=theme or self.pick_idea())

    # --- produce --------------------------------------------------------
    def produce(self, job_id: int) -> None:
        job = self.db.get(job_id)
        self.db.update(job_id, status="producing", error="")
        folder = self.job_dir(job_id)

        if not job["lyrics"]:
            pkg = claude_api.make_package(
                self.cfg, raw_text=job["raw_text"], theme=job["theme"],
                avoid_titles=self.db.recent_titles(25),
            )
            self.db.update(
                job_id, title=pkg["title"], lyrics=pkg["lyrics"], style=pkg["style"],
                image_prompt=pkg["image_prompt"], yt_title=pkg["yt_title"],
                yt_description=pkg["yt_description"], yt_tags=json.dumps(pkg["yt_tags"]),
                theme=job["theme"] or pkg.get("theme", ""),
            )
            job = self.db.get(job_id)

        mp3 = folder / "song.mp3"
        if not (job["mp3_path"] and Path(job["mp3_path"]).exists()):
            self._check_budget()
            result = mureka.generate(self.cfg, job["lyrics"], job["style"])
            self._count_mureka()
            mureka.download(result["url"], mp3)
            self.db.update(job_id, mp3_path=str(mp3))
            job = self.db.get(job_id)

        image = folder / "cover.png"
        if not (job["image_path"] and Path(job["image_path"]).exists()):
            try:
                images.generate(self.cfg, job["image_prompt"], image)
            except Exception as exc:
                log.warning("job %s: image generation failed (%s); using fallback art", job_id, exc)
                try:
                    self.notify(f"⚠️ #{job_id}: rasm chizilmadi, oddiy fon qo'yildi.\nSabab: {str(exc)[:600]}")
                except Exception:
                    pass
                video.fallback_image(image, seed=job_id)
            self.db.update(job_id, image_path=str(image))
            job = self.db.get(job_id)

        mp4 = folder / "video.mp4"
        if not (job["mp4_path"] and Path(job["mp4_path"]).exists()):
            video.make_video(Path(job["image_path"]), Path(job["mp3_path"]), mp4)
            self.db.update(job_id, mp4_path=str(mp4))

        self.db.update(job_id, status="ready")
        log.info("job %s ready: %s", job_id, job["title"])

    # --- publish --------------------------------------------------------
    def build_description(self, job: dict, tags: list) -> str:
        parts = [job["yt_description"].strip()]
        parts.append("🎵 This song was created with AI. New songs every day — subscribe so you don't miss one.")
        if self.cfg.channel_footer:
            parts.append(self.cfg.channel_footer)
        hashtags = " ".join("#" + "".join(ch for ch in t.title() if ch.isalnum()) for t in tags[:3])
        if hashtags.strip():
            parts.append(hashtags)
        return "\n\n".join(p for p in parts if p)

    def publish(self, job_id: int) -> str:
        job = self.db.get(job_id)
        if not job or not job["mp4_path"] or not Path(job["mp4_path"]).exists():
            raise RuntimeError(f"job {job_id} has no video to publish")
        self.db.update(job_id, status="posting")
        tags = json.loads(job["yt_tags"] or "[]")
        thumb = None
        try:
            thumb = video.make_thumbnail(Path(job["image_path"]), self.job_dir(job_id) / "thumb.jpg")
        except Exception as exc:
            log.warning("thumbnail not created: %s", exc)
        try:
            video_id = youtube.upload(
                self.cfg, Path(job["mp4_path"]), job["yt_title"] or job["title"],
                self.build_description(job, tags), tags, thumb,
            )
        except Exception:
            self.db.update(job_id, status="ready")
            raise
        self.db.update(job_id, status="posted", youtube_id=video_id, posted_at=time.time())
        return video_id

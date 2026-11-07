"""The production line: text package -> song -> picture -> video -> YouTube.
Each step saves its result in the DB/disk, so a crashed job resumes where it stopped."""
from __future__ import annotations

import json
import logging
import random
import time
from datetime import datetime, timezone
from pathlib import Path

from . import backgrounds, brand, claude_api, images, mureka, video, youtube

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

    def _check_budget(self, source: str = "auto") -> None:
        # The owner's own songs may go past the automatic daily cap, up to a hard ceiling.
        limit = self.cfg.max_mureka_per_day + (10 if source == "user" else 0)
        if self.mureka_used_today() >= limit:
            raise BudgetExceeded(f"daily Mureka limit reached ({limit})")

    def _count_mureka(self) -> None:
        self.db.kv_set(self._budget_key(), str(self.mureka_used_today() + 1))

    def pick_idea(self) -> str:
        recent = set(self.db.recent_themes(8))
        options = [f"{g} | {t}" for g, t in IDEAS if f"{g} | {t}" not in recent] or [f"{g} | {t}" for g, t in IDEAS]
        return random.choice(options)

    def add_auto_job(self, theme: str = "") -> int:
        job_id = self.db.add_job("auto", theme=theme or self.pick_idea())
        # a share of the automatic songs is sung in Turkish (AUTO_TURKISH_PERCENT, default 30)
        if random.randint(1, 100) <= self.cfg.auto_turkish_percent:
            self.db.kv_set(f"lang:{job_id}", "tr")
        return job_id

    def _choose_look(self, job_id: int):
        """Background for this song: ("brand", n) = logo + circular spectrum on animated theme n (1-11),
        ("clip", path) = one of the owner's looping clips, or None = an AI picture (OpenAI)."""
        mode = self.db.kv_get("bg_mode", "brand")
        job = self.db.get(job_id)
        if job and job["source"] == "auto":
            mode = "brand"  # automatic songs always use the 11 logo backgrounds, never clips or AI pictures
        forced = self.db.kv_get(f"bgforce:{job_id}", "")
        prev = self.db.kv_get(f"bg:{job_id}", "")
        clips = backgrounds.list_all(self.cfg)
        if forced.startswith("b") and forced[1:].isdigit() and 1 <= int(forced[1:]) <= brand.THEME_COUNT:
            self.db.kv_set(f"bg:{job_id}", forced)
            return ("brand", int(forced[1:]))
        if forced.isdigit():  # the owner chose this clip for this song
            for bg_id, path in clips:
                if bg_id == int(forced):
                    self.db.kv_set(f"bg:{job_id}", forced)
                    return ("clip", path)
        if mode == "rasm":
            return None
        use_brand = mode == "brand" or (mode == "aralash" and (not clips or random.random() < 0.5))
        if use_brand:
            options = [n for n in range(1, brand.THEME_COUNT + 1) if f"b{n}" != prev]
            n = random.choice(options)
            self.db.kv_set(f"bg:{job_id}", f"b{n}")
            return ("brand", n)
        pick = backgrounds.pick(self.cfg, exclude=int(prev) if prev.isdigit() else None)
        if not pick:
            return None
        self.db.kv_set(f"bg:{job_id}", str(pick[0]))
        return ("clip", pick[1])

    # --- produce --------------------------------------------------------
    def _voice_for(self, job_id: int) -> str:
        """Chosen voice for this song; automatic songs get one at random (mostly male) and keep it."""
        voice = self.db.kv_get(f"voice:{job_id}", "")
        if voice not in claude_api.VOICES:
            voice = claude_api.pick_voice()
            self.db.kv_set(f"voice:{job_id}", voice)
        return voice

    def produce(self, job_id: int, style: str | None = None) -> None:
        job = self.db.get(job_id)
        self.db.update(job_id, status="producing", error="")
        folder = self.job_dir(job_id)

        if job["style"] == "__upload__" and not job["yt_title"]:
            # a finished track the owner uploaded: only the YouTube metadata is needed, no song generation
            meta = claude_api.make_meta(self.cfg, hint=job["raw_text"], title=job["title"],
                                        avoid_titles=self.db.recent_titles(25))
            self.db.update(
                job_id, title=meta["title"], image_prompt=meta["image_prompt"], yt_title=meta["yt_title"],
                yt_description=meta["yt_description"], yt_tags=json.dumps(meta["yt_tags"]),
                lyrics=job["lyrics"] or "[uploaded track]",
            )
            job = self.db.get(job_id)

        if not job["lyrics"]:
            pkg = claude_api.make_package(
                self.cfg, raw_text=job["raw_text"], theme=job["theme"],
                avoid_titles=self.db.recent_titles(25),
                language=self.db.kv_get(f"lang:{job_id}", "en"),
                genre=self.db.kv_get(f"genre:{job_id}", ""),
                voice=self._voice_for(job_id),
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
            self._check_budget(job["source"])
            result = mureka.generate(self.cfg, job["lyrics"], job["style"])
            self._count_mureka()
            mureka.download(result["url"], mp3)
            try:  # light mastering: even loudness, tighter low end; the untouched file is kept as song_raw.mp3
                mastered = folder / "song_mastered.mp3"
                video.master_audio(mp3, mastered)
                if mastered.exists() and mastered.stat().st_size > 10_000:
                    mp3.replace(folder / "song_raw.mp3")
                    mastered.replace(mp3)
            except Exception as exc:
                log.warning("mastering skipped: %s", exc)
            self.db.update(job_id, mp3_path=str(mp3))
            job = self.db.get(job_id)

        image = folder / "cover.png"
        mp4 = folder / "video.mp4"
        needs_image = not (job["image_path"] and Path(job["image_path"]).exists())
        needs_video = not (job["mp4_path"] and Path(job["mp4_path"]).exists())
        look = self._choose_look(job_id) if (needs_image or needs_video) else None
        bg = look[1] if look and look[0] == "clip" else None
        theme = look[1] if look and look[0] == "brand" else None
        if (needs_image or needs_video) and not look:
            self.db.kv_set(f"bg:{job_id}", "")

        if needs_image:
            if theme:
                brand.cover_frame(image, theme, "")  # thumbnail source: logo + spectrum on the theme
            elif bg:
                video.frame_from(bg, image)  # thumbnail source; no image-generation cost
            else:
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

        if needs_video:
            prev = self.db.kv_get(f"style:{job_id}", "")
            if theme:
                style = f"brand{theme}"
                brand.render(Path(job["mp3_path"]), mp4, theme, job["title"])
            elif bg:
                style = "clip"  # over the owner's clips nothing is drawn: no waveform, no bars
                video.make_video(Path(job["image_path"]), Path(job["mp3_path"]), mp4, style=style,
                                 bg_video=bg, title=job["title"])
            else:
                style = style or video.pick_style(exclude=prev)
                video.make_video(Path(job["image_path"]), Path(job["mp3_path"]), mp4, style=style)
            self.db.kv_set(f"style:{job_id}", style)
            self.db.update(job_id, mp4_path=str(mp4))

        # The owner's own songs wait for a yes/no before they can be posted; automatic ones go straight to stock.
        self.db.update(job_id, status="review" if job["source"] == "user" else "ready")
        log.info("job %s finished: %s", job_id, job["title"])

    def new_look(self, job_id: int, bg_id=None) -> None:
        """Owner asked for "another version": fresh cover and a different visual style, same song.
        With bg_id the owner picked that background clip; otherwise a different one is drawn."""
        self.db.kv_set(f"bgforce:{job_id}", str(bg_id) if bg_id else "")
        job = self.db.get(job_id)
        base = (job["image_prompt"] or "").split(" [variation")[0]
        n = int(self.db.kv_get(f"variation:{job_id}", "0")) + 1
        self.db.kv_set(f"variation:{job_id}", str(n))
        self.db.update(
            job_id, status="producing", image_path="", mp4_path="",
            image_prompt=f"{base} [variation {n}: a different composition, time of day and art style]",
        )
        self.produce(job_id)

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
            has_bg = bool(self.db.kv_get(f"bg:{job_id}"))
            thumb = video.make_thumbnail(Path(job["image_path"]), self.job_dir(job_id) / "thumb.jpg",
                                         title=job["title"] if has_bg else "")
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

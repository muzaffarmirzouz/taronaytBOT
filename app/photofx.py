"""Photo mode: the owner's picture is the background and the bot animates weather over it
(rain, tears running down, wind, snow, or a mix). Everything is drawn in code (numpy + Pillow); frames are
piped to ffmpeg together with the song. The picture itself stays a still (slow zoom + a pulse on the bass),
except with wind, which makes the whole picture flutter a little (hair, clothes, branches)."""
import logging
import math
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from . import brand, video

log = logging.getLogger(__name__)

EFFECTS = {
    "rain": "🌧 Yomg'ir",
    "tears": "😢 Ko'z yosh",
    "wind": "🌬 Shamol",
    "snow": "❄️ Qor",
}
# what the buttons offer (the effect string is "+"-joined single effects)
CHOICES = ["rain", "tears", "wind", "snow", "rain+wind", "tears+rain", "snow+wind"]
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def label(fx: str) -> str:
    return " + ".join(EFFECTS.get(p, p) for p in fx.split("+") if p)


def normalise(fx: str) -> str:
    parts = [p for p in dict.fromkeys((fx or "").split("+")) if p in EFFECTS]
    return "+".join(parts) or "rain"


def _soft(r: int, rgb: tuple, alpha: int) -> Image.Image:
    s = r * 4
    im = Image.new("RGBA", (s, s), rgb + (0,))
    a = Image.new("L", (s, s), 0)
    ImageDraw.Draw(a).ellipse((s // 2 - r, s // 2 - r, s // 2 + r, s // 2 + r), fill=alpha)
    im.putalpha(a.filter(ImageFilter.GaussianBlur(max(1, r // 2))))
    return im


class Scene:
    def __init__(self, photo: Path, fx: str, title: str = "", size=(1920, 1080), fps: int = 24, duration: float = 180.0):
        self.w, self.h = size
        self.fps = fps
        self.fx = set(normalise(fx).split("+"))
        self.title = title
        self.duration = max(duration, 1.0)
        self.s = self.w / 1920.0
        rnd = np.random.default_rng(7)
        self.rnd = rnd
        w, h = self.w, self.h

        img = ImageOps.exif_transpose(Image.open(photo)).convert("RGB")
        self.bw, self.bh = int(w * 1.14), int(h * 1.14)
        base = ImageOps.fit(img, (self.bw, self.bh), Image.LANCZOS, centering=(0.5, 0.4))
        self.base = self._grade(base)

        yy, xx = np.mgrid[0:h, 0:w]
        r = np.sqrt(((xx - w / 2) / (w / 2)) ** 2 + ((yy - h / 2) / (h / 2)) ** 2)
        strength = 0.55 if ({"rain", "tears"} & self.fx) else 0.4
        self.vig = (1 - strength * np.clip(r - 0.45, 0, 1) ** 1.6)[..., None].astype(np.float32)
        self.xx, self.yy = xx, yy

        s = self.s
        n = lambda a: int(a * max(s, 0.5))
        # rain: slanted streaks
        self.rain = None
        if {"rain", "tears"} & self.fx:
            cnt = 420 if "rain" in self.fx else 90
            self.rain = dict(
                x=rnd.random(cnt) * (w + 400), y=rnd.random(cnt) * (h + 200), v=(1300 + rnd.random(cnt) * 900) * s,
                ln=(34 + rnd.random(cnt) * 60) * s, a=(40 + rnd.random(cnt) * 120).astype(int),
                wd=np.where(rnd.random(cnt) < 0.25, 2, 1))
        # drops sliding down the "glass" (a few with rain, big and slow for tears)
        self.drops = []
        if "tears" in self.fx:
            for i in range(9):
                self.drops.append(dict(x=w * (0.30 + 0.40 * rnd.random()), y0=h * (0.30 + 0.22 * rnd.random()),
                                       v=(38 + 34 * rnd.random()) * s, r=(8 + 6 * rnd.random()) * s,
                                       ph=rnd.random() * 100, trail=0.5))
        elif "rain" in self.fx:
            for i in range(16):
                self.drops.append(dict(x=w * rnd.random(), y0=h * rnd.random() * 0.8, v=(26 + 40 * rnd.random()) * s,
                                       r=(4 + 5 * rnd.random()) * s, ph=rnd.random() * 100, trail=0.35))
        # snow: three depth layers of soft flakes
        self.snow = []
        if "snow" in self.fx:
            for cnt, rad, v, al in ((140, 3, 55, 230), (80, 6, 95, 240), (20, 15, 170, 170)):
                spr = _soft(max(1, n(rad)), (245, 248, 255), al)
                self.snow.append(dict(spr=spr, x=rnd.random(cnt) * w, y=rnd.random(cnt) * h, v=v * s * (0.8 + 0.5 * rnd.random(cnt)),
                                      ph=rnd.random(cnt) * 6.28, amp=(10 + 30 * rnd.random(cnt)) * s, f=0.4 + rnd.random(cnt)))
        # wind: fast thin streaks and drifting dust
        self.streaks = None
        self.motes = None
        if "wind" in self.fx:
            cnt = 16
            self.streaks = dict(x=rnd.random(cnt) * w, y=rnd.random(cnt) * h, v=(1500 + rnd.random(cnt) * 1000) * s,
                                ln=(160 + rnd.random(cnt) * 260) * s, a=(25 + rnd.random(cnt) * 45).astype(int))
            cnt = 70
            self.motes = dict(x=rnd.random(cnt) * w, y=rnd.random(cnt) * h, v=(220 + rnd.random(cnt) * 380) * s,
                              ph=rnd.random(cnt) * 6.28, r=1 + rnd.random(cnt) * 2.2)
        self.title_sprite = self._title_sprite()

    def _grade(self, im: Image.Image) -> Image.Image:
        arr = np.asarray(im).astype(np.float32)
        if {"rain", "tears"} & self.fx:
            arr *= np.array([0.80, 0.87, 0.98], dtype=np.float32)
        elif "snow" in self.fx:
            arr *= np.array([0.94, 0.98, 1.04], dtype=np.float32)
        elif "wind" in self.fx:
            arr *= np.array([1.02, 1.0, 0.96], dtype=np.float32)
        return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))

    def _title_sprite(self):
        if not self.title:
            return None
        try:
            font = ImageFont.truetype(FONT, int(40 * self.s))
        except OSError:
            font = ImageFont.load_default()
        txt = "♪  " + self.title
        tw = int(ImageDraw.Draw(Image.new("RGBA", (4, 4))).textlength(txt, font=font)) + 10
        sp = Image.new("RGBA", (tw, int(64 * self.s)), (0, 0, 0, 0))
        d = ImageDraw.Draw(sp)
        d.text((7, 7), txt, font=font, fill=(0, 0, 0, 190))
        d.text((4, 4), txt, font=font, fill=(255, 255, 255, 240))
        return sp

    # --- one frame ------------------------------------------------------
    def frame(self, t: float, bass: float) -> Image.Image:
        w, h = self.w, self.h
        prog = min(1.0, t / self.duration)
        z = 0.07 * prog + 0.012 * bass
        vw, vh = w * 1.10 / (1 + z), h * 1.10 / (1 + z)
        cx = self.bw / 2 + math.sin(t * 0.13) * 14 * self.s
        cy = self.bh / 2 + math.cos(t * 0.11) * 8 * self.s
        box = (cx - vw / 2, cy - vh / 2, cx + vw / 2, cy + vh / 2)
        if "wind" in self.fx:
            m = int(14 * self.s) + 2
            k = (w + 2 * m) / w
            ex = (box[2] - box[0]) * (k - 1) / 2
            im = self.base.resize((w + 2 * m, h), Image.BILINEAR, box=(box[0] - ex, box[1], box[2] + ex, box[3]))
            im = self._flutter(im, t, bass).crop((m, 0, m + w, h))
        else:
            im = self.base.resize((w, h), Image.BILINEAR, box=box)
        arr = (np.asarray(im).astype(np.float32) * self.vig).astype(np.uint8)
        out = Image.fromarray(arr).convert("RGBA")

        ov = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(ov)
        self._rain(d, t, bass)
        self._drops(d, t)
        self._snow(ov, t)
        self._wind(d, t)
        out = Image.alpha_composite(out, ov)
        if self.title_sprite is not None:
            a = min(1.0, t / 1.5)
            sp = self.title_sprite
            if a < 1:
                sp = sp.copy()
                sp.putalpha(sp.getchannel("A").point(lambda v: int(v * a)))
            out.alpha_composite(sp, dest=(int(self.w / 2 - sp.width / 2), int(34 * self.s)))
        return out

    def _flutter(self, im: Image.Image, t: float, bass: float) -> Image.Image:
        """Wind: the picture ripples sideways a few pixels (a coarse mesh warp, so it is cheap)."""
        w, h = im.size
        cell = max(24, int(48 * self.s))
        amp = (4.5 + 2.5 * bass) * self.s
        gx = list(range(0, w + 1, cell))
        gy = list(range(0, h + 1, cell))
        if gx[-1] != w:
            gx.append(w)
        if gy[-1] != h:
            gy.append(h)

        def dx(x, y):
            return amp * math.sin(y / (60.0 * self.s) + t * 1.8) * (0.55 + 0.45 * math.sin(x / (330.0 * self.s) - t * 0.7))

        mesh = []
        for j in range(len(gy) - 1):
            y0, y1 = gy[j], gy[j + 1]
            for i in range(len(gx) - 1):
                x0, x1 = gx[i], gx[i + 1]
                mesh.append(((x0, y0, x1, y1), (x0 + dx(x0, y0), y0, x0 + dx(x0, y1), y1, x1 + dx(x1, y1), y1, x1 + dx(x1, y0), y0)))
        return im.transform((w, h), Image.MESH, mesh, Image.BILINEAR)

    def _rain(self, d, t, bass):
        r = self.rain
        if r is None:
            return
        wind = 0.5 if "wind" in self.fx else 0.16
        gain = 1.0 + 0.5 * bass
        ys = (r["y"] + t * r["v"]) % (self.h + 200) - 100
        xs = (r["x"] + t * r["v"] * wind * 0.9) % (self.w + 400) - 200
        for x, y, ln, a, wd in zip(xs, ys, r["ln"], r["a"], r["wd"]):
            d.line((x, y, x - ln * wind, y - ln), fill=(205, 220, 240, min(255, int(a * gain))), width=int(wd))

    def _drops(self, d, t):
        for dr in self.drops:
            travel = self.h * 0.75
            # stop-and-go like a real drop on glass
            run = dr["v"] * (t + 3 * math.sin(t * 0.7 + dr["ph"]) + dr["ph"])
            y = dr["y0"] + run % travel
            x = dr["x"] + math.sin(y / (55 * self.s) + dr["ph"]) * 3 * self.s
            r = dr["r"]
            tl = r * (9 if dr["trail"] > 0.4 else 5)
            d.line((x, y - tl, x, y), fill=(210, 225, 245, 38), width=max(1, int(r * 0.5)))
            d.ellipse((x - r * 0.78, y - r, x + r * 0.78, y + r * 1.15), fill=(190, 212, 240, 78), outline=(235, 244, 255, 150))
            d.ellipse((x - r * 0.35, y - r * 0.55, x - r * 0.05, y - r * 0.15), fill=(255, 255, 255, 210))

    def _snow(self, ov, t):
        wind = 38 if "wind" in self.fx else 8
        for lay in self.snow:
            spr = lay["spr"]
            ys = (lay["y"] + t * lay["v"]) % (self.h + 40) - 20
            xs = (lay["x"] + np.sin(t * lay["f"] + lay["ph"]) * lay["amp"] + t * wind * self.s * (lay["v"] / (90 * self.s))) % self.w
            for x, y in zip(xs, ys):
                px, py = int(x) - spr.width // 2, int(y) - spr.height // 2
                if 0 <= px <= self.w - spr.width and 0 <= py <= self.h - spr.height:
                    ov.alpha_composite(spr, dest=(px, py))

    def _wind(self, d, t):
        if self.streaks is not None:
            st = self.streaks
            xs = (st["x"] + t * st["v"]) % (self.w + 600) - 300
            for x, y, ln, a in zip(xs, st["y"] + np.sin(t * 0.8 + st["x"]) * 18, st["ln"], st["a"]):
                d.line((x - ln, y, x, y - ln * 0.03), fill=(255, 255, 255, int(a)), width=1)
        if self.motes is not None:
            m = self.motes
            xs = (m["x"] + t * m["v"]) % (self.w + 40) - 20
            ys = (m["y"] + np.sin(t * 1.1 + m["ph"]) * 38 * self.s) % self.h
            for x, y, r in zip(xs, ys, m["r"]):
                d.ellipse((x - r, y - r, x + r, y + r), fill=(255, 238, 205, 150))


def render(audio: Path, out_path: Path, photo: Path, fx: str, title: str = "", size=(1920, 1080), fps: int = 24,
           max_seconds: float | None = None, timeout: int = 3600) -> Path:
    """Full video: the photo with the chosen weather effects + the song."""
    duration = video.probe_duration(audio)
    if max_seconds:
        duration = min(duration, max_seconds)
    scene = Scene(photo, fx, title, size, fps, duration)
    sr, N = 22050, 2048
    a = brand._decode(audio, duration + 0.5, sr)
    freqs = np.fft.rfftfreq(N, 1 / sr)
    low = (freqs >= 40) & (freqs < 160)
    window = np.hanning(N)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{scene.w}x{scene.h}", "-r", str(fps), "-i", "-",
           "-i", str(audio), "-map", "0:v", "-map", "1:a", "-t", f"{duration:.2f}", "-c:v", "libx264", "-preset", "veryfast",
           "-crf", "23", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out_path)]
    enc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    peak, bass_s = 1e-6, 0.0
    try:
        for fi in range(int(duration * fps)):
            t = fi / fps
            c = int(t * sr)
            seg = a[c:c + N]
            if len(seg) < N:
                seg = np.pad(seg, (0, N - len(seg)))
            lvl = float(np.abs(np.fft.rfft(seg * window))[low].mean())
            peak = max(lvl, peak * 0.9993)
            bass_s = max(min(1.0, lvl / peak), bass_s * 0.86)
            enc.stdin.write(scene.frame(t, bass_s).tobytes())
        enc.stdin.close()
        rc = enc.wait(timeout=timeout)
    except Exception:
        enc.kill()
        raise
    if rc != 0 or not Path(out_path).exists():
        raise RuntimeError(f"ffmpeg failed with code {rc}")
    return out_path


def cover_frame(out_path: Path, photo: Path, fx: str, title: str = "", size=(1920, 1080)) -> Path:
    """A still of the finished look (the YouTube thumbnail source)."""
    Scene(photo, fx, title, size, 24, 60).frame(6.0, 0.5).convert("RGB").save(out_path, "PNG")
    return out_path

"""Brand mode: the channel logo in the centre, a circular spectrum around it that follows the music,
and one of 11 animated backgrounds drawn in the logo's colours. Everything is drawn in code (numpy + Pillow),
frames are piped to ffmpeg together with the song."""
import logging
import math
import random
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from . import video

log = logging.getLogger(__name__)

THEME_COUNT = 11
THEME_NAMES = ["Nebula", "Bokeh", "Rays", "Synth grid", "Pulse rings", "Stars", "Aurora", "Lasers",
               "Dot matrix", "Floor bars", "Smoke"]
LOGO = Path(__file__).parent / "assets" / "logo.png"
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
CYAN, BLUE, VIOLET, MAGENTA = (0, 229, 255), (80, 120, 255), (150, 90, 255), (225, 75, 255)
NAVY = (10, 12, 36)


def _col(t: float, a: int = 255) -> tuple:
    """Cyan on the left through violet to magenta on the right, like the logo ring."""
    stops = [CYAN, BLUE, VIOLET, MAGENTA]
    t = min(max(t, 0.0), 1.0) * 3
    i = min(int(t), 2)
    f = t - i
    return tuple(int(stops[i][k] * (1 - f) + stops[i + 1][k] * f) for k in range(3)) + (a,)


def _radial(w: int, h: int, power: float = 1.0) -> np.ndarray:
    yy, xx = np.mgrid[0:h, 0:w]
    r = np.sqrt(((xx - w / 2) / (w / 2)) ** 2 + ((yy - h / 2) / (h / 2)) ** 2)
    return np.clip(1 - r, 0, 1) ** power


def _base(w: int, h: int, k: float = 1.0) -> Image.Image:
    g = _radial(w, h)
    px = np.zeros((h, w, 3), dtype=np.float32)
    px[..., 0] = NAVY[0] + 14 * k * g
    px[..., 1] = NAVY[1] + 16 * k * g
    px[..., 2] = NAVY[2] + 42 * k * g
    return Image.fromarray(px.astype(np.uint8))


def _soft_dot(r: int, color: tuple, alpha: int) -> Image.Image:
    s = r * 4
    im = Image.new("RGBA", (s, s), color + (0,))
    a = Image.new("L", (s, s), 0)
    ImageDraw.Draw(a).ellipse((s // 2 - r, s // 2 - r, s // 2 + r, s // 2 + r), fill=alpha)
    im.putalpha(a.filter(ImageFilter.GaussianBlur(max(1, r // 2))))
    return im


def _noise_band(w: int, h: int, seed: int, scale: int, blur: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    small = rng.random((max(2, h // scale), max(2, w // scale))).astype(np.float32)
    im = Image.fromarray((small * 255).astype(np.uint8)).resize((w, h), Image.BICUBIC).filter(ImageFilter.GaussianBlur(blur))
    return np.asarray(im, dtype=np.float32) / 255.0


class _Theme:
    """Static background + a per-frame overlay (drawn behind the circular spectrum)."""

    def __init__(self, n: int, w: int, h: int, logo: Image.Image):
        self.n, self.w, self.h = n, w, h
        self.s = w / 1920.0
        rnd = random.Random(n * 17)
        s = self.s
        self.bg = _base(w, h)
        self.extra = {}
        if n == 1:  # nebula: blurred logo behind, rising particles
            glow = logo.resize((w, w), Image.LANCZOS).crop((0, (w - h) // 2, w, (w - h) // 2 + h)).filter(ImageFilter.GaussianBlur(60 * s))
            self.bg = Image.blend(self.bg, glow, 0.22)
            self.sprites = [_soft_dot(max(2, int(r * s)), c, 255) for r in (3, 5) for c in (CYAN, VIOLET, MAGENTA)]
            self.parts = [(rnd.random() * w, rnd.random() * h, rnd.randrange(len(self.sprites)), rnd.uniform(14, 44) * s, rnd.random() * 6.28) for _ in range(80)]
        elif n == 2:  # bokeh
            self.sprites = [_soft_dot(int(rnd.uniform(40, 110) * s), rnd.choice([CYAN, BLUE, VIOLET, MAGENTA]), rnd.randint(45, 90)) for _ in range(14)]
            self.parts = [(rnd.random() * w, rnd.random() * h, i, rnd.uniform(6, 20) * s, rnd.random() * 6.28) for i in range(14)]
        elif n == 3:  # rotating light rays (12 rays: the picture repeats every 30 degrees)
            big = Image.new("RGBA", (int(w * 1.5), int(w * 1.5)), (0, 0, 0, 0))
            d = ImageDraw.Draw(big)
            c = big.size[0] // 2
            for i in range(12):
                a0 = math.radians(i * 30)
                a1 = a0 + math.radians(9)
                d.polygon([(c, c), (c + math.cos(a0) * c * 1.5, c + math.sin(a0) * c * 1.5), (c + math.cos(a1) * c * 1.5, c + math.sin(a1) * c * 1.5)],
                          fill=_col(i / 12, 34))
            big = big.filter(ImageFilter.GaussianBlur(6 * s))
            self.rays = []
            for k in range(15):
                rot = big.rotate(k * 2.0, resample=Image.BILINEAR)
                ox, oy = (rot.size[0] - w) // 2, (rot.size[1] - h) // 2
                self.rays.append(rot.crop((ox, oy, ox + w, oy + h)))
        elif n == 4:  # synthwave grid
            d = ImageDraw.Draw(self.bg, "RGBA")
            hy = int(h * 0.62)
            for i in range(0, 60):
                f = i / 60
                d.rectangle((0, hy - int(f * 130 * s), w, hy), fill=(120, 70, 255, int(2 + 4 * (1 - f))))
            for i in range(-14, 15):
                d.line((w / 2 + i * 60 * s, hy, w / 2 + i * 340 * s, h), fill=_col((i + 14) / 28, 90), width=max(1, int(2 * s)))
            d.line((0, hy, w, hy), fill=(180, 120, 255, 200), width=max(1, int(3 * s)))
        elif n == 5:  # pulse rings
            self.rings = []
            self.last_ring = -9.0
        elif n == 6:  # stars
            self.stars = [(rnd.random() * w, rnd.random() * h, rnd.uniform(1, 3.2) * s, rnd.random() * 6.28, rnd.choice([CYAN, (255, 255, 255), MAGENTA])) for _ in range(190)]
        elif n == 7:  # aurora: wide picture scrolled slowly
            ww = w * 2
            a = _noise_band(ww, h, 7, 220, 70)
            b = _noise_band(ww, h, 8, 260, 80)
            px = np.zeros((h, ww, 3), dtype=np.float32)
            px[..., 0] = NAVY[0] + 80 * b + 20 * a
            px[..., 1] = NAVY[1] + 150 * a
            px[..., 2] = NAVY[2] + 140 * a + 60 * b
            self.wide = Image.fromarray(np.clip(px, 0, 255).astype(np.uint8))
        elif n == 8:  # lasers
            pass
        elif n == 9:  # dot matrix
            self.cols, self.rows = 48, 27
        elif n == 10:  # floor bars
            self.sprites = [_soft_dot(max(2, int(4 * s)), c, 255) for c in (CYAN, MAGENTA)]
            self.parts = [(rnd.random() * w, rnd.random() * h, rnd.randrange(2), rnd.uniform(10, 28) * s, rnd.random() * 6.28) for _ in range(40)]
        elif n == 11:  # smoke: two noise layers sliding at different speeds
            ww = w * 2
            a = _noise_band(ww, h, 21, 200, 60)
            b = _noise_band(ww, h, 22, 150, 50)
            self.smoke = [Image.fromarray(np.clip(np.stack([(40 + 90 * x) * np.ones_like(x), (30 + 40 * x), (70 + 150 * x)], -1), 0, 255).astype(np.uint8)).convert("RGBA")
                          for x in (a, b)]
            for im in self.smoke:
                im.putalpha(Image.fromarray((np.clip(_noise_band(ww, h, 23, 220, 70), 0, 1) * 60).astype(np.uint8)))

    def draw(self, img: Image.Image, t: float, bass: float, smooth: np.ndarray) -> Image.Image:
        w, h, s, n = self.w, self.h, self.s, self.n
        if n in (1, 10):
            for (x0, y0, si, spd, ph) in self.parts:
                x = (x0 + math.sin(t * 0.4 + ph) * 22 * s) % w
                y = (y0 - t * spd) % h
                sp = self.sprites[si]
                al = 0.35 + 0.5 * (0.5 + 0.5 * math.sin(t * 1.3 + ph)) + 0.4 * bass
                tmp = sp if al >= 0.95 else sp.copy()
                if al < 0.95:
                    tmp.putalpha(sp.getchannel("A").point(lambda v, k=al: int(v * k)))
                img.alpha_composite(tmp, (int(x - sp.size[0] / 2), int(y - sp.size[1] / 2))) if 0 <= x < w - sp.size[0] and 0 <= y < h - sp.size[1] else None
            if n == 10:
                d = ImageDraw.Draw(img, "RGBA")
                nb = len(smooth)
                bw = w / (nb * 2)
                for i in range(nb * 2):
                    v = smooth[min(nb - 1, abs(i - nb) if i >= nb else nb - 1 - i)]
                    bh = 8 * s + v * 230 * s
                    x = i * bw
                    d.rectangle((x + bw * 0.2, h - bh, x + bw * 0.8, h), fill=_col(i / (nb * 2), 120))
        elif n == 2:
            for (x0, y0, si, spd, ph) in self.parts:
                sp = self.sprites[si]
                x = (x0 + math.sin(t * 0.25 + ph) * 60 * s) % (w + sp.size[0]) - sp.size[0] / 2
                y = (y0 - t * spd * 0.6) % (h + sp.size[1]) - sp.size[1] / 2
                sc = 1 + 0.12 * bass
                tmp = sp if sc == 1 else sp.resize((int(sp.size[0] * sc), int(sp.size[1] * sc)))
                px, py = int(x - tmp.size[0] / 2), int(y - tmp.size[1] / 2)
                if -tmp.size[0] < px < w and -tmp.size[1] < py < h:
                    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
                    layer.paste(tmp, (px, py))
                    img.alpha_composite(layer)
        elif n == 3:
            img.alpha_composite(self.rays[int(t * 3) % len(self.rays)])
        elif n == 4:
            d = ImageDraw.Draw(img, "RGBA")
            hy = int(h * 0.62)
            for k in range(8):
                f = ((k + (t * 0.5) % 1.0) / 8) ** 2
                y = hy + f * (h - hy)
                d.line((0, y, w, y), fill=_col(k / 8, 110), width=max(1, int(2 * s)))
        elif n == 5:
            if bass > 0.55 and t - self.last_ring > 0.35:
                self.rings.append(t)
                self.last_ring = t
            d = ImageDraw.Draw(img, "RGBA")
            self.rings = [r for r in self.rings if t - r < 2.2]
            for r0 in self.rings:
                age = t - r0
                rad = 300 * s + age * 520 * s
                d.ellipse((w / 2 - rad, h / 2 - rad, w / 2 + rad, h / 2 + rad), outline=_col((age * 0.5) % 1, int(180 * (1 - age / 2.2))), width=max(1, int(4 * s)))
        elif n == 6:
            d = ImageDraw.Draw(img, "RGBA")
            for (x, y, r, ph, c) in self.stars:
                a = int(60 + 190 * (0.5 + 0.5 * math.sin(t * 2.0 + ph)))
                d.ellipse((x - r, y - r, x + r, y + r), fill=c + (min(255, a + int(60 * bass)),))
        elif n == 7:
            off = int((t * 40 * s) % self.w)
            img.alpha_composite(self.wide.crop((off, 0, off + w, h)).convert("RGBA"), (0, 0)) if False else img.paste(Image.blend(img.convert("RGB"), self.wide.crop((off, 0, off + w, h)), 0.38 + 0.15 * bass).convert("RGBA"))
        elif n == 8:
            d = ImageDraw.Draw(img, "RGBA")
            for k in range(6):
                side = -1 if k % 2 == 0 else 1
                ox = 0 if side == -1 else w
                ang = math.radians(-60 + 120 * (0.5 + 0.5 * math.sin(t * (0.7 + 0.15 * k) + k)))
                ang = (math.pi - ang) if side == 1 else ang
                x1 = ox + math.cos(ang) * w * 1.2
                y1 = h + math.sin(ang) * h * 1.6
                wd = 18 * s
                d.polygon([(ox, h), (x1 - wd, y1), (x1 + wd, y1)], fill=_col(k / 5, int(38 + 50 * bass)))
        elif n == 9:
            d = ImageDraw.Draw(img, "RGBA")
            cw, ch = w / self.cols, h / self.rows
            for i in range(self.cols):
                for j in range(self.rows):
                    x, y = (i + 0.5) * cw, (j + 0.5) * ch
                    dist = math.hypot(x - w / 2, y - h / 2) / (w / 2)
                    wave = 0.5 + 0.5 * math.sin(dist * 9 - t * 3)
                    r = (2 + 4 * wave + 5 * bass * (1 - min(dist, 1))) * s
                    d.ellipse((x - r, y - r, x + r, y + r), fill=_col(i / self.cols, int(70 + 120 * wave)))
        elif n == 11:
            for k, im in enumerate(self.smoke):
                off = int((t * (14 + 22 * k) * s) % self.w)
                img.alpha_composite(im.crop((off, 0, off + w, h)))
        return img

    def background(self, t: float) -> Image.Image:
        if self.n == 7:
            return self.bg.convert("RGBA")
        return self.bg.convert("RGBA")


def _decode(audio: Path, seconds: float, sr: int = 22050) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-t", f"{seconds:.2f}", "-i", str(audio), "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.float32)


class Renderer:
    def __init__(self, theme: int, title: str = "", size=(1920, 1080), fps: int = 24):
        self.w, self.h = size
        self.fps = fps
        self.s = self.w / 1920.0
        self.theme_n = max(1, min(THEME_COUNT, theme))
        logo_src = Image.open(LOGO).convert("RGB")
        self.ls = int(self.h * 0.5)
        logo = logo_src.resize((self.ls, self.ls), Image.LANCZOS).convert("RGBA")
        m = Image.new("L", (self.ls * 4, self.ls * 4), 0)
        ImageDraw.Draw(m).ellipse((0, 0, self.ls * 4 - 1, self.ls * 4 - 1), fill=255)
        logo.putalpha(m.resize((self.ls, self.ls), Image.LANCZOS))
        self.logo = logo
        self.theme = _Theme(self.theme_n, self.w, self.h, logo_src)
        self.pulse = Image.new("L", (int(self.h * 0.95),) * 2, 0)
        ImageDraw.Draw(self.pulse).ellipse((self.pulse.size[0] * 0.2, self.pulse.size[1] * 0.2, self.pulse.size[0] * 0.8, self.pulse.size[1] * 0.8), fill=255)
        self.pulse = self.pulse.filter(ImageFilter.GaussianBlur(70 * self.s))
        self.title = title
        self.nb = 64
        self.bg_rgba = self.theme.background(0)
        self.title_sprite = self._title_sprite()

    def _title_sprite(self):
        if not self.title:
            return None
        try:
            font = ImageFont.truetype(FONT, int(40 * self.s))
        except OSError:
            font = ImageFont.load_default()
        txt = "♪  " + self.title
        tmp = ImageDraw.Draw(Image.new("RGBA", (4, 4)))
        tw = int(tmp.textlength(txt, font=font)) + 8
        th = int(60 * self.s)
        sp = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
        d = ImageDraw.Draw(sp)
        d.text((6, 6), txt, font=font, fill=(0, 0, 0, 170))
        d.text((4, 4), txt, font=font, fill=(255, 255, 255, 240))
        return sp

    def frame(self, t: float, bass: float, smooth: np.ndarray) -> Image.Image:
        w, h, s = self.w, self.h, self.s
        cx, cy = w // 2, h // 2
        img = self.bg_rgba.copy()
        img = self.theme.draw(img, t, bass, smooth)
        # soft pulsing glow behind the logo
        k = 0.35 + 0.65 * bass
        pm = self.pulse.point(lambda v, k=k: int(v * k))
        img.paste((110, 90, 255, 255), (cx - pm.size[0] // 2, cy - pm.size[1] // 2), pm)
        # circular spectrum, mirrored left/right
        R = self.ls * (0.5 + 0.025 * bass) + 22 * s
        reach = int(R + 130 * s) + 16
        box = (cx - reach, cy - reach, cx + reach, cy + reach)
        viz = Image.new("RGBA", (2 * reach, 2 * reach), (0, 0, 0, 0))
        d = ImageDraw.Draw(viz)
        for i in range(self.nb):
            for side in (1, -1):
                ang = -math.pi / 2 + side * (i + 0.5) / self.nb * math.pi
                L = 7 * s + smooth[i] * 112 * s
                x0, y0 = reach + math.cos(ang) * R, reach + math.sin(ang) * R
                x1, y1 = reach + math.cos(ang) * (R + L), reach + math.sin(ang) * (R + L)
                cc = _col((math.cos(ang) + 1) / 2)
                d.line((x0, y0, x1, y1), fill=cc, width=max(2, int(6 * s)))
                rr = 3 * s
                d.ellipse((x1 - rr, y1 - rr, x1 + rr, y1 + rr), fill=cc)
        small = viz.resize((viz.size[0] // 2, viz.size[1] // 2)).filter(ImageFilter.GaussianBlur(5 * s)).resize(viz.size)
        region = img.crop(box)
        region.alpha_composite(small)
        region.alpha_composite(viz)
        sz = int(self.ls * (1 + 0.04 * bass))
        lg = self.logo if sz == self.ls else self.logo.resize((sz, sz), Image.BILINEAR)
        region.alpha_composite(lg, (reach - sz // 2, reach - sz // 2))
        img.paste(region, box[:2])
        if self.title_sprite is not None:
            img.alpha_composite(self.title_sprite, (int((w - self.title_sprite.size[0]) / 2), int(36 * s)))
        return img


def render(audio: Path, out_path: Path, theme: int, title: str = "", size=(1920, 1080), fps: int = 24,
           max_seconds: float | None = None, timeout: int = 3000) -> Path:
    """Render the full video (song + logo + circular spectrum + themed background)."""
    duration = video.probe_duration(audio)
    if max_seconds:
        duration = min(duration, max_seconds)
    rend = Renderer(theme, title, size, fps)
    sr, N = 22050, 2048
    a = _decode(audio, duration + 0.5, sr)
    edges = np.geomspace(40, 9000, rend.nb + 1)
    freqs = np.fft.rfftfreq(N, 1 / sr)
    masks = [((freqs >= edges[i]) & (freqs < edges[i + 1])) for i in range(rend.nb)]
    window = np.hanning(N)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{rend.w}x{rend.h}", "-r", str(fps), "-i", "-",
           "-i", str(audio), "-map", "0:v", "-map", "1:a", "-t", f"{duration:.2f}", "-c:v", "libx264", "-preset", "veryfast",
           "-crf", "23", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out_path)]
    enc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    smooth = np.zeros(rend.nb)
    bass_s = 0.0
    try:
        for fi in range(int(duration * fps)):
            t = fi / fps
            c = int(t * sr)
            seg = a[c:c + N]
            if len(seg) < N:
                seg = np.pad(seg, (0, N - len(seg)))
            sp = np.abs(np.fft.rfft(seg * window))
            lvl = np.array([sp[mk].mean() if mk.any() else 0.0 for mk in masks])
            lvl = np.sqrt(lvl / (lvl.max() + 1e-9)) * (0.4 + 0.6 * min(1.0, float(np.abs(seg).mean()) * 18))
            smooth = np.maximum(lvl, smooth * 0.82)
            bass = min(1.0, float(smooth[:6].mean()) * 1.4)
            bass_s = max(bass, bass_s * 0.86)
            enc.stdin.write(rend.frame(t, bass_s, smooth).tobytes())
        enc.stdin.close()
        rc = enc.wait(timeout=timeout)
    except Exception:
        enc.kill()
        raise
    if rc != 0 or not Path(out_path).exists():
        raise RuntimeError(f"ffmpeg failed with code {rc}")
    return out_path


def cover_frame(out_path: Path, theme: int, title: str = "", size=(1920, 1080)) -> Path:
    """A still of the finished look (used as the YouTube thumbnail / cover)."""
    rend = Renderer(theme, title, size)
    rng = np.random.default_rng(3)
    smooth = 0.25 + 0.65 * rng.random(rend.nb) * np.linspace(1, 0.35, rend.nb)
    rend.frame(2.0, 0.6, smooth).convert("RGB").save(out_path, "PNG")
    return out_path

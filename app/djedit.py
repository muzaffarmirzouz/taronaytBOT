"""A free club edit of a finished song: no AI, no cost, just ffmpeg and numpy.

The song's tempo is measured, nudged towards club speed, and a kick and hat are laid under it in time with the
beat, with the classic sidechain duck on every kick. It is a DJ edit, not a new arrangement: the vocals, the
melody and the chords stay exactly as they were.
"""
from __future__ import annotations

import logging
import subprocess
import tempfile
from pathlib import Path

import numpy as np

from . import video

log = logging.getLogger("djedit")

SR = 44100
STYLES = {
    "club": dict(bpm=126.0, kick=0.75, hat=0.20, duck=0.28, lo=0.92, hi=1.22, label="🪩 Klub"),
    "slow": dict(bpm=92.0, kick=0.55, hat=0.12, duck=0.18, lo=0.85, hi=1.05, label="🌙 Sekin"),
    "hard": dict(bpm=136.0, kick=0.95, hat=0.26, duck=0.38, lo=1.00, hi=1.32, label="🔥 Tez"),
}


def _decode(path: Path, sr: int = SR) -> np.ndarray:
    """The whole song as mono floats."""
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.float32).astype(np.float32)


def estimate_bpm(samples: np.ndarray, sr: int = SR) -> float:
    """Tempo from the rhythm of the loud moments (onset envelope + autocorrelation). 60-180 BPM."""
    hop = 512
    n = len(samples) // hop
    if n < 64:
        return 120.0
    frames = samples[: n * hop].reshape(n, hop)
    energy = np.sqrt((frames.astype(np.float64) ** 2).mean(axis=1))
    onset = np.diff(energy, prepend=energy[0]).clip(min=0)  # only the rises count as hits
    onset -= onset.mean()
    if not np.any(onset):
        return 120.0
    spectrum = np.fft.rfft(onset, n=2 * len(onset))
    corr = np.fft.irfft(spectrum * np.conj(spectrum))[: len(onset)]
    fps = sr / hop
    lo, hi = int(fps * 60 / 180), int(fps * 60 / 60)  # 180 BPM down to 60 BPM
    lo, hi = max(lo, 1), min(hi, len(corr) - 1)
    if hi <= lo:
        return 120.0
    lag = lo + int(np.argmax(corr[lo:hi]))
    bpm = 60.0 * fps / lag
    while bpm < 85:  # half-time readings are common; bring them into dance range
        bpm *= 2
    while bpm > 175:
        bpm /= 2
    return float(bpm)


def _kick(sr: int = SR) -> np.ndarray:
    """One kick drum: a pitch drop from 120 Hz to 45 Hz with a short click on top."""
    t = np.arange(int(0.26 * sr)) / sr
    freq = 45 + 75 * np.exp(-t * 32)
    body = np.sin(2 * np.pi * np.cumsum(freq) / sr) * np.exp(-t * 11)
    click = np.random.default_rng(3).normal(0, 1, len(t)) * np.exp(-t * 420) * 0.25
    return ((body + click) * 0.9).astype(np.float32)


def _hat(sr: int = SR) -> np.ndarray:
    t = np.arange(int(0.05 * sr)) / sr
    noise = np.random.default_rng(11).normal(0, 1, len(t))
    noise = np.diff(noise, prepend=0.0)  # a crude high-pass: keeps only the bright part
    return (noise * np.exp(-t * 150) * 0.5).astype(np.float32)


def _drums(total: int, bpm: float, style: dict, sr: int = SR):
    """The drum track and the ducking envelope that makes the song breathe with it."""
    beat = 60.0 / bpm
    kick, hat = _kick(sr) * style["kick"], _hat(sr) * style["hat"]
    drums = np.zeros(total + len(kick) + 1, dtype=np.float32)
    duck = np.ones(total + len(kick) + 1, dtype=np.float32)
    fall = np.exp(-np.arange(int(beat * 0.9 * sr)) / (0.11 * sr))  # dip, then recover before the next beat
    for i in range(int(total / (beat * sr)) + 1):
        at = int(i * beat * sr)
        drums[at: at + len(kick)] += kick
        duck[at: at + len(fall)] = np.minimum(duck[at: at + len(fall)], 1 - style["duck"] * fall[: len(duck) - at])
        off = at + int(beat * sr / 2)  # hats on the off-beat
        if off + len(hat) < len(drums):
            drums[off: off + len(hat)] += hat
    return drums[:total], duck[:total]


def remix(src: Path, out_path: Path, style: str = "club") -> dict:
    """Club edit of src -> out_path. Returns what was done, for the message back to the owner."""
    cfg = STYLES.get(style, STYLES["club"])
    samples = _decode(src)
    if len(samples) < SR * 10:
        raise ValueError("Audio juda qisqa (kamida 10 soniya kerak)")
    source_bpm = estimate_bpm(samples)
    ratio = float(np.clip(cfg["bpm"] / source_bpm, cfg["lo"], cfg["hi"]))  # beyond this the vocals start to sound wrong
    final_bpm = source_bpm * ratio

    with tempfile.TemporaryDirectory() as tmp:
        stretched = Path(tmp) / "fast.wav"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-ac", "2", "-ar", str(SR),
                        "-filter:a", f"atempo={ratio:.4f}", str(stretched)], check=True, timeout=600)
        raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(stretched), "-ac", "2", "-ar", str(SR), "-f", "f32le", "-"],
                             capture_output=True, check=True).stdout
        song = np.frombuffer(raw, dtype=np.float32).reshape(-1, 2).astype(np.float32)

        drums, duck = _drums(len(song), final_bpm, cfg)
        mixed = song * duck[:, None] + drums[:, None]
        peak = float(np.abs(mixed).max()) or 1.0
        mixed = (mixed / peak * 0.89).astype(np.float32)

        mixed_wav = Path(tmp) / "mixed.wav"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "f32le", "-ar", str(SR), "-ac", "2", "-i", "-",
                        str(mixed_wav)], input=mixed.tobytes(), check=True, timeout=600)
        video.master_audio(mixed_wav, Path(out_path))
    return {"source_bpm": round(source_bpm), "bpm": round(final_bpm), "ratio": round(ratio, 3), "label": cfg["label"]}

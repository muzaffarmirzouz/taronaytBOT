"""Claude writes lyrics, the Mureka style prompt, the image prompt and the
YouTube metadata (English, tuned for a US audience)."""
from __future__ import annotations

import json
import logging
import re

from . import net

log = logging.getLogger("claude")

API_URL = "https://api.anthropic.com/v1/messages"

SYSTEM = """You are the creative engine behind an English-language AI music channel on YouTube aimed at a US audience.
You write original songs and the metadata to publish them. Reply with ONE JSON object and nothing else."""

SCHEMA_HELP = """Return a JSON object with exactly these keys:
- "title": song title, max 45 characters.
- "theme": 2-4 word label of the song's topic (used internally to avoid repeats).
- "lyrics": full song lyrics in ENGLISH, at most 1800 characters, structured with section tags on their own line
  like [Verse], [Pre-Chorus], [Chorus], [Bridge], [Outro]. Singable: consistent syllable counts, clean rhymes,
  a memorable repeated hook in the chorus. Original wording only.
- "style": music style prompt for the song generator, max 220 characters: genre, mood, tempo/BPM, vocal type
  (e.g. warm male vocal / airy female vocal), 3-4 instruments. NEVER mention real artists, bands or songs.
  The song must get to the vocals FAST: include "very short 4-bar intro, vocals start within 10 seconds" in the style.
  The lyrics must start directly with the first verse or the chorus: NO [Intro] tag and no instrumental opening section.
- "image_prompt": a vivid prompt for a 3:2 landscape illustration that visualises THIS song's theme and story: one clear
  scene built from concrete places, objects, weather, light and mood named in the lyrics (not a generic backdrop). Vary
  the art style from song to song (cinematic photo, watercolor, anime-style landscape, neon synthwave, oil painting...). It must contain no text, letters, logos or watermarks and no real people or celebrities.
- "yt_title": YouTube title, max 90 characters. Format like "<Song Title> (Official Lyric Video) | <Genre/Mood> Song".
  Natural, appealing to US listeners, no clickbait lies.
- "yt_description": 2-3 short paragraphs (what the song feels like, who it is for), then a line "Lyrics:" followed by
  the full lyrics. Max 3000 characters in total.
- "yt_tags": array of 12-18 search-friendly English tags (mix of broad and specific, e.g. genre, mood, "new song 2026",
  "lyric video"). Each tag at most 30 characters."""


def _extract_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in Claude reply")
    return json.loads(text[start:end + 1])


def _validate(data: dict) -> dict:
    required = ["title", "lyrics", "style", "image_prompt", "yt_title", "yt_description", "yt_tags"]
    for key in required:
        if not data.get(key):
            raise ValueError(f"missing field: {key}")
    if "[" not in data["lyrics"]:
        raise ValueError("lyrics have no section tags")
    tags = data["yt_tags"]
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",")]
    data["yt_tags"] = [str(t).strip() for t in tags if str(t).strip()]
    data["title"] = str(data["title"]).strip()[:50]
    data["lyrics"] = _no_intro(str(data["lyrics"]).strip())[:4800]
    data["style"] = _fast_start(str(data["style"]).strip())
    data["yt_title"] = str(data["yt_title"]).strip()[:100]
    data["yt_description"] = str(data["yt_description"]).strip()[:4000]
    data["theme"] = str(data.get("theme", "")).strip()[:60]
    return data


FAST_START = "short 4-bar intro, vocals start within 10 seconds"


def _fast_start(style: str, limit: int = 290) -> str:
    """Songs were opening with a minute of music: always ask the generator for an immediate vocal entrance."""
    if "vocals start" not in style.lower():
        style = style[: limit - len(FAST_START) - 2].rstrip(" ,.;") + ", " + FAST_START
    return style[:limit]


def _no_intro(lyrics: str) -> str:
    """Drop [Intro]/[Instrumental] sections at the start so the vocals come in right away."""
    import re
    out = re.sub(r"^\s*\[(?:intro|instrumental|music|build[- ]?up)[^\]]*\][^\[]*", "", lyrics, flags=re.I)
    return out.strip() or lyrics


# key -> (button label, instruction for the music style)
GENRES = {
    "club": ("🎧 Klub / Dance", "club dance track, four-on-the-floor EDM beat, big catchy vocal hook, 124-128 BPM, euphoric and energetic"),
    "remix": ("🔥 Remix / EDM", "party remix style, energetic EDM dance-pop with a big bass drop, punchy synths, 126 BPM"),
    "afro": ("🌴 Afro House", "afro house / amapiano groove, warm percussion and log-drum bass, danceable and sunny, 118-124 BPM"),
    "pop": ("🎉 Sho'x Pop", "upbeat playful pop, bright and cheerful, handclaps, catchy chorus, 110-120 BPM"),
    "trap": ("🎤 Hip-Hop / Trap", "trap / hip-hop with deep 808 bass and hi-hats, sung-rap vocal hook, confident and bouncy, 135-145 BPM"),
    "ballad": ("💞 Ballada", "emotional slow ballad, piano and strings, heartfelt vocal, 70-80 BPM"),
    "any": ("🎲 Tasodifiy sho'x", "any upbeat, energetic and danceable genre you think fits this text best"),
}

UZBEK_BRIEF = (
    "The owner chose UZBEK as the song language. Write the song in UZBEK (Latin script, natural modern Uzbek), "
    "NOT in English. Keep the owner's own words, lines, imagery and story as much as possible: only lightly edit for "
    "rhythm and rhyme, give it song structure with section tags, and build the chorus from its strongest line. "
    "Do not translate it into English. Field rules for this song: \"title\" is a short Uzbek title; \"lyrics\" are "
    "Uzbek; \"style\" stays in English for the music generator and MUST include 'Uzbek-language vocals'; "
    "\"image_prompt\" stays in English; \"yt_title\" stays English-friendly, like '<Uzbek Title> (Official Lyric "
    "Video) | Uzbek <Genre> Song'; \"yt_description\" starts with 1-2 English paragraphs (say it is an Uzbek-language "
    "song from Central Asia and give a one-sentence English summary of what it says), then 'Lyrics:' followed by the "
    "Uzbek lyrics; \"yt_tags\" stay English but must include 'uzbek song', 'uzbek music', 'central asian music'."
)


def build_user_message(raw_text: str, theme: str, avoid_titles: list, language: str = "en", genre: str = "") -> str:
    parts = []
    if raw_text.strip() and language == "uz":
        parts.append(
            "The channel owner supplied this text (a poem or lyrics).\n" + UZBEK_BRIEF +
            "\n\n--- TEXT ---\n" + raw_text.strip() + "\n--- END ---"
        )
    elif raw_text.strip():
        parts.append(
            "The channel owner supplied this text (it may be a poem or lyrics in any language, e.g. Uzbek). "
            "Turn it into an ENGLISH song: keep its meaning, mood, story and imagery, but rewrite it so it sings "
            "naturally in English with rhyme and rhythm (do not translate word for word). Add a chorus built from its "
            "strongest line if it has none.\n\n--- TEXT ---\n" + raw_text.strip() + "\n--- END ---"
        )
    else:
        genre, _, topic = (theme or "").partition("|")
        parts.append(
            "Write a brand-new original song.\n"
            f"Genre/vibe: {genre.strip() or 'your choice'}\n"
            f"Topic: {topic.strip() or genre.strip() or 'your choice'}"
        )
    if avoid_titles:
        parts.append("Do NOT reuse or closely echo these recent titles: " + "; ".join(avoid_titles[:25]))
    if genre in GENRES and raw_text.strip():
        parts.append(
            f"GENRE requested by the owner: {GENRES[genre][1]}. The \"style\" field must clearly describe this genre "
            "and the lyrics must fit its energy. (If the owner's text contains a line starting with 'Janr:' or "
            "'Uslub:', that line has priority over this genre.)"
        )
    schema = SCHEMA_HELP
    if language == "uz":
        schema = schema.replace("full song lyrics in ENGLISH", "full song lyrics in UZBEK (Latin script)")
    parts.append(schema)
    return "\n\n".join(parts)


def make_package(cfg, raw_text: str = "", theme: str = "", avoid_titles: list | None = None,
                 language: str = "en", genre: str = "") -> dict:
    headers = {
        "x-api-key": cfg.anthropic_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = {
        "model": cfg.claude_model,
        "max_tokens": 4000,
        "system": SYSTEM,
        "messages": [{"role": "user", "content": build_user_message(raw_text, theme, avoid_titles or [], language, genre)}],
    }
    last_err = None
    for attempt in range(3):
        resp = net.request("POST", API_URL, headers=headers, json=body, timeout=120)
        if resp.status_code != 200:
            raise RuntimeError(f"Claude API {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        try:
            return _validate(_extract_json(text))
        except (ValueError, json.JSONDecodeError) as exc:
            last_err = exc
            log.warning("Claude reply invalid (attempt %s): %s", attempt + 1, exc)
    raise RuntimeError(f"Claude returned invalid JSON 3 times: {last_err}")

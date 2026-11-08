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
  (e.g. deep gritty male vocal / full low alto female vocal; never soft or airy), 3-4 instruments. NEVER mention real artists, bands or songs.
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


def _validate(data: dict, voice: str = "") -> dict:
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
    style = str(data["style"]).strip()
    if voice:
        style = _set_vocal(style, voice, limit=290 - len(FAST_START) - 2)
    data["style"] = _fast_start(style)
    data["yt_title"] = str(data["yt_title"]).strip()[:100]
    data["yt_description"] = str(data["yt_description"]).strip()[:4000]
    data["theme"] = str(data.get("theme", "")).strip()[:60]
    return data


FAST_START = "short 4-bar intro, vocals start within 10 seconds, professional studio mix"


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
    "club": ("🎧 Klub / Dance",
             "club house / dance-pop, 124-128 BPM, four-on-the-floor punchy kick, rolling sidechained bassline, bright plucky synth stabs, "
             "filtered build-up riser into a big euphoric drop, catchy vocal hook, wide polished club mix"),
    "remix": ("🔥 Remix / EDM",
              "festival EDM remix, 126-128 BPM, energetic build-up with snare roll, huge bass drop, supersaw leads, sidechain pump, "
              "chopped vocal hook, crisp hi-hats, loud modern club master"),
    "afro": ("🌴 Afro House",
             "afro house / amapiano, 118-122 BPM, deep log-drum bass, shakers and congas, warm pads, smooth airy vocal chants, "
             "hypnotic groove with an energetic percussion break, sunny and danceable, clean polished mix"),
    "pop": ("🎉 Sho'x Pop",
            "upbeat dance-pop, 112-120 BPM, tight bouncy drums, handclaps, bright synths and plucks, funky bass, "
            "huge singalong chorus with stacked vocal harmonies, glossy radio-ready production"),
    "trap": ("🎤 Hip-Hop / Trap",
             "modern trap, 138-142 BPM, deep 808 bass, rolling hi-hats with triplets, dark bell melody, confident melodic rap-sung hook, "
             "ad-libs, hard-hitting punchy drums, clean heavy mix"),
    "rap": ("🎙 Rep",
            "modern rap, 90-96 BPM, hard punchy boom-bap and trap drums, deep sub bass, dark piano or guitar loop, "
            "confident rhythmic rap verses with sharp flow and internal rhymes, a short melodic sung hook, gritty polished mix"),
    "ballad": ("💞 Ballada",
               "cinematic emotional pop ballad, 70-76 BPM, intimate piano, warm strings, soft building drums, "
               "expressive heartfelt vocal with a powerful final chorus, rich spacious studio mix"),
    "any": ("🎲 Tasodifiy sho'x",
            "any upbeat, energetic and danceable genre you think fits this text best, with modern polished studio production"),
}

# lyric-structure rules per genre: what makes the SONG sound like that genre, not just the style tag
GENRE_LYRIC_RULES = {
    "club": "CLUB STRUCTURE: short punchy lines (4-7 words), hypnotic repetition, a very simple chant-like hook repeated many times. "
            "Use this order: [Verse] (4 short lines) [Pre-Chorus] (build-up, rising energy, 2-4 lines) [Chorus] (hook, repeat the key line) "
            "[Drop] (a short line or just 'oh-oh / hey' chant, mostly instrumental) [Verse] [Pre-Chorus] [Chorus] [Drop] [Outro]. "
            "Total length compact: about 900-1300 characters.",
    "remix": "EDM STRUCTURE: very short lines, a big shout-along hook. Order: [Verse] (4 lines) [Build-up] (2-4 lines getting faster, "
             "e.g. repeating one word) [Drop] (chant of 1-2 lines) [Verse] [Build-up] [Drop] [Chorus] [Drop]. Keep it around 900-1200 characters.",
    "afro": "AFRO HOUSE STRUCTURE: relaxed rhythmic lines with call-and-response, repeated chant phrases and short hum parts "
            "(ooh / yeah / na-na). Order: [Verse] [Chorus] [Verse] [Chorus] [Bridge] (percussion break, chant only) [Chorus]. 1000-1400 characters.",
    "pop": "DANCE-POP STRUCTURE: [Verse] [Pre-Chorus] [Chorus] [Verse] [Pre-Chorus] [Chorus] [Bridge] [Chorus]. Playful, singable lines, "
           "a huge repeated chorus hook. 1200-1600 characters.",
    "trap": "TRAP STRUCTURE: rhythmic sung-rap verses with internal rhymes and ad-libs in brackets, a short melodic hook repeated. "
            "Order: [Hook] [Verse] [Hook] [Verse] [Bridge] [Hook]. 1200-1600 characters.",
    "rap": "RAP STRUCTURE: the verses are real RAPPED lines (not sung): 8-12 lines each, dense rhythm, internal rhymes and multi-syllable "
           "rhymes, short bracketed ad-libs, storytelling or boasting with concrete details. Order: [Hook] (4 lines, sung, catchy) "
           "[Verse] [Hook] [Verse] [Bridge] [Hook]. 1400-1900 characters.",
    "ballad": "BALLAD STRUCTURE: [Verse] [Pre-Chorus] [Chorus] [Verse] [Pre-Chorus] [Chorus] [Bridge] [Final Chorus]. Vivid imagery, "
              "slow phrasing, emotional peak in the bridge.",
}


VOICES = {
    "male": ("👨 Erkak ovozi", "deep powerful male lead vocal, low rich baritone, strong chest voice, slightly raspy and gritty, "
                              "full-bodied and assertive, not soft or breathy"),
    "female": ("👩 Ayol ovozi", "full-bodied female lead vocal, low rich alto, strong chest voice, powerful and soulful, "
                                "confident and mature, not thin, not airy"),
    "duet": ("👫 Duet", "male and female duet vocals, trading lines and harmonising on the chorus"),
}


def pick_voice(weights=(0.7, 0.25, 0.05)) -> str:
    """Random voice for automatic songs: mostly male, as the channel owner prefers."""
    import random
    return random.choices(["male", "female", "duet"], weights=weights)[0]


def _set_vocal(style: str, voice: str, limit: int = 290) -> str:
    """Replace any vocal descriptor in the style prompt with the chosen voice."""
    import re
    if voice not in VOICES:
        return style
    style = re.sub(r"\b(?:(?:warm|airy|soft|bright|smooth|deep|powerful|breathy|sweet|silky|raspy|gritty|soulful|"
                   r"melodic|confident)\s+)*(?:male|female|man|woman|girl|boy)(?:\s*(?:and|&|/)\s*(?:male|female))?"
                   r"(?:\s+(?:lead|rap|sung|backing))?\s+(?:vocals?|voice|singer|duet)\b", "", style, flags=re.I)
    style = re.sub(r"\s*,\s*,+", ",", style).strip(" ,;")
    voice_text = VOICES[voice][1]
    return (style[: limit - len(voice_text) - 2].rstrip(" ,.;") + ", " + voice_text)[:limit]


def genre_key(text: str) -> str:
    """Map a free-text genre/theme (auto songs) to one of the GENRES keys, '' if unknown."""
    t = (text or "").lower()
    for key, words in (("remix", ("remix", "edm", "festival", "bass drop")),
                       ("afro", ("afro", "amapiano")),
                       ("club", ("club", "house", "dance", "disco", "techno", "electro")),
                       ("trap", ("trap",)),
                       ("rap", ("rap", "hip-hop", "hip hop")),
                       ("ballad", ("ballad", "slow")),
                       ("pop", ("pop",))):
        if any(w in t for w in words):
            return key
    return ""


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


TURKISH_BRIEF = (
    "The owner chose TURKISH as the song language. The supplied text may be in Uzbek or another language: write the song "
    "in natural, modern, singable TURKISH (Istanbul Turkish, correct Turkish letters ç ğ ı ö ş ü), NOT in English. "
    "Keep the owner's meaning, story, feelings and imagery and as many of its strongest lines as translate well; "
    "adapt wording for Turkish rhythm and rhyme (do not translate word for word), give it song structure with section "
    "tags, and build the chorus from its strongest line. Field rules for this song: \"title\" is a short Turkish title; "
    "\"lyrics\" are Turkish; \"style\" stays in English for the music generator and MUST include 'Turkish-language vocals'; "
    "\"image_prompt\" stays in English; \"yt_title\" like '<Turkish Title> (Official Lyric Video) | Turkish <Genre> Song'; "
    "\"yt_description\" starts with 1-2 English paragraphs (say it is a Turkish-language song and give a one-sentence "
    "English summary of what it says), then 'Lyrics:' followed by the Turkish lyrics; \"yt_tags\" stay English but must "
    "include 'turkish song', 'turkish music', 'türkçe şarkı'."
)

LANG_BRIEFS = {"uz": UZBEK_BRIEF, "tr": TURKISH_BRIEF}
LANG_NAMES = {"uz": ("O'zbekcha", "UZBEK (Latin script)"), "tr": ("Turkcha", "TURKISH"), "en": ("Inglizcha", "ENGLISH")}


def build_user_message(raw_text: str, theme: str, avoid_titles: list, language: str = "en", genre: str = "",
                       voice: str = "") -> str:
    parts = []
    if raw_text.strip() and language in LANG_BRIEFS:
        parts.append(
            "The channel owner supplied this text (a poem or lyrics).\n" + LANG_BRIEFS[language] +
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
        if language == "tr":
            parts.append(
                "Write this song in natural, modern, singable TURKISH (correct letters ç ğ ı ö ş ü), NOT in English. "
                "\"title\" is a short Turkish title; \"lyrics\" are Turkish; \"style\" stays in English and MUST include "
                "'Turkish-language vocals'; \"image_prompt\" stays in English; \"yt_title\" like '<Turkish Title> | Turkish "
                "<Genre> Song'; \"yt_description\" starts with 1-2 English paragraphs (it is a Turkish-language song, one "
                "English sentence on what it says), then 'Lyrics:' and the Turkish lyrics; \"yt_tags\" stay English and "
                "include 'turkish song', 'turkish music', 'türkçe şarkı'.")
    if avoid_titles:
        parts.append("Do NOT reuse or closely echo these recent titles: " + "; ".join(avoid_titles[:25]))
    rule_key = genre if genre in GENRES else genre_key((theme or "").partition("|")[0])
    if rule_key in GENRE_LYRIC_RULES:
        parts.append("LYRICS FORMAT FOR THIS GENRE (overrides the generic structure above): " + GENRE_LYRIC_RULES[rule_key] +
                     " Never add an [Intro] section.")
    if genre in GENRES and raw_text.strip():
        parts.append(
            f"GENRE requested by the owner: {GENRES[genre][1]}. The \"style\" field must clearly describe this genre "
            "and the lyrics must fit its energy. (If the owner's text contains a line starting with 'Janr:' or "
            "'Uslub:', that line has priority over this genre.)"
        )
    if voice in VOICES:
        parts.append(f"VOCALS requested by the owner: {VOICES[voice][1]}. The \"style\" must describe exactly this vocal "
                     "(do not mention another singer gender).")
    schema = SCHEMA_HELP
    if language in LANG_BRIEFS:
        schema = schema.replace("full song lyrics in ENGLISH", "full song lyrics in " + LANG_NAMES[language][1])
    parts.append(schema)
    return "\n\n".join(parts)


def make_package(cfg, raw_text: str = "", theme: str = "", avoid_titles: list | None = None,
                 language: str = "en", genre: str = "", voice: str = "") -> dict:
    headers = {
        "x-api-key": cfg.anthropic_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = {
        "model": cfg.claude_model,
        "max_tokens": 4000,
        "system": SYSTEM,
        "messages": [{"role": "user", "content": build_user_message(raw_text, theme, avoid_titles or [], language, genre, voice)}],
    }
    last_err = None
    for attempt in range(3):
        resp = net.request("POST", API_URL, headers=headers, json=body, timeout=120)
        if resp.status_code != 200:
            raise RuntimeError(f"Claude API {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        try:
            return _validate(_extract_json(text), voice)
        except (ValueError, json.JSONDecodeError) as exc:
            last_err = exc
            log.warning("Claude reply invalid (attempt %s): %s", attempt + 1, exc)
    raise RuntimeError(f"Claude returned invalid JSON 3 times: {last_err}")


META_SCHEMA = """Return a JSON object with exactly these keys:
- "title": song title, max 45 characters. If the owner gave a title use it exactly.
- "image_prompt": vivid prompt for a 3:2 landscape illustration matching the song's mood/theme (no text, letters, logos,
  watermarks, no real people or celebrities).
- "yt_title": YouTube title, max 90 characters, like "<Song Title> | <Genre/Mood> Song". Natural, no clickbait lies.
- "yt_description": 2 short paragraphs about the feel of the song and who it is for. If the owner pasted lyrics, add a line
  "Lyrics:" followed by them. Max 3000 characters in total.
- "yt_tags": array of 12-18 search-friendly English tags, each at most 30 characters."""


def make_meta(cfg, hint: str = "", title: str = "", avoid_titles: list | None = None) -> dict:
    """Metadata for a FINISHED track the owner uploaded (Claude cannot hear it, so it works from the owner's notes)."""
    parts = ["The owner uploaded a finished song (audio). You cannot hear it; write the YouTube metadata from the notes below. "
             "Do not invent facts about the singer or claim specific lyrics unless pasted."]
    if title:
        parts.append(f"Song title given by the owner: {title}")
    parts.append("Owner's notes (genre / mood / story / lyrics, any language): " + (hint.strip()[:3000] or "(none: keep it generic: an upbeat modern song)"))
    if avoid_titles:
        parts.append("Do not reuse these titles: " + "; ".join(avoid_titles[:25]))
    parts.append(META_SCHEMA)
    headers = {"x-api-key": cfg.anthropic_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    body = {"model": cfg.claude_model, "max_tokens": 3000, "system": SYSTEM,
            "messages": [{"role": "user", "content": "\n\n".join(parts)}]}
    last_err = None
    for attempt in range(3):
        resp = net.request("POST", API_URL, headers=headers, json=body, timeout=120)
        if resp.status_code != 200:
            raise RuntimeError(f"Claude API {resp.status_code}: {resp.text[:300]}")
        text = "".join(b.get("text", "") for b in resp.json().get("content", []) if b.get("type") == "text")
        try:
            data = _extract_json(text)
            for key in ("title", "image_prompt", "yt_title", "yt_description", "yt_tags"):
                if not data.get(key):
                    raise ValueError(f"missing field: {key}")
            tags = data["yt_tags"]
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.split(",")]
            data["yt_tags"] = [str(t).strip()[:30] for t in tags if str(t).strip()][:18]
            data["title"] = (title or str(data["title"])).strip()[:50]
            data["yt_title"] = str(data["yt_title"]).strip()[:95]
            data["yt_description"] = str(data["yt_description"]).strip()[:3000]
            return data
        except (ValueError, json.JSONDecodeError) as exc:
            last_err = exc
            log.warning("Claude meta reply invalid (attempt %s): %s", attempt + 1, exc)
    raise RuntimeError(f"Claude returned invalid JSON 3 times: {last_err}")

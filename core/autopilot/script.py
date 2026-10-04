"""
Narration scripts for archive videos.

Guardrails (docs/COMPLIANCE.md section 5):
  - Fact safety: the model may only use facts from the item's own title,
    description and original narration transcript.
  - Originality: scripts too similar to recent ones are rejected and rewritten,
    and recent hooks are passed in so openings don't repeat.
  - Tone: curious and respectful; no sensational or distressing framing.
"""
import difflib
import json
import random
import re

from core.autopilot import llm
from core.autopilot.sources import SENSITIVE
from db.database import get_conn

WORDS_PER_SEC = 2.8  # measured pace of the LibriTTS voice at length_scale 1.15

ANGLES = [
    "what everyday life looked like",
    "what surprised audiences at the time",
    "what changed afterwards",
    "the small details that are easy to miss in the footage",
    "how people at the time talked about it",
    "how it compares with today",
]

EPISODE_PROMPT = """You write narration for a history channel that brings old public-domain newsreels back to life.

SOURCE (the ONLY facts you may use):
Title: {title}
Date: {date}
Archive description: {description}
Original newsreel narration (machine transcript, may contain errors): {transcript}

RULES
- Use only facts stated in the SOURCE. Never invent names, numbers, places, dates or quotes.
  If the source is thin, describe what viewers are seeing and what the newsreel told audiences, framed as such
  ("the newsreel told audiences...", "in this footage...").
- Tone: warm, curious, documentary. Respectful about war, disasters and suffering — never sensational, graphic, or
  written to shock. No clickbait claims the video doesn't deliver.
- Speak to the viewer naturally. Short sentences that read well aloud. No emojis, hashtags or stage directions.
- Do not start any script with "Imagine", "Picture this", "In {year}," or "Did you know".
- This episode's angle: {angle}.
- Avoid these recently used hooks: {recent_hooks}

WRITE (return ONLY a JSON object with these keys):
- "hook": on-screen opening line, max 8 words, intriguing but accurate.
- "title_short": YouTube Shorts title, max 70 characters, accurate, no hashtags.
- "segment_title": chapter title for a compilation, max 45 characters.
- "script_short": narration of about {short_words} words for a 50-second vertical video. First sentence must hook.
- "script_tiktok": a DIFFERENT narration of about {tiktok_words} words for a 70-second video. Different opening and structure.
- "script_segment": narration of about {segment_words} words that plays over the full {footage_secs}-second clip in a longer documentary.
- "description": 2-3 sentences describing the video for viewers. Accurate, no hashtags.
- "tags": 5-10 lowercase search tags (strings).
"""

COMPILATION_PROMPT = """You are titling a documentary-style YouTube video made of narrated public-domain newsreels.

Chapters, in order:
{chapters}

Return ONLY a JSON object with:
- "title": YouTube title, max 90 characters, accurate, no clickbait, no hashtags.
- "intro": spoken intro of about 35 words that welcomes viewers and previews the chapters. Don't start with "Imagine".
- "outro": spoken outro of about 30 words that thanks viewers and invites them to subscribe for more history from the archive.
- "description": 2-3 sentences describing the video. No hashtags.
- "tags": 5-10 lowercase search tags.
"""


def words(text: str) -> list:
    return re.findall(r"[A-Za-z0-9']+", (text or "").lower())


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, words(a), words(b)).ratio()


def recent_scripts(limit: int = 40) -> list:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT hook, script_short, script_tiktok FROM archive_episodes "
            "WHERE script_short IS NOT NULL ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _clean(text: str) -> str:
    text = re.sub(r"[#*_`~<>\[\]{}]", "", str(text or ""))
    text = re.sub(r"\([^)]*(music|sound|pause|narrator)[^)]*\)", "", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip()


def validate_episode(data: dict, targets: dict, recent: list) -> list:
    """Return a list of problems; empty means the script passes."""
    problems = []
    for key in ("hook", "title_short", "segment_title", "script_short", "script_tiktok",
                "script_segment", "description"):
        if not str(data.get(key) or "").strip():
            problems.append(f"missing {key}")
    if problems:
        return problems
    for key, target in targets.items():
        n = len(words(data[key]))
        if not (0.65 * target <= n <= 1.4 * target):
            problems.append(f"{key} has {n} words, wanted about {target}")
    all_text = " ".join(str(data[k]) for k in ("hook", "title_short", "script_short", "script_tiktok", "script_segment"))
    if re.search(r"\bas an ai\b|\bi cannot\b|\bi can't help\b", all_text, re.I):
        problems.append("model refusal text")
    if SENSITIVE.search(data["hook"] + " " + data["title_short"]):
        problems.append("sensational hook/title")
    if similarity(data["script_short"], data["script_tiktok"]) > 0.6:
        problems.append("short and tiktok scripts are too similar")
    for r in recent:
        if (r.get("hook") or "").strip().lower() == data["hook"].strip().lower():
            problems.append("hook repeats a recent video")
        if similarity(data["script_short"], r.get("script_short") or "") > 0.5:
            problems.append("script too similar to a recent video")
            break
    return problems


def write_episode(source: dict, transcript: str, footage_secs: float) -> dict:
    recent = recent_scripts()
    seg_secs = max(20.0, footage_secs * 0.85)
    targets = {
        "script_short": 130,
        "script_tiktok": 195,
        "script_segment": int(seg_secs * WORDS_PER_SEC),
    }
    year = (source.get("date") or "")[:4] or "the past"
    base = EPISODE_PROMPT.format(
        title=source.get("title") or "",
        date=source.get("date") or "unknown",
        description=(source.get("description") or "(none)")[:2500],
        transcript=(transcript or "(no narration — silent footage)")[:4000],
        year=year,
        angle=random.choice(ANGLES),
        recent_hooks=json.dumps([r["hook"] for r in recent[:15] if r.get("hook")]),
        short_words=targets["script_short"],
        tiktok_words=targets["script_tiktok"],
        segment_words=targets["script_segment"],
        footage_secs=int(footage_secs),
    )
    problems = []
    for attempt in range(3):
        prompt = base
        if problems:
            prompt += "\nYOUR PREVIOUS ATTEMPT WAS REJECTED FOR: " + "; ".join(problems) + ". Fix these.\n"
        data = llm.complete_json(prompt, max_tokens=3500, temperature=0.75 + 0.1 * attempt)
        for k in ("hook", "title_short", "segment_title", "script_short", "script_tiktok",
                  "script_segment", "description"):
            data[k] = _clean(data.get(k))
        tags = data.get("tags") or []
        data["tags"] = [_clean(t).lower()[:30] for t in tags if isinstance(t, str) and _clean(t)][:10]
        problems = validate_episode(data, targets, recent)
        if not problems:
            data["title_short"] = data["title_short"][:70]
            data["segment_title"] = data["segment_title"][:45]
            data["hook"] = " ".join(data["hook"].split()[:8])
            return data
    raise RuntimeError("Script failed quality checks: " + "; ".join(problems))


def write_compilation(chapters: list) -> dict:
    listing = "\n".join(f"{i+1}. {c['segment_title']} ({c.get('date') or 'date unknown'})"
                        for i, c in enumerate(chapters))
    try:
        data = llm.complete_json(COMPILATION_PROMPT.format(chapters=listing), max_tokens=800)
    except RuntimeError:
        data = {}
    years = sorted({(c.get("date") or "")[:4] for c in chapters if (c.get("date") or "")[:4]})
    span = f"{years[0]}–{years[-1]}" if len(years) > 1 else (years[0] if years else "the archive")
    title = _clean(data.get("title")) or f"Newsreels from {span}: {len(chapters)} stories from the archive"
    return {
        "title": title[:90],
        "intro": _clean(data.get("intro")) or f"Welcome back to the archive. Today we're watching {len(chapters)} newsreels from {span}, and the stories they told audiences at the time.",
        "outro": _clean(data.get("outro")) or "Thanks for watching. If you enjoyed this trip through the archive, subscribe for more history, one newsreel at a time.",
        "description": _clean(data.get("description")) or f"{len(chapters)} narrated newsreels from {span}.",
        "tags": [_clean(t).lower()[:30] for t in (data.get("tags") or []) if isinstance(t, str)][:10],
    }

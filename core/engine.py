"""
ClipForge — Core Engine
Pipeline:
  Step 1: Download (proxy + cookies support)
  Step 2: Transcribe (faster-whisper local)
  Step 3: Detect Moments (AI via Groq/OpenAI/Claude, keyword fallback)
  Step 4: Cut Clips (face-tracking crop via OpenCV YuNet, center-crop fallback)
  Step 5: Burn Captions (karaoke style)
  Step 6: Hook text overlay
  Step 7: Apply Watermark (text-based, no PNG)
  Split Mode: Sequential parts for archive content
"""
import os
import re
import json
import subprocess
import tempfile
import time
from pathlib import Path
from config import CLIPS_DIR, UPLOADS_DIR, WATERMARKS_DIR, STATIC_DIR, VERSION
from db.database import log, set_step
from core.reframe import face_track_filter

WATERMARK_FONT = "Archivo Black"
HOOK_FONT = "Anton"

# ─── Constants ────────────────────────────────────────────────────────────

ASS_COLORS = {
    "white":   "&H00FFFFFF",
    "yellow":  "&H0000FFFF",
    "cyan":    "&H00FFFF00",
    "magenta": "&H00FF00FF",
    "orange":  "&H000080FF",
    "red":     "&H000000FF",
    "black":   "&H00000000",
    "green":   "&H0000FF00",
}

PLAY_RES = {"9:16": (1080, 1920), "16:9": (1920, 1080), "1:1": (1080, 1080)}

CAPTION_PRESETS = {
    "bold":     (1, 1, 5, 2, ""),
    "outlined": (1, 1, 6, 0, ""),
    "shadow":   (1, 1, 3, 4, ""),
    "minimal":  (0, 1, 2, 1, ""),
    "box":      (1, 3, 0, 0, ""),
    "karaoke":  (1, 1, 5, 2, ""),
}

DEFAULT_FONT_SIZE = {"9:16": 82, "16:9": 52, "1:1": 66}
MAX_WORDS = {"9:16": 4, "16:9": 5, "1:1": 4}

POSITION_MAP = {
    "bottom": {"9:16": (2, 120),  "16:9": (2, 60),  "1:1": (2, 80)},
    "middle": {"9:16": (2, 850),  "16:9": (2, 460),  "1:1": (2, 460)},
    "top":    {"9:16": (8, 80),   "16:9": (8, 50),   "1:1": (8, 60)},
}

# AI moment detection prompt — adapted from yt-short-clipper approach
MOMENT_DETECTION_PROMPT = """You are an expert short-form video editor who identifies the most viral, engaging segments.

Analyze this transcript and identify the {num_clips} most engaging segments for short-form content.

PRIORITY — find segments with:
1. Conflict, tension, controversy, or drama
2. Personal confessions or vulnerability  
3. Bold opinions or surprising statements
4. Punchlines or strong humor
5. Complete story arcs (setup → payoff)
6. Memorable quotes that stand alone

AVOID:
- Filler words and transitions
- Technical explanations without emotion
- Incomplete thoughts

DURATION RULES (CRITICAL):
- Each clip MUST be 30–90 seconds
- Target 45–60 seconds ideally
- Calculate from transcript timestamps — do NOT estimate

Return ONLY a JSON array. No markdown. No explanation. Exactly {num_clips} items.

Each item MUST have exactly these fields:
- "start": number (seconds, from transcript)
- "end": number (seconds, from transcript)  
- "score": integer 1-100 (virality score)
- "hook": string (max 10 words — attention-grabbing hook text)
- "transcript": string (the spoken text in this segment)

Transcript:
{transcript}"""


# ─── STEP 1: Download ─────────────────────────────────────────────────────

def step1_download(url: str, job_id: int) -> str:
    set_step(job_id, "Downloading video...", 10)
    log(job_id, f"Step 1 — Downloading: {url}")
    out_dir = UPLOADS_DIR / str(job_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = str(out_dir / "source.%(ext)s")

    # Write cookies from environment variable if set
    cookies_path = None
    cookies_content = os.environ.get("YOUTUBE_COOKIES", "")
    if cookies_content:
        cookies_path = str(out_dir / "cookies.txt")
        with open(cookies_path, "w") as f:
            f.write(cookies_content)

    cmd = [
        "yt-dlp",
        "--format", "bestvideo[height<=1080]+bestaudio/best[height<=1080]/bestvideo+bestaudio/best",
        "--merge-output-format", "mp4",
        "--output", out_path,
        "--no-playlist",
        "--no-warnings",
    ]

    # Proxy support — env var: PROXY_URL=http://user:pass@host:port
    proxy_url = os.environ.get("PROXY_URL", "")
    if proxy_url:
        cmd += ["--proxy", proxy_url]
        log(job_id, f"Using proxy: {proxy_url.split('@')[-1]}")

    if cookies_path:
        cmd += ["--cookies", cookies_path]

    cmd.append(url)

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        err = result.stderr.strip()
        log(job_id, f"Download failed: {err[:300]}", "error")
        if "Sign in to confirm" in err or "429" in err or "bot" in err.lower():
            raise RuntimeError("YouTube blocked the download. Cookies may have expired — update YOUTUBE_COOKIES in Railway variables.")
        elif "Private video" in err:
            raise RuntimeError("This video is private.")
        elif "not available" in err:
            raise RuntimeError("Video not available in your region.")
        elif "Unsupported URL" in err:
            raise RuntimeError("URL not supported. Try YouTube, Instagram, Facebook, or TikTok.")
        else:
            raise RuntimeError(f"Download failed: {err[:300]}")

    files = list(out_dir.glob("source.*"))
    if not files:
        raise RuntimeError("Download succeeded but no file found.")

    size_mb = files[0].stat().st_size // 1024 // 1024
    log(job_id, f"Step 1 complete — {files[0].name} ({size_mb}MB)")
    set_step(job_id, f"Downloaded ({size_mb}MB)", 25)
    return str(files[0])


# ─── STEP 2: Transcribe ───────────────────────────────────────────────────

def step2_transcribe(video_path: str, job_id: int, whisper_model: str = "base") -> list:
    """
    Transcribe using faster-whisper locally.
    whisper_model: 'base' (fast, good), 'medium' (slow, better accuracy)
    Falls back gracefully if transcription fails.
    """
    set_step(job_id, "Transcribing audio...", 30)
    log(job_id, "Step 2 — Extracting audio...")

    audio_path = video_path.rsplit(".", 1)[0] + "_audio.wav"
    r = subprocess.run([
        "ffmpeg", "-i", video_path, "-vn", "-ar", "16000", "-ac", "1",
        "-acodec", "pcm_s16le", audio_path, "-y", "-loglevel", "quiet"
    ], capture_output=True, timeout=180)

    if r.returncode != 0 or not os.path.exists(audio_path):
        log(job_id, "Audio extraction failed — no captions will be available", "warn")
        set_step(job_id, "Transcription skipped", 55)
        return []

    log(job_id, f"Audio extracted — loading Whisper {whisper_model} model...")
    set_step(job_id, f"Loading Whisper {whisper_model}...", 35)

    segments = []

    # Try faster-whisper
    try:
        from faster_whisper import WhisperModel
        set_step(job_id, "Transcribing speech...", 40)
        model = WhisperModel(whisper_model, device="cpu", compute_type="int8")
        segments_iter, info = model.transcribe(
            audio_path,
            beam_size=3,
            word_timestamps=True,
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=500),
        )
        for seg in segments_iter:
            words = [{"word": w.word.strip(), "start": w.start, "end": w.end}
                     for w in (seg.words or [])]
            segments.append({
                "start": seg.start, "end": seg.end,
                "text": seg.text.strip(), "words": words
            })
            if len(segments) % 20 == 0:
                log(job_id, f"Transcribing... {len(segments)} segments")
                set_step(job_id, f"Transcribing... {len(segments)} segments", 45)
        log(job_id, f"Whisper {whisper_model} complete — {len(segments)} segments, lang: {info.language}")
    except Exception as e:
        log(job_id, f"Transcription error: {str(e)[:100]}", "warn")

    if os.path.exists(audio_path):
        os.remove(audio_path)

    if segments:
        set_step(job_id, f"Transcribed — {len(segments)} segments", 55)
    else:
        log(job_id, "No segments transcribed — clips will have no captions", "warn")
        set_step(job_id, "Transcription skipped", 55)

    return segments


# ─── STEP 3: Detect Moments (AI-powered) ─────────────────────────────────

def step3_detect_moments(video_path: str, segments: list, job_id: int,
                          num_clips: int = 5) -> list:
    """
    Detect best moments using AI (Groq free → OpenAI → keyword fallback).
    Groq is free, fast, and high quality — perfect default.
    Falls back to keyword scoring if no AI API is configured.
    """
    set_step(job_id, "Detecting best moments...", 60)
    log(job_id, "Step 3 — AI moment detection...")

    duration = get_duration(video_path)
    if duration == 0:
        raise RuntimeError("Could not read video duration.")

    log(job_id, f"Video duration: {int(duration // 60)}m {int(duration % 60)}s")

    # Build transcript text with timestamps
    if not segments:
        log(job_id, "No transcript — using time-based fallback moments", "warn")
        return _fallback_moments(duration, num_clips)

    transcript_text = _build_transcript_with_timestamps(segments)

    # Try AI detection — Groq first (free), then OpenAI, then Claude
    groq_key = os.environ.get("GROQ_API_KEY", "")
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")

    moments = None

    if groq_key:
        log(job_id, "Using Groq for moment detection (free + fast)...")
        moments = _detect_moments_groq(transcript_text, num_clips, groq_key, job_id)

    if not moments and openai_key:
        log(job_id, "Trying OpenAI for moment detection...")
        moments = _detect_moments_openai(transcript_text, num_clips, openai_key, job_id)

    if not moments and anthropic_key:
        log(job_id, "Trying Claude for moment detection...")
        moments = _detect_moments_claude(transcript_text, num_clips, anthropic_key, job_id)

    if not moments:
        log(job_id, "No AI key configured — using keyword scoring fallback", "warn")
        moments = _keyword_detect_moments(video_path, segments, job_id, num_clips)

    # Validate moments against actual video duration
    valid = []
    for m in moments:
        if m["start"] < 0 or m["end"] > duration + 5 or m["start"] >= m["end"]:
            continue
        m["end"] = min(m["end"], duration)
        valid.append(m)

    if not valid:
        log(job_id, "AI moments invalid — falling back to keyword scoring", "warn")
        valid = _keyword_detect_moments(video_path, segments, job_id, num_clips)

    log(job_id, f"Step 3 complete — {len(valid)} moments (top score: {valid[0]['score'] if valid else 0})")
    set_step(job_id, f"Found {len(valid)} moments", 75)
    return valid[:num_clips]


def _build_transcript_with_timestamps(segments: list) -> str:
    """Build transcript string with timestamps for AI consumption."""
    lines = []
    for seg in segments:
        start = seg["start"]
        m, s = int(start // 60), start % 60
        lines.append(f"[{m:02d}:{s:05.2f}] {seg['text'].strip()}")
    return "\n".join(lines)


def _parse_ai_moments(raw: str, job_id: int) -> list:
    """Parse AI response into moments list. Handles common JSON issues."""
    # Strip markdown code blocks if present
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"```[a-z]*\n?", "", raw).strip().rstrip("```").strip()

    try:
        data = json.loads(raw)
        if not isinstance(data, list):
            log(job_id, f"AI returned non-list: {type(data)}", "warn")
            return []
        moments = []
        for item in data:
            if not isinstance(item, dict):
                continue
            start = float(item.get("start", 0))
            end = float(item.get("end", 0))
            if end <= start or end - start < 15:
                continue
            moments.append({
                "start": round(start, 2),
                "end": round(end, 2),
                "score": int(item.get("score", 75)),
                "transcript": str(item.get("transcript", "")),
                "hook": str(item.get("hook", "")),
                "segments": [],
            })
        return moments
    except Exception as e:
        log(job_id, f"Failed to parse AI response: {e} — raw: {raw[:200]}", "warn")
        return []


def _detect_moments_groq(transcript: str, num_clips: int, api_key: str, job_id: int) -> list:
    """Call Groq API for moment detection. Free, fast, high quality."""
    try:
        import urllib.request
        prompt = MOMENT_DETECTION_PROMPT.format(
            num_clips=num_clips,
            transcript=transcript[:12000]  # Groq context limit
        )
        payload = json.dumps({
            "model": "llama-3.3-70b-versatile",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "max_tokens": 2000,
        }).encode()

        req = urllib.request.Request(
            "https://api.groq.com/openai/v1/chat/completions",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        content = data["choices"][0]["message"]["content"]
        moments = _parse_ai_moments(content, job_id)
        if moments:
            log(job_id, f"Groq detected {len(moments)} moments")
        return moments
    except Exception as e:
        log(job_id, f"Groq failed: {str(e)[:100]}", "warn")
        return []


def _detect_moments_openai(transcript: str, num_clips: int, api_key: str, job_id: int) -> list:
    """Call OpenAI API for moment detection."""
    try:
        import urllib.request
        prompt = MOMENT_DETECTION_PROMPT.format(
            num_clips=num_clips,
            transcript=transcript[:15000]
        )
        payload = json.dumps({
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "max_tokens": 2000,
        }).encode()

        req = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        content = data["choices"][0]["message"]["content"]
        moments = _parse_ai_moments(content, job_id)
        if moments:
            log(job_id, f"OpenAI detected {len(moments)} moments")
        return moments
    except Exception as e:
        log(job_id, f"OpenAI failed: {str(e)[:100]}", "warn")
        return []


def _detect_moments_claude(transcript: str, num_clips: int, api_key: str, job_id: int) -> list:
    """Call Anthropic Claude API for moment detection."""
    try:
        import urllib.request
        prompt = MOMENT_DETECTION_PROMPT.format(
            num_clips=num_clips,
            transcript=transcript[:15000]
        )
        payload = json.dumps({
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 2000,
            "messages": [{"role": "user", "content": prompt}],
        }).encode()

        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        content = data["content"][0]["text"]
        moments = _parse_ai_moments(content, job_id)
        if moments:
            log(job_id, f"Claude detected {len(moments)} moments")
        return moments
    except Exception as e:
        log(job_id, f"Claude failed: {str(e)[:100]}", "warn")
        return []


def _keyword_detect_moments(video_path: str, segments: list, job_id: int, num_clips: int = 5) -> list:
    """
    Keyword-based moment detection — fallback when no AI key is configured.
    Same logic as v5.x for backward compatibility.
    """
    duration = get_duration(video_path)
    moments = []
    window, step = 45.0, 15.0
    t = 0.0

    while t + window <= duration:
        segs = [s for s in segments if s["start"] >= t and s["end"] <= t + window]
        transcript = " ".join(s["text"] for s in segs)
        tl = transcript.lower()
        word_count = sum(len(s["text"].split()) for s in segs)

        speech_density = min(35, word_count // 3)
        hooks = ["why", "how", "what if", "the truth", "never", "always",
                 "secret", "biggest mistake", "most people", "this is why",
                 "stop", "wait", "listen", "let me tell you"]
        hook_score = min(20, sum(6 for h in hooks if h in tl))
        energy = ["incredible", "insane", "crazy", "unbelievable", "shocked",
                  "amazing", "important", "money", "success", "failed", "quit",
                  "love", "hate", "fear", "angry", "excited"]
        energy_score = min(15, sum(4 for e in energy if e in tl))
        question_score = min(10, transcript.count("?") * 4)
        silence_penalty = -15 if word_count < 20 else 0
        fillers = ["um", "uh", "like", "you know", "basically"]
        filler_penalty = -min(10, sum(tl.count(f) for f in fillers) * 2)

        score = max(0, min(100,
            30 + speech_density + hook_score + energy_score +
            question_score + silence_penalty + filler_penalty
        ))
        moments.append({
            "start": round(t, 2),
            "end": round(min(t + window, duration), 2),
            "score": score,
            "transcript": transcript,
            "hook": "",
            "segments": segs,
        })
        t += step

    moments.sort(key=lambda x: x["score"], reverse=True)

    # Deduplicate — keep moments at least 20s apart
    top = []
    for m in moments[:25]:
        if not any(abs(m["start"] - k["start"]) < 20 for k in top):
            top.append(m)
        if len(top) >= num_clips:
            break

    return top


def _fallback_moments(duration: float, num_clips: int) -> list:
    """Time-based fallback when no transcript is available."""
    clip_len = min(45.0, duration / max(num_clips, 1))
    moments = []
    for i in range(num_clips):
        start = i * (duration / num_clips)
        moments.append({
            "start": round(start, 2),
            "end": round(min(start + clip_len, duration), 2),
            "score": 70,
            "transcript": "",
            "hook": "",
            "segments": [],
        })
    return moments


# ─── STEP 4: Cut Clips ────────────────────────────────────────────────────

def _cut_cmd(video_path: str, start: float, duration: float, vf: str, out_path: str) -> list:
    return [
        "ffmpeg", "-y",
        "-ss", str(start),
        "-i", video_path,
        "-t", str(duration),
        "-vf", vf,
        "-c:v", "libx264", "-preset", "fast", "-crf", "22",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        "-loglevel", "error",
        out_path,
    ]


def cut_clip(video_path: str, start: float, duration: float, fmt: str, out_path: str,
             job_id: int = 0, face_track: bool = True, zoom_punch: bool = False) -> bool:
    """Cut one clip, trying face-tracked crop → zoom → plain crop, best first."""
    crop = get_crop(fmt)
    base_vfs = []
    if face_track and fmt in ("9:16", "1:1"):
        tracked = face_track_filter(video_path, start, duration, fmt)
        if tracked:
            base_vfs.append(tracked)
            if job_id:
                log(job_id, "Face tracking: following the speaker")
    base_vfs.append(crop)

    attempts = []
    for vf in base_vfs:
        if zoom_punch:
            res = {"9:16": "1080x1920", "16:9": "1920x1080", "1:1": "1080x1080"}.get(fmt, "1080x1920")
            attempts.append(
                f"{vf},zoompan=z='if(lte(on,9),1.0+on*0.004,1.04)':"
                f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={res}:fps=30"
            )
        attempts.append(vf)

    last_err = ""
    for vf in attempts:
        r = subprocess.run(_cut_cmd(video_path, start, duration, vf, out_path), capture_output=True)
        if r.returncode == 0 and os.path.exists(out_path):
            return True
        last_err = r.stderr.decode()[:200]
        if job_id:
            log(job_id, f"Cut attempt failed ({last_err[:80]}) — trying simpler filter", "warn")
    if job_id:
        log(job_id, f"Clip cut failed: {last_err}", "error")
    return False


def relative_segments(segments: list, clip_start: float, clip_end: float) -> list:
    """Segments overlapping [clip_start, clip_end], re-timed to start at 0, with words."""
    out = []
    dur = clip_end - clip_start
    for seg in segments:
        if seg["end"] < clip_start or seg["start"] > clip_end:
            continue
        words = []
        for w in seg.get("words", []):
            ws, we = w["start"] - clip_start, w["end"] - clip_start
            if we < 0 or ws > dur:
                continue
            words.append({"word": w["word"], "start": round(max(0.0, ws), 3), "end": round(min(dur, we), 3)})
        out.append({
            "text": seg.get("text", ""),
            "start": round(max(0.0, seg["start"] - clip_start), 3),
            "end": round(min(dur, seg["end"] - clip_start), 3),
            "words": words,
        })
    return out


def step4_cut_clips(video_path: str, moments: list, job_id: int,
                    fmt: str, zoom_punch: bool = False, face_track: bool = True) -> list:
    set_step(job_id, "Cutting clips...", 78)
    log(job_id, f"Step 4 — Cutting {len(moments)} clips in {fmt} format...")
    out_dir = CLIPS_DIR / str(job_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    results = []

    for i, m in enumerate(moments):
        clip_path = str(out_dir / f"clip_{i+1:02d}_raw.mp4")
        duration = m["end"] - m["start"]
        log(job_id, f"Cutting clip {i+1} at {int(m['start'])}s — {int(duration)}s (score: {m['score']})")

        if not cut_clip(video_path, m["start"], duration, fmt, clip_path,
                        job_id=job_id, face_track=face_track, zoom_punch=zoom_punch):
            continue

        results.append({
            "clip_path": clip_path,
            "start": m["start"], "end": m["end"],
            "duration": duration, "score": m["score"],
            "transcript": m["transcript"],
            "hook": m.get("hook", ""),
            "segments": m.get("segments", []),
            "format": fmt,
        })
        pct = 78 + int((i + 1) / len(moments) * 7)
        set_step(job_id, f"Cutting clip {i+1} of {len(moments)}", pct)

    log(job_id, f"Step 4 complete — {len(results)} clips cut")
    set_step(job_id, f"Cut {len(results)} clips", 85)
    return results


# ─── STEP 5: Burn Captions ───────────────────────────────────────────────

def build_hook_ass(text: str, fmt: str, duration: float = 2.8, font: str = None,
                   y_frac: float = 0.42) -> str:
    """Hook text as an ASS subtitle — wraps and centers properly, unlike drawtext."""
    res_x, res_y = PLAY_RES.get(fmt, (1080, 1920))
    size = {"9:16": 104, "1:1": 88, "16:9": 72}.get(fmt, 88)
    margin = int(res_x * 0.08)
    clean = re.sub(r"[{}\\]", "", text or "").strip().upper()
    if not clean:
        return None
    end = f"0:00:{duration:05.2f}"
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: {res_x}
PlayResY: {res_y}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Hook,{font or HOOK_FONT},{size},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,6,3,5,{margin},{margin},0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 1,0:00:00.00,{end},Hook,,0,0,0,,{{\\pos({res_x // 2},{int(res_y * y_frac)})\\fad(150,250)}}{clean}
"""


def watermark_filter(text_file: str, font: str = WATERMARK_FONT) -> str:
    return (
        f"drawtext=textfile='{text_file}':font='{font}':fontsize=38:"
        f"fontcolor=white:x=w-tw-25:y=25:"
        f"shadowx=2:shadowy=2:shadowcolor=black@1.0:"
        f"borderw=2:bordercolor=black@0.9"
    )


def _encode_with_filters(in_path: str, out_path: str, filters: list) -> bool:
    r = subprocess.run([
        "ffmpeg", "-y", "-i", in_path,
        "-vf", ",".join(filters),
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-c:a", "copy", "-movflags", "+faststart",
        "-loglevel", "error", out_path
    ], capture_output=True)
    return r.returncode == 0 and os.path.exists(out_path)


def step5_burn_captions(clips: list, video_path: str, all_segments: list,
                         job_id: int, font: str = "Bebas Neue",
                         text_color: str = "white", outline_color: str = "black",
                         preset: str = "karaoke", font_size: int = None,
                         position: str = "bottom", captions: bool = True,
                         hooks: bool = False, watermark_text: str = None,
                         highlight_color: str = "yellow") -> list:
    """Finish each clip in ONE encode: captions + hook + watermark.
    If the combined pass fails, retry with fewer layers so a clip is never lost."""
    set_step(job_id, "Burning captions...", 86)
    log(job_id, f"Step 5 — Finishing clips (captions:{captions} hooks:{hooks} "
                f"watermark:{bool(watermark_text)} font:{font})")
    results = []

    for i, clip in enumerate(clips):
        raw_path = clip["clip_path"]
        stem = raw_path.rsplit(".", 1)[0]
        final_path = stem + "_final.mp4"
        ass_path = stem + ".ass"
        hook_path = stem + "_hook.ass"
        wm_path = stem + "_wm.txt"
        thumb_path = stem + "_thumb.jpg"
        fmt = clip["format"]

        buf_start = max(0, clip["start"] - 5)
        buf_end = clip["end"] + 5
        buf_segs = [s for s in all_segments if s["start"] >= buf_start and s["end"] <= buf_end]
        clip["segments_json"] = json.dumps(relative_segments(buf_segs, clip["start"], clip["end"]))

        layers = []  # (name, filter) in burn order
        if captions:
            if preset == "karaoke":
                ass_content = build_ass_karaoke(buf_segs, clip["start"], clip["end"], fmt, font,
                                                outline_color, font_size, position, highlight_color)
            else:
                ass_content = build_ass(buf_segs, clip["start"], clip["end"], fmt, font, text_color,
                                        outline_color, preset, font_size, position)
            if ass_content:
                with open(ass_path, "w", encoding="utf-8") as f:
                    f.write(ass_content)
                layers.append(("captions", f"ass='{ass_path}'"))
            else:
                log(job_id, f"No transcript for clip {i+1} — no captions", "warn")
        if hooks:
            hook_text = clip.get("hook", "").strip() or _extract_hook_from_transcript(clip.get("transcript", ""))
            hook_ass = build_hook_ass(hook_text, fmt) if hook_text else None
            if hook_ass:
                with open(hook_path, "w", encoding="utf-8") as f:
                    f.write(hook_ass)
                layers.append(("hook", f"ass='{hook_path}'"))
        if watermark_text:
            with open(wm_path, "w", encoding="utf-8") as f:
                f.write(watermark_text)
            layers.append(("watermark", watermark_filter(wm_path)))

        done = not layers
        # Try all layers, then drop the least important ones until it works.
        for keep in range(len(layers), 0, -1):
            if _encode_with_filters(raw_path, final_path, [f for _, f in layers[:keep]]):
                if keep < len(layers):
                    log(job_id, f"Clip {i+1}: skipped {', '.join(n for n, _ in layers[keep:])} (render error)", "warn")
                os.remove(raw_path)
                done = True
                break
        if not layers:
            os.rename(raw_path, final_path)
        elif not done:
            log(job_id, f"Clip {i+1}: overlays failed — keeping plain cut", "warn")
            os.rename(raw_path, final_path)
        for tmp in (ass_path, hook_path, wm_path):
            if os.path.exists(tmp):
                os.remove(tmp)

        clip["clip_path"] = final_path
        subprocess.run([
            "ffmpeg", "-y", "-ss", "2", "-i", final_path,
            "-vframes", "1", "-q:v", "2", "-loglevel", "quiet", thumb_path
        ], capture_output=True)
        clip["thumbnail_path"] = thumb_path if os.path.exists(thumb_path) else None

        results.append(clip)
        pct = 86 + int((i + 1) / len(clips) * 12)
        set_step(job_id, f"Finished {i+1}/{len(clips)}", pct)

    log(job_id, "Step 5 complete")
    set_step(job_id, "Captions complete", 98)
    return results


# ─── STEP 6: Hook Generation (NEW) ───────────────────────────────────────

def step6_add_hooks(clips: list, job_id: int, all_segments: list) -> list:
    """
    Add AI-generated hook intro to each clip.
    Creates a 3-second text overlay intro using the hook text from AI detection.
    Falls back gracefully if no hook text is available.
    Only runs if GROQ_API_KEY or OPENAI_API_KEY is set.
    """
    set_step(job_id, "Adding hooks...", 92)
    log(job_id, "Step 6 — Adding hook intros...")
    results = []

    for i, clip in enumerate(clips):
        hook_text = clip.get("hook", "").strip()
        if not hook_text:
            # Generate hook from transcript using simple extraction
            hook_text = _extract_hook_from_transcript(clip.get("transcript", ""))

        if hook_text:
            hooked_path = _burn_hook_text(clip["clip_path"], hook_text, clip["format"], job_id)
            if hooked_path and os.path.exists(hooked_path):
                clip["clip_path"] = hooked_path
                log(job_id, f"Hook added to clip {i+1}: '{hook_text[:40]}'")
            else:
                log(job_id, f"Hook generation failed clip {i+1} — keeping without hook", "warn")
        else:
            log(job_id, f"No hook text for clip {i+1} — skipping", "warn")

        results.append(clip)

    set_step(job_id, "Hooks complete", 93)
    return results


def _extract_hook_from_transcript(transcript: str) -> str:
    """Extract a punchy hook from the first sentence of transcript."""
    if not transcript:
        return ""
    # Take first 8-10 words
    words = transcript.strip().split()
    hook = " ".join(words[:8])
    # Clean up
    hook = re.sub(r"[^A-Za-z0-9' ]", "", hook).strip().upper()
    return hook if len(hook) > 5 else ""


def _burn_hook_text(video_path: str, hook_text: str, fmt: str, job_id: int) -> str:
    """Burn a wrapped, centered hook for the first ~2.8 seconds."""
    out_path = video_path.rsplit(".", 1)[0] + "_hooked.mp4"
    hook_ass = build_hook_ass(hook_text, fmt)
    if not hook_ass:
        return None
    ass_path = video_path.rsplit(".", 1)[0] + "_hook.ass"
    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(hook_ass)
    ok = _encode_with_filters(video_path, out_path, [f"ass='{ass_path}'"])
    os.remove(ass_path)
    if ok:
        if os.path.exists(video_path):
            os.remove(video_path)
        return out_path
    return None


# ─── STEP 7: Apply Watermark ─────────────────────────────────────────────

def step7_apply_watermark(clips: list, job_id: int,
                           watermark_text: str = "ClipForge",
                           watermark_font: str = WATERMARK_FONT) -> list:
    """
    Burn text watermark onto each clip.
    White text, dark border + shadow, top right. No PNG — no issues ever.
    """
    set_step(job_id, "Applying watermark...", 97)
    log(job_id, f"Step 7 — Text watermark: '{watermark_text}'")
    results = []

    for i, clip in enumerate(clips):
        in_path = clip["clip_path"]
        stem = in_path.rsplit(".", 1)[0]
        out_path = stem + "_wm.mp4"
        thumb_path = stem + "_thumb.jpg"

        success = apply_text_watermark(in_path, out_path, watermark_text, watermark_font)

        if success and os.path.exists(out_path):
            if os.path.exists(in_path): os.remove(in_path)
            clip["clip_path"] = out_path
            log(job_id, f"Watermark applied to clip {i+1}")
            subprocess.run([
                "ffmpeg", "-y", "-ss", "2", "-i", out_path,
                "-vframes", "1", "-q:v", "2", "-loglevel", "quiet", thumb_path
            ], capture_output=True)
            if os.path.exists(thumb_path):
                clip["thumbnail_path"] = thumb_path
        else:
            log(job_id, f"Watermark failed clip {i+1} — keeping without watermark", "warn")

        results.append(clip)

    set_step(job_id, "Watermark complete", 99)
    return results

# Keep backward-compatible alias
step6_apply_watermark = step7_apply_watermark


# ─── Package Mode ─────────────────────────────────────────────────────────

def step_package_mode(video_path: str, moments: list, segments: list,
                      job_id: int, font: str = "Bebas Neue",
                      text_color: str = "white", outline_color: str = "black",
                      highlight_color: str = "yellow", font_size: int = None,
                      position: str = "bottom",
                      apply_watermark: bool = True,
                      watermark_text: str = "ClipForge",
                      watermark_font: str = WATERMARK_FONT,
                      demo_mode: bool = False,
                      add_hooks: bool = False) -> list:
    """
    Package mode — top 3 moments × 16:9 = 3 clips.
    demo_mode: caps clips at 20 seconds for prospect pitches.
    add_hooks: prepend hook text overlay if available.
    """
    mode_label = "Demo" if demo_mode else "Package"
    set_step(job_id, f"Creating {mode_label} — top 3 moments, 16:9...", 78)
    log(job_id, f"{mode_label} mode — 3 clips, 16:9 format")

    top3 = moments[:3]
    fmt = "16:9"
    out_dir = CLIPS_DIR / str(job_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    results = []
    total = len(top3)

    for m_idx, moment in enumerate(top3):
        count = m_idx + 1
        buf_start = max(0, moment["start"] - 5)
        buf_end = moment["end"] + 5
        buf_segs = [s for s in segments if s["start"] >= buf_start and s["end"] <= buf_end]
        duration = min(moment["end"] - moment["start"], 20) if demo_mode else moment["end"] - moment["start"]
        label = f"Demo Clip {count}" if demo_mode else f"Moment {count}"
        slug = label.replace(" ", "_")
        raw_path = str(out_dir / f"pkg_{count:02d}_{slug}_raw.mp4")
        cap_path = str(out_dir / f"pkg_{count:02d}_{slug}_cap.mp4")
        final_path = str(out_dir / f"pkg_{count:02d}_{slug}_final.mp4")
        thumb_path = str(out_dir / f"pkg_{count:02d}_{slug}_thumb.jpg")

        # Cut clip
        crop = get_crop(fmt)
        r = subprocess.run([
            "ffmpeg", "-y",
            "-ss", str(moment["start"]),
            "-i", video_path,
            "-t", str(duration),
            "-vf", crop,
            "-c:v", "libx264", "-preset", "fast", "-crf", "22",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            "-loglevel", "error",
            raw_path
        ], capture_output=True)

        if r.returncode != 0:
            log(job_id, f"Package clip {count} cut failed", "error")
            continue

        # Burn captions
        ass_content = build_ass_karaoke(
            buf_segs, moment["start"], moment["end"],
            fmt, font, outline_color, font_size, position, highlight_color
        )
        work_path = raw_path
        if ass_content:
            ass_path = raw_path.replace("_raw.mp4", ".ass")
            with open(ass_path, "w", encoding="utf-8") as f:
                f.write(ass_content)
            r2 = subprocess.run([
                "ffmpeg", "-y", "-i", raw_path,
                "-vf", f"ass={ass_path}",
                "-c:v", "libx264", "-preset", "fast", "-crf", "22",
                "-c:a", "copy", "-movflags", "+faststart",
                "-loglevel", "error", cap_path
            ], capture_output=True)
            if r2.returncode == 0:
                if os.path.exists(raw_path): os.remove(raw_path)
                if os.path.exists(ass_path): os.remove(ass_path)
                work_path = cap_path

        # Add hook overlay if requested
        if add_hooks and moment.get("hook"):
            hooked = _burn_hook_text(work_path, moment["hook"], fmt, job_id)
            if hooked:
                work_path = hooked

        # Apply watermark
        if apply_watermark:
            wm_out = work_path.replace(".mp4", "_wm.mp4")
            success = apply_text_watermark(work_path, wm_out, watermark_text, watermark_font)
            if success:
                if os.path.exists(work_path): os.remove(work_path)
                work_path = wm_out

        if work_path != final_path:
            os.rename(work_path, final_path)

        subprocess.run([
            "ffmpeg", "-y", "-ss", "2", "-i", final_path,
            "-vframes", "1", "-q:v", "2", "-loglevel", "quiet", thumb_path
        ], capture_output=True)

        chunks = relative_segments(buf_segs, moment["start"], moment["start"] + duration)

        results.append({
            "clip_path": final_path,
            "thumbnail_path": thumb_path if os.path.exists(thumb_path) else None,
            "start": moment["start"], "end": moment["end"],
            "duration": duration, "score": moment["score"],
            "transcript": moment["transcript"],
            "hook": moment.get("hook", ""),
            "segments_json": json.dumps(chunks),
            "format": fmt, "label": label,
        })

        pct = 78 + int(count / total * 21)
        set_step(job_id, f"Package {count}/{total}: {label}", pct)
        log(job_id, f"Package clip {count} done: {label}")

    log(job_id, f"Package complete — {len(results)} clips")
    set_step(job_id, f"Package ready — {len(results)} clips", 99)
    return results


# ─── Split Mode (NEW) ─────────────────────────────────────────────────────

def step_split_mode(video_path: str, segments: list, job_id: int,
                    fmt: str = "16:9", clip_duration: int = 60,
                    apply_watermark_flag: bool = True,
                    watermark_text: str = "ClipForge",
                    watermark_font: str = WATERMARK_FONT,
                    font: str = "Bebas Neue",
                    outline_color: str = "black",
                    font_size: int = None,
                    position: str = "bottom") -> list:
    """
    Sequential split mode for archive/long-form content.
    Cuts video into equal parts (default 60s each), numbered Part 1, Part 2...
    Perfect for retro content channels — post parts sequentially.
    Captions + watermark applied to each part.
    """
    set_step(job_id, f"Splitting video into {clip_duration}s parts...", 78)
    log(job_id, f"Split mode — {clip_duration}s parts, {fmt} format")

    total_duration = get_duration(video_path)
    if total_duration == 0:
        raise RuntimeError("Could not read video duration.")

    out_dir = CLIPS_DIR / str(job_id)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Keep a trailing partial part unless it's just a few seconds of tail.
    total_parts = int(total_duration // clip_duration)
    if total_duration - total_parts * clip_duration >= 10 or total_parts == 0:
        total_parts += 1

    log(job_id, f"Video is {int(total_duration)}s — creating {total_parts} parts of {clip_duration}s")
    results = []

    for part_num in range(1, total_parts + 1):
        start = (part_num - 1) * clip_duration
        end = min(part_num * clip_duration, total_duration)
        duration = end - start
        label = f"Part {part_num}"
        raw_path = str(out_dir / f"split_{part_num:03d}_raw.mp4")
        cap_path = str(out_dir / f"split_{part_num:03d}_cap.mp4")
        final_path = str(out_dir / f"split_{part_num:03d}_final.mp4")
        thumb_path = str(out_dir / f"split_{part_num:03d}_thumb.jpg")

        log(job_id, f"Cutting Part {part_num}/{total_parts} ({int(start)}s-{int(end)}s)")

        # Cut
        if not cut_clip(video_path, start, duration, fmt, raw_path, job_id=job_id):
            log(job_id, f"Split part {part_num} cut failed", "error")
            continue

        # Burn captions for this segment
        buf_segs = [s for s in segments if s["start"] >= start - 2 and s["end"] <= end + 2]
        work_path = raw_path

        if buf_segs:
            ass_content = build_ass_karaoke(buf_segs, start, end, fmt, font, outline_color, font_size, position)
            if ass_content:
                ass_path = raw_path.replace("_raw.mp4", ".ass")
                with open(ass_path, "w", encoding="utf-8") as f:
                    f.write(ass_content)
                r2 = subprocess.run([
                    "ffmpeg", "-y", "-i", raw_path,
                    "-vf", f"ass={ass_path}",
                    "-c:v", "libx264", "-preset", "fast", "-crf", "22",
                    "-c:a", "copy", "-movflags", "+faststart",
                    "-loglevel", "error", cap_path
                ], capture_output=True)
                if r2.returncode == 0:
                    if os.path.exists(raw_path): os.remove(raw_path)
                    if os.path.exists(ass_path): os.remove(ass_path)
                    work_path = cap_path

        # Watermark
        if apply_watermark_flag:
            wm_out = work_path.replace(".mp4", "_wm.mp4")
            if apply_text_watermark(work_path, wm_out, watermark_text, watermark_font):
                if os.path.exists(work_path): os.remove(work_path)
                work_path = wm_out

        if work_path != final_path:
            os.rename(work_path, final_path)

        subprocess.run([
            "ffmpeg", "-y", "-ss", "2", "-i", final_path,
            "-vframes", "1", "-q:v", "2", "-loglevel", "quiet", thumb_path
        ], capture_output=True)

        transcript = " ".join(s["text"] for s in buf_segs)
        chunks = relative_segments(buf_segs, start, end)

        results.append({
            "clip_path": final_path,
            "thumbnail_path": thumb_path if os.path.exists(thumb_path) else None,
            "start": start, "end": end,
            "duration": duration, "score": 75,
            "transcript": transcript,
            "hook": "",
            "segments_json": json.dumps(chunks),
            "format": fmt, "label": label,
        })

        pct = 78 + int(part_num / total_parts * 21)
        set_step(job_id, f"Split {part_num}/{total_parts}", pct)

    log(job_id, f"Split complete — {len(results)} parts")
    set_step(job_id, f"Split ready — {len(results)} parts", 99)
    return results


# ─── Caption Builders ─────────────────────────────────────────────────────

def split_segments_into_chunks(segments: list, clip_start: float, clip_end: float, fmt: str) -> list:
    max_w = MAX_WORDS.get(fmt, 5)
    clip_dur = clip_end - clip_start
    chunks = []

    for seg in segments:
        ss = seg["start"] - clip_start
        se = seg["end"] - clip_start
        if se < 0 or ss > clip_dur:
            continue
        ss = max(0.0, ss)
        se = min(clip_dur, se)
        if se <= ss:
            continue
        words = seg["text"].strip().split()
        if not words:
            continue
        for i in range(0, len(words), max_w):
            group = words[i:i + max_w]
            if not group:
                continue
            total = len(words)
            group_start = ss + (se - ss) * (i / total)
            group_end = ss + (se - ss) * (min(i + max_w, total) / total)
            text = " ".join(group)
            text = re.sub(r"[^A-Za-z0-9' ]", "", text).strip().upper()
            if text:
                chunks.append({
                    "text": text,
                    "start": round(group_start, 3),
                    "end": round(group_end, 3),
                })
    return chunks


def build_ass_karaoke(segments: list, clip_start: float, clip_end: float,
                      fmt: str, font: str = "Bebas Neue",
                      outline_color: str = "black",
                      font_size: int = None,
                      position: str = "bottom",
                      highlight_color: str = "yellow") -> str:
    res_x, res_y = PLAY_RES.get(fmt, (1080, 1920))
    clip_duration = clip_end - clip_start
    fs = font_size if font_size else DEFAULT_FONT_SIZE.get(fmt, 82)
    oc = ASS_COLORS.get(outline_color, "&H00000000")
    white = "&H00FFFFFF"
    yellow = ASS_COLORS.get(highlight_color, "&H0000FFFF")
    bc = "&H80000000"
    max_w = MAX_WORDS.get(fmt, 4)

    pos_x = res_x // 2
    pos_y = {
        "bottom": {"9:16": 1750, "16:9": 980, "1:1": 940},
        "middle": {"9:16": 960,  "16:9": 540, "1:1": 540},
        "top":    {"9:16": 160,  "16:9": 100, "1:1": 130},
    }.get(position, {"9:16": 1750, "16:9": 980, "1:1": 940}).get(fmt, 1750)

    def clean_word(w: str) -> str:
        return re.sub(r"[^A-Za-z0-9']", "", w.strip()).upper()

    def to_ass_time(s: float) -> str:
        s = max(0.0, s)
        h = int(s // 3600)
        m = int((s % 3600) // 60)
        sec = s % 60
        return f"{h}:{m:02d}:{sec:05.2f}"

    words = []
    for seg in segments:
        for w in seg.get("words", []):
            ws = round(w["start"] - clip_start - 0.05, 3)
            we = round(w["end"] - clip_start, 3)
            if we >= 0 and ws <= clip_duration:
                ws = max(0.0, ws)
                we = min(clip_duration, we)
                if we > ws:
                    c = clean_word(w["word"])
                    if c:
                        words.append({"word": c, "start": ws, "end": we})

    if not words:
        for seg in segments:
            ss = round(seg["start"] - clip_start, 3)
            se = round(seg["end"] - clip_start, 3)
            if se >= 0 and ss <= clip_duration:
                ss = max(0.0, ss)
                se = min(clip_duration, se)
                seg_words = seg["text"].strip().split()
                if not seg_words or se <= ss:
                    continue
                d = (se - ss) / len(seg_words)
                for j, w in enumerate(seg_words):
                    c = clean_word(w)
                    if c:
                        words.append({
                            "word": c,
                            "start": round(max(0.0, ss + j * d - 0.05), 3),
                            "end": round(min(clip_duration, ss + (j + 1) * d), 3)
                        })

    if not words:
        return None

    ass = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {res_x}
PlayResY: {res_y}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: CF,{font},{fs},{white},&H000000FF,{oc},{bc},1,0,0,0,100,100,0,0,1,5,2,8,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    groups = []
    for i in range(0, len(words), max_w):
        g = words[i:i + max_w]
        if g:
            groups.append(g)

    for g_idx, group in enumerate(groups):
        next_group_start = groups[g_idx + 1][0]["start"] if g_idx + 1 < len(groups) else None

        for active_idx, active_word in enumerate(group):
            line_start = active_word["start"]
            if active_idx + 1 < len(group):
                line_end = group[active_idx + 1]["start"]
            else:
                line_end = active_word["end"] + 0.05
                if next_group_start is not None:
                    line_end = min(line_end, next_group_start - 0.01)
            if line_end <= line_start:
                line_end = line_start + 0.08

            parts = []
            for idx, w in enumerate(group):
                if idx == active_idx:
                    parts.append(f"{{{chr(92)}c{yellow}}}{w['word']}{{{chr(92)}c{white}}}")
                else:
                    parts.append(w["word"])

            text = " ".join(parts)
            ass += f"Dialogue: 0,{to_ass_time(line_start)},{to_ass_time(line_end)},CF,,0,0,0,,{{{chr(92)}pos({pos_x},{pos_y})}}{text}\n"

    return ass


def build_ass(segments: list, clip_start: float, clip_end: float,
              fmt: str, font: str = "Bebas Neue",
              text_color: str = "white", outline_color: str = "black",
              preset: str = "bold", font_size: int = None,
              position: str = "bottom") -> str:
    res_x, res_y = PLAY_RES.get(fmt, (1080, 1920))
    clip_duration = clip_end - clip_start
    fs = font_size if font_size else DEFAULT_FONT_SIZE.get(fmt, 72)
    tc = ASS_COLORS.get(text_color, "&H00FFFFFF")
    oc = ASS_COLORS.get(outline_color, "&H00000000")
    bc = "&H80000000"
    bold, border_style, outline, shadow, extra = CAPTION_PRESETS.get(preset, CAPTION_PRESETS["bold"])
    pos_settings = POSITION_MAP.get(position, POSITION_MAP["bottom"])
    alignment, margin_v = pos_settings.get(fmt, (2, 120))

    def clean_text(t: str) -> str:
        return re.sub(r"[^A-Za-z0-9' ]", "", t.strip()).upper().strip()

    def to_ass_time(s: float) -> str:
        s = max(0.0, s)
        h = int(s // 3600)
        m = int((s % 3600) // 60)
        sec = s % 60
        return f"{h}:{m:02d}:{sec:05.2f}"

    ass = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {res_x}
PlayResY: {res_y}
WrapStyle: 1
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: CF,{font},{fs},{tc},&H000000FF,{oc},{bc},{bold},0,0,0,100,100,0,0,{border_style},{outline},{shadow},{alignment},20,20,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    lines_added = 0

    if segments and "text" in segments[0] and "words" not in segments[0]:
        for chunk in segments:
            ss, se = chunk["start"], chunk["end"]
            if se < 0 or ss > clip_duration or se <= ss:
                continue
            text = clean_text(chunk["text"])
            if text:
                ass += f"Dialogue: 0,{to_ass_time(ss)},{to_ass_time(se)},CF,,0,0,0,,{extra}{text}\n"
                lines_added += 1
        return ass if lines_added > 0 else None

    chunks = split_segments_into_chunks(segments, clip_start, clip_end, fmt)
    for chunk in chunks:
        ass += f"Dialogue: 0,{to_ass_time(chunk['start'])},{to_ass_time(chunk['end'])},CF,,0,0,0,,{extra}{chunk['text']}\n"
        lines_added += 1

    return ass if lines_added > 0 else None


# ─── Helpers ──────────────────────────────────────────────────────────────

def get_crop(fmt: str) -> str:
    if fmt == "9:16":
        return "crop=ih*9/16:ih:(iw-ih*9/16)/2:0,scale=1080:1920"
    elif fmt == "16:9":
        return "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black"
    elif fmt == "1:1":
        return "crop=min(iw\\,ih):min(iw\\,ih):(iw-min(iw\\,ih))/2:(ih-min(iw\\,ih))/2,scale=1080:1080"
    return "crop=ih*9/16:ih:(iw-ih*9/16)/2:0,scale=1080:1920"


def get_duration(path: str) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", path],
        capture_output=True, text=True
    )
    try:
        return float(json.loads(r.stdout)["format"].get("duration", 0))
    except Exception:
        return 0.0


def generate_title(transcript: str) -> str:
    if not transcript:
        return "Untitled clip"
    words = transcript.strip().split()
    return " ".join(words[:10]).capitalize() + ("..." if len(words) > 10 else "")


def apply_text_watermark(in_path: str, out_path: str,
                          text: str = "ClipForge",
                          font: str = WATERMARK_FONT) -> bool:
    """Burn a text watermark — white text, dark border + shadow, top right."""
    txt = out_path.rsplit(".", 1)[0] + "_wm.txt"
    with open(txt, "w", encoding="utf-8") as f:
        f.write(text)
    try:
        return _encode_with_filters(in_path, out_path, [watermark_filter(txt, font)])
    finally:
        os.remove(txt)


def make_thumbnail(video_path: str, thumb_path: str, at: float = 2.0) -> str:
    subprocess.run([
        "ffmpeg", "-y", "-ss", str(at), "-i", video_path,
        "-vframes", "1", "-q:v", "2", "-loglevel", "quiet", thumb_path
    ], capture_output=True)
    return thumb_path if os.path.exists(thumb_path) else None


def resolve_latest_video(channel_url: str) -> str:
    """Turn a channel/profile/playlist URL into the URL of its newest video."""
    cmd = ["yt-dlp", "--flat-playlist", "--playlist-items", "1",
           "--print", "url", "--no-warnings"]
    proxy_url = os.environ.get("PROXY_URL", "")
    if proxy_url:
        cmd += ["--proxy", proxy_url]
    url = channel_url.rstrip("/")
    # YouTube channel root pages list tabs, not videos — point at the videos tab.
    if re.search(r"youtube\.com/(@[^/]+|channel/[^/]+|c/[^/]+|user/[^/]+)$", url):
        url += "/videos"
    r = subprocess.run(cmd + [url], capture_output=True, text=True, timeout=120)
    lines = [l.strip() for l in r.stdout.splitlines() if l.strip().startswith("http")]
    if r.returncode != 0 or not lines:
        # Not a list (e.g. already a single video URL) — use as-is.
        return channel_url
    return lines[0]


def reprocess_clip(source_path: str, out_dir: str, clip_id: int,
                   abs_start: float, abs_end: float, fmt: str,
                   segments: list, seg_offset: float,
                   font: str, highlight_color: str, font_size: int,
                   position: str = "bottom", outline_color: str = "black",
                   watermark_text: str = None, face_track: bool = True) -> dict:
    """
    Re-render a clip from the original source with new trim points and
    caption style. `segments` are timed relative to the old clip start;
    `seg_offset` is the trim start within the old clip, so captions are
    shifted onto the new clip.
    Returns {"clip_path", "thumbnail_path", "segments"}.
    """
    duration = abs_end - abs_start
    if duration <= 0.5:
        raise RuntimeError("Trim range is too short.")
    stamp = int(time.time())
    raw = os.path.join(out_dir, f"edit_{clip_id}_{stamp}_raw.mp4")
    final = os.path.join(out_dir, f"edit_{clip_id}_{stamp}_final.mp4")
    if not cut_clip(source_path, abs_start, duration, fmt, raw, face_track=face_track):
        raise RuntimeError("Could not cut the clip from the source video.")

    new_segs = relative_segments(segments, seg_offset, seg_offset + duration)
    work = raw
    ass_content = build_ass_karaoke(new_segs, 0.0, duration, fmt, font,
                                    outline_color, font_size or None, position,
                                    highlight_color)
    if ass_content:
        ass_path = raw.replace("_raw.mp4", ".ass")
        with open(ass_path, "w", encoding="utf-8") as f:
            f.write(ass_content)
        cap = raw.replace("_raw.mp4", "_cap.mp4")
        r = subprocess.run([
            "ffmpeg", "-y", "-i", raw, "-vf", f"ass={ass_path}",
            "-c:v", "libx264", "-preset", "fast", "-crf", "22",
            "-c:a", "copy", "-movflags", "+faststart", "-loglevel", "error", cap
        ], capture_output=True)
        os.remove(ass_path)
        if r.returncode == 0:
            os.remove(raw)
            work = cap

    if watermark_text:
        wm = work.replace(".mp4", "_wm.mp4")
        if apply_text_watermark(work, wm, watermark_text):
            os.remove(work)
            work = wm

    os.rename(work, final)
    thumb = make_thumbnail(final, final.replace("_final.mp4", "_thumb.jpg"),
                           at=min(2.0, duration / 2))
    return {"clip_path": final, "thumbnail_path": thumb, "segments": new_segs}

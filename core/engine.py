"""
ClipForge v1.12 — Core Engine
Pipeline:
  Step 1: Download
  Step 2: Transcribe
  Step 3: Detect Moments
  Step 4: Cut Clips
  Step 5: Burn Captions (sentence mode, Saved-style)
"""
import os
import re
import json
import subprocess
import tempfile
from pathlib import Path
from db.database import log, set_step

VERSION = "5.4"
CLIPS_DIR = Path(__file__).parent.parent / "clips"
UPLOADS_DIR = Path(__file__).parent.parent / "uploads"
WATERMARKS_DIR = Path(__file__).parent.parent / "watermarks"
STATIC_DIR = Path(__file__).parent.parent / "static"

for d in [CLIPS_DIR, UPLOADS_DIR, WATERMARKS_DIR]:
    d.mkdir(exist_ok=True)

# ASS color map — &HAABBGGRR format
ASS_COLORS = {
    "white":   "&H00FFFFFF",
    "yellow":  "&H0000FFFF",
    "cyan":    "&H00FFFF00",
    "magenta": "&H00FF00FF",
    "orange":  "&H000080FF",
    "red":     "&H000000FF",
    "black":   "&H00000000",
}

# ASS PlayRes per format
PLAY_RES = {"9:16": (1080, 1920), "16:9": (1920, 1080), "1:1": (1080, 1080)}

# Caption style presets — (Bold, BorderStyle, Outline, Shadow, extra_tags)
# BorderStyle 1=outline+shadow, 3=opaque box
CAPTION_PRESETS = {
    "bold":      (1, 1, 5, 2, ""),
    "outlined":  (1, 1, 6, 0, ""),
    "shadow":    (1, 1, 3, 4, ""),
    "minimal":   (0, 1, 2, 1, ""),
    "box":       (1, 3, 0, 0, ""),
    "karaoke":   (1, 1, 5, 2, ""),
}

# Default font size per format (can be overridden by user)
DEFAULT_FONT_SIZE = {"9:16": 82, "16:9": 52, "1:1": 66}

# Max words per caption line per format
MAX_WORDS = {"9:16": 4, "16:9": 5, "1:1": 4}

# Position settings: (Alignment, MarginV)
# Alignment 2=bottom-center, 8=top-center
POSITION_MAP = {
    "bottom": {"9:16": (2, 120),  "16:9": (2, 60),  "1:1": (2, 80)},
    "middle": {"9:16": (2, 850),  "16:9": (2, 460),  "1:1": (2, 460)},
    "top":    {"9:16": (8, 80),   "16:9": (8, 50),   "1:1": (8, 60)},
}

# Watermark sizes (width, height) per format — square for logo
WATERMARK_SIZES = {
    "small":  {"9:16": (120, 120), "16:9": (120, 120), "1:1": (120, 120)},
    "medium": {"9:16": (180, 180), "16:9": (180, 180), "1:1": (180, 180)},
    "large":  {"9:16": (240, 240), "16:9": (240, 240), "1:1": (240, 240)},
}

def get_watermark_overlay(position: str, fmt: str, wm_w: int, wm_h: int) -> str:
    """Return FFmpeg overlay x:y expression for watermark position."""
    pad = 30
    w, h = {"9:16": (1080, 1920), "16:9": (1920, 1080), "1:1": (1080, 1080)}.get(fmt, (1080, 1920))
    return {
        "bottom_right": f"{w - wm_w - pad}:{h - wm_h - pad}",
        "bottom_left":  f"{pad}:{h - wm_h - pad}",
        "top_right":    f"{w - wm_w - pad}:{pad}",
        "top_left":     f"{pad}:{pad}",
    }.get(position, f"{w - wm_w - pad}:{h - wm_h - pad}")


# ─── STEP 1: Download ─────────────────────────────────────────────────────

def step1_download(url: str, job_id: int) -> str:
    set_step(job_id, "Downloading video...", 10)
    log(job_id, f"Step 1 — Downloading: {url}")
    out_dir = UPLOADS_DIR / str(job_id)
    out_dir.mkdir(exist_ok=True)
    out_path = str(out_dir / "source.%(ext)s")
    cmd = [
        "yt-dlp",
        "--format", "bestvideo[height<=1080]+bestaudio/best",
        "--merge-output-format", "mp4",
        "--output", out_path,
        "--no-playlist",
        "--cookies-from-browser", "chrome",
        "--remote-components", "ejs:github",
        url
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        err = result.stderr.strip()
        log(job_id, f"Download failed: {err}", "error")
        if "Sign in to confirm" in err or "429" in err:
            raise RuntimeError("YouTube blocked the download. Log into YouTube in Chrome and try again.")
        elif "Private video" in err:
            raise RuntimeError("This video is private.")
        elif "not available" in err:
            raise RuntimeError("Video not available in your region.")
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

def step2_transcribe(video_path: str, job_id: int) -> list:
    set_step(job_id, "Transcribing audio...", 30)
    log(job_id, "Step 2 — Extracting audio...")

    audio_path = video_path.rsplit(".", 1)[0] + "_audio.wav"
    r = subprocess.run([
        "ffmpeg", "-i", video_path, "-vn", "-ar", "16000", "-ac", "1",
        "-acodec", "pcm_s16le", audio_path, "-y", "-loglevel", "quiet"
    ], capture_output=True, timeout=120)

    if r.returncode != 0:
        log(job_id, f"Audio extraction failed: {r.stderr.decode()[:200]}", "error")
        set_step(job_id, "Transcription skipped", 55)
        return []

    if not os.path.exists(audio_path):
        log(job_id, "Audio file not created", "error")
        set_step(job_id, "Transcription skipped", 55)
        return []

    log(job_id, "Audio extracted — loading Whisper model...")
    set_step(job_id, "Loading Whisper model...", 35)

    segments = []

    # Try WhisperX first (better word timestamps)
    try:
        import matplotlib  # required by whisperx
        import whisperx
        log(job_id, "Using WhisperX for better word timestamps...")
        set_step(job_id, "Transcribing with WhisperX...", 40)
        model = whisperx.load_model("base", device="cpu", compute_type="int8")
        audio = whisperx.load_audio(audio_path)
        result = model.transcribe(audio, batch_size=8)
        align_model, metadata = whisperx.load_align_model(
            language_code=result["language"], device="cpu"
        )
        result = whisperx.align(result["segments"], align_model, metadata, audio, device="cpu")
        for seg in result["segments"]:
            words = [{"word": w["word"].strip(), "start": w["start"], "end": w["end"]}
                     for w in seg.get("words", []) if "start" in w and "end" in w]
            segments.append({"start": seg["start"], "end": seg["end"],
                              "text": seg["text"].strip(), "words": words})
        log(job_id, f"WhisperX complete — {len(segments)} segments")
    except Exception as e:
        log(job_id, f"WhisperX not available ({str(e)[:60]}) — using faster-whisper", "warn")
        segments = []

    # Fall back to faster-whisper with medium model for better accuracy
    if not segments:
        try:
            from faster_whisper import WhisperModel
            set_step(job_id, "Transcribing speech...", 40)
            log(job_id, "Loading Whisper medium model...")
            model = WhisperModel("medium", device="cpu", compute_type="int8")
            segments_iter, info = model.transcribe(
                audio_path, beam_size=3, word_timestamps=True,
                vad_filter=True, vad_parameters=dict(min_silence_duration_ms=500),
            )
            for seg in segments_iter:
                words = [{"word": w.word.strip(), "start": w.start, "end": w.end}
                         for w in (seg.words or [])]
                segments.append({"start": seg.start, "end": seg.end,
                                  "text": seg.text.strip(), "words": words})
                if len(segments) % 20 == 0:
                    log(job_id, f"Transcribing... {len(segments)} segments")
                    set_step(job_id, f"Transcribing... {len(segments)} segments", 45)
            log(job_id, f"Whisper medium complete — {len(segments)} segments, lang: {info.language}")
        except Exception as e:
            log(job_id, f"Transcription error: {str(e)}", "error")

    if os.path.exists(audio_path):
        os.remove(audio_path)

    if segments:
        set_step(job_id, f"Transcribed — {len(segments)} segments", 55)
    else:
        log(job_id, "No segments — continuing without captions", "warn")
        set_step(job_id, "Transcription skipped", 55)

    return segments


# ─── STEP 3: Detect Moments ───────────────────────────────────────────────

def step3_detect_moments(video_path: str, segments: list, job_id: int) -> list:
    set_step(job_id, "Detecting best moments...", 60)
    log(job_id, "Step 3 — Analyzing video for best moments...")
    duration = get_duration(video_path)
    if duration == 0:
        raise RuntimeError("Could not read video duration.")
    log(job_id, f"Video duration: {int(duration // 60)}m {int(duration % 60)}s")

    moments = []
    window, step = 45.0, 15.0
    t = 0.0

    while t + window <= duration:
        segs = [s for s in segments if s["start"] >= t and s["end"] <= t + window]
        transcript = " ".join(s["text"] for s in segs)
        tl = transcript.lower()
        word_count = sum(len(s["text"].split()) for s in segs)

        # Speech density — reward dense speech, penalize silence
        speech_density = min(35, word_count // 3)

        # Strong hooks — things that make people stop scrolling
        hooks = [
            "why", "how", "what if", "the truth", "nobody talks about",
            "never", "always", "secret", "biggest mistake", "most people",
            "you need to", "i was wrong", "i can't believe", "this is why",
            "let me tell you", "here's the thing", "the problem is",
            "what nobody tells you", "stop", "wait", "listen",
        ]
        hook_score = min(20, sum(6 for h in hooks if h in tl))

        # Emotional energy words
        energy = [
            "incredible", "insane", "crazy", "unbelievable", "shocked",
            "amazing", "important", "critical", "massive", "serious",
            "money", "success", "failed", "broke", "fired", "quit",
            "love", "hate", "fear", "angry", "excited", "nervous",
        ]
        energy_score = min(15, sum(4 for e in energy if e in tl))

        # Questions — drive curiosity and engagement
        question_count = transcript.count("?")
        question_score = min(10, question_count * 4)

        # Penalize if mostly silence (very few words)
        silence_penalty = -15 if word_count < 20 else 0

        # Penalize filler-heavy segments
        fillers = ["um", "uh", "like", "you know", "basically", "literally"]
        filler_count = sum(tl.count(f) for f in fillers)
        filler_penalty = -min(10, filler_count * 2)

        score = max(0, min(100,
            30 + speech_density + hook_score + energy_score +
            question_score + silence_penalty + filler_penalty
        ))

        moments.append({
            "start": round(t, 2),
            "end": round(min(t + window, duration), 2),
            "score": score,
            "transcript": transcript,
            "segments": segs,
        })
        t += step

    moments.sort(key=lambda x: x["score"], reverse=True)

    # Deduplicate — keep top moments at least 20s apart
    top = []
    for m in moments[:25]:
        if not any(abs(m["start"] - k["start"]) < 20 for k in top):
            top.append(m)
        if len(top) >= 10:
            break

    log(job_id, f"Step 3 complete — {len(top)} moments selected (top score: {top[0]['score'] if top else 0})")
    set_step(job_id, f"Found {len(top)} moments", 75)
    return top


# ─── STEP 4: Cut Clips (with zoom punch) ─────────────────────────────────

def step4_cut_clips(video_path: str, moments: list, job_id: int, fmt: str, zoom_punch: bool = False) -> list:
    set_step(job_id, "Cutting clips...", 78)
    log(job_id, f"Step 4 — Cutting {len(moments)} clips in {fmt} format...")
    out_dir = CLIPS_DIR / str(job_id)
    out_dir.mkdir(exist_ok=True)
    crop = get_crop(fmt)
    results = []

    for i, m in enumerate(moments):
        clip_path = str(out_dir / f"clip_{i+1:02d}_raw.mp4")
        duration = m["end"] - m["start"]
        log(job_id, f"Cutting clip {i+1} at {int(m['start'])}s — {int(duration)}s (score: {m['score']})")

        # Build video filter — zoom punch is optional
        if zoom_punch:
            res = {"9:16": "1080x1920", "16:9": "1920x1080", "1:1": "1080x1080"}.get(fmt, "1080x1920")
            zoom_filter = (
                f"zoompan=z='if(lte(on,9),1.0+on*0.004,1.04)':"
                f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={res}:fps=30"
            )
            vf = f"{crop},{zoom_filter}"
        else:
            vf = crop

        cmd = [
            "ffmpeg", "-y",
            "-ss", str(m["start"]),
            "-i", video_path,
            "-t", str(duration),
            "-vf", vf,
            "-c:v", "libx264", "-preset", "fast", "-crf", "22",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            "-loglevel", "error",
            clip_path
        ]
        r = subprocess.run(cmd, capture_output=True)

        if r.returncode != 0 and zoom_punch:
            # Zoom filter failed — retry without it
            log(job_id, f"Zoom filter failed on clip {i+1} — retrying without zoom", "warn")
            cmd_plain = [
                "ffmpeg", "-y",
                "-ss", str(m["start"]),
                "-i", video_path,
                "-t", str(duration),
                "-vf", crop,
                "-c:v", "libx264", "-preset", "fast", "-crf", "22",
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart",
                "-loglevel", "error",
                clip_path
            ]
            r = subprocess.run(cmd_plain, capture_output=True)

        if r.returncode != 0:
            log(job_id, f"Clip {i+1} cut failed: {r.stderr.decode()[:200]}", "error")
            continue

        results.append({
            "clip_path": clip_path,
            "start": m["start"], "end": m["end"],
            "duration": duration, "score": m["score"],
            "transcript": m["transcript"], "segments": m["segments"],
            "format": fmt,
        })
        pct = 78 + int((i+1) / len(moments) * 7)
        set_step(job_id, f"Cutting clip {i+1} of {len(moments)}", pct)

    log(job_id, f"Step 4 complete — {len(results)} clips cut")
    set_step(job_id, f"Cut {len(results)} clips", 85)
    return results


# ─── STEP 5: Burn Captions ───────────────────────────────────────────────

def step5_burn_captions(clips: list, video_path: str, all_segments: list,
                         job_id: int, font: str = "Bebas Neue",
                         text_color: str = "white", outline_color: str = "black",
                         preset: str = "karaoke", font_size: int = None,
                         position: str = "bottom") -> list:
    set_step(job_id, "Burning captions...", 86)
    log(job_id, f"Step 5 — Captions (preset:{preset} font:{font})")
    results = []

    for i, clip in enumerate(clips):
        raw_path = clip["clip_path"]
        # Generate paths based on raw_path stem — works regardless of naming
        stem = raw_path.rsplit(".", 1)[0]
        cap_path = stem + "_cap.mp4"
        final_path = stem + "_final.mp4"
        ass_path = stem + ".ass"
        thumb_path = stem + "_thumb.jpg"
        fmt = clip["format"]

        # Get segments with 5s buffer each side
        buf_start = max(0, clip["start"] - 5)
        buf_end = clip["end"] + 5
        buf_segs = [s for s in all_segments if s["start"] >= buf_start and s["end"] <= buf_end]

        # Pre-split into chunks for editor
        chunks = split_segments_into_chunks(buf_segs, clip["start"], clip["end"], fmt)
        clip["segments_json"] = json.dumps(chunks)

        # Build caption file
        if preset == "karaoke":
            ass_content = build_ass_karaoke(
                buf_segs, clip["start"], clip["end"],
                fmt, font, outline_color, font_size, position
            )
        else:
            ass_content = build_ass(buf_segs, clip["start"], clip["end"],
                                    fmt, font, text_color, outline_color,
                                    preset, font_size, position)

        work_path = raw_path  # default — use raw if captions fail

        if ass_content:
            with open(ass_path, "w", encoding="utf-8") as f:
                f.write(ass_content)

            r = subprocess.run([
                "ffmpeg", "-y",
                "-i", raw_path,
                "-vf", f"ass={ass_path}",
                "-c:v", "libx264", "-preset", "fast", "-crf", "22",
                "-c:a", "copy",
                "-movflags", "+faststart",
                "-loglevel", "error",
                cap_path
            ], capture_output=True)

            if r.returncode == 0 and os.path.exists(cap_path):
                log(job_id, f"Captions burned on clip {i+1}")
                if os.path.exists(raw_path): os.remove(raw_path)
                if os.path.exists(ass_path): os.remove(ass_path)
                work_path = cap_path
            else:
                err = r.stderr.decode()[:150]
                log(job_id, f"Caption burn failed clip {i+1}: {err} — using raw", "warn")
                if os.path.exists(ass_path): os.remove(ass_path)
        else:
            log(job_id, f"No transcript for clip {i+1} — skipping captions", "warn")

        # Rename to final
        if os.path.exists(work_path):
            if work_path != final_path:
                os.rename(work_path, final_path)
            clip["clip_path"] = final_path
        else:
            log(job_id, f"Clip {i+1} file missing after captions", "error")
            continue

        # Generate thumbnail from final file
        subprocess.run([
            "ffmpeg", "-y", "-ss", "2", "-i", final_path,
            "-vframes", "1", "-q:v", "2", "-loglevel", "quiet", thumb_path
        ], capture_output=True)
        clip["thumbnail_path"] = thumb_path if os.path.exists(thumb_path) else None

        results.append(clip)
        pct = 86 + int((i+1) / len(clips) * 10)
        set_step(job_id, f"Captions {i+1}/{len(clips)}", pct)

    log(job_id, "Step 5 complete")
    set_step(job_id, "Captions complete", 96)
    return results


def split_segments_into_chunks(segments: list, clip_start: float, clip_end: float, fmt: str) -> list:
    """
    Split Whisper segments into caption chunks of max 5-6 words.
    Returns list of {text, start, end} relative to clip start.
    These are stored in DB and shown in the editor.
    """
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

        # Split into groups of max_w words
        for i in range(0, len(words), max_w):
            group = words[i:i + max_w]
            if not group:
                continue
            # Proportional timing within segment
            total = len(words)
            group_start = ss + (se - ss) * (i / total)
            group_end = ss + (se - ss) * (min(i + max_w, total) / total)
            text = " ".join(group)
            # Clean punctuation except apostrophes
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
    """
    Karaoke-style captions — exactly like the screenshot:
    - Words appear in groups of 4, locked position using \\pos
    - Active word = yellow, all others = white
    - 100ms early highlight so yellow hits exactly when word is spoken
    - \\pos locks Y coordinate — zero jumping
    """
    res_x, res_y = PLAY_RES.get(fmt, (1080, 1920))
    clip_duration = clip_end - clip_start
    fs = font_size if font_size else DEFAULT_FONT_SIZE.get(fmt, 82)
    oc = ASS_COLORS.get(outline_color, "&H00000000")
    white = "&H00FFFFFF"
    yellow = ASS_COLORS.get(highlight_color, "&H0000FFFF")
    bc = "&H80000000"
    max_w = MAX_WORDS.get(fmt, 4)

    # Fixed X center, fixed Y per position — never moves
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

    # Collect words with 100ms early highlight
    words = []
    for seg in segments:
        for w in seg.get("words", []):
            ws = round(w["start"] - clip_start - 0.05, 3)  # 50ms early
            we = round(w["end"] - clip_start, 3)
            if we >= 0 and ws <= clip_duration:
                ws = max(0.0, ws)
                we = min(clip_duration, we)
                if we > ws:
                    c = clean_word(w["word"])
                    if c:
                        words.append({"word": c, "start": ws, "end": we})

    # Fallback: distribute proportionally
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
                            "end": round(min(clip_duration, ss + (j+1) * d), 3)
                        })

    if not words:
        return None

    # ASS header — Alignment 8 = top-center of \pos coordinate
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

    # One dialogue line per active word — no overlap between groups
    # Pre-compute groups
    groups = []
    for i in range(0, len(words), max_w):
        g = words[i:i + max_w]
        if g: groups.append(g)

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
                    parts.append(w['word'])

            text = " ".join(parts)
            ass += f"Dialogue: 0,{to_ass_time(line_start)},{to_ass_time(line_end)},CF,,0,0,0,,{{{chr(92)}pos({pos_x},{pos_y})}}{text}\n"

    return ass


def build_ass(segments: list, clip_start: float, clip_end: float,
              fmt: str, font: str = "Bebas Neue",
              text_color: str = "white", outline_color: str = "black",
              preset: str = "bold", font_size: int = None,
              position: str = "bottom") -> str:
    """
    Build ASS file from pre-split caption chunks.
    segments can be raw Whisper segments OR pre-split chunks {text, start, end}.
    """
    res_x, res_y = PLAY_RES.get(fmt, (1080, 1920))
    clip_duration = clip_end - clip_start

    fs = font_size if font_size else DEFAULT_FONT_SIZE.get(fmt, 72)
    tc = ASS_COLORS.get(text_color, "&H00FFFFFF")
    oc = ASS_COLORS.get(outline_color, "&H00000000")
    bc = "&H80000000"

    bold, border_style, outline, shadow, extra = CAPTION_PRESETS.get(preset, CAPTION_PRESETS["bold"])

    # Position: alignment and margin
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

    # Check if these are pre-split chunks {text, start, end} or raw segments
    if segments and "text" in segments[0] and "start" in segments[0] and "end" in segments[0]:
        if "words" not in segments[0]:
            # Pre-split chunks — use directly
            for chunk in segments:
                ss = chunk["start"]
                se = chunk["end"]
                if se < 0 or ss > clip_duration or se <= ss:
                    continue
                text = clean_text(chunk["text"])
                if text:
                    ass += f"Dialogue: 0,{to_ass_time(ss)},{to_ass_time(se)},CF,,0,0,0,,{extra}{text}\n"
                    lines_added += 1
            return ass if lines_added > 0 else None

    # Raw Whisper segments — split into chunks
    chunks = split_segments_into_chunks(segments, clip_start, clip_end, fmt)
    for chunk in chunks:
        ass += f"Dialogue: 0,{to_ass_time(chunk['start'])},{to_ass_time(chunk['end'])},CF,,0,0,0,,{extra}{chunk['text']}\n"
        lines_added += 1

    return ass if lines_added > 0 else None


def step_package_mode(video_path: str, moments: list, segments: list,
                      job_id: int, font: str = "Bebas Neue",
                      text_color: str = "white", outline_color: str = "black",
                      highlight_color: str = "yellow", font_size: int = None,
                      position: str = "bottom",
                      apply_watermark: bool = True,
                      wm_size: str = "large", wm_position: str = "top_right",
                      client_logo: str = None,
                      watermark_text: str = "ClipForge",
                      watermark_font: str = "Arial Rounded MT Bold",
                      demo_mode: bool = False) -> list:
    """
    Package mode — top 3 moments × 16:9 = 3 clips.
    demo_mode: caps clips at 20 seconds for prospect pitches.
    """
    if demo_mode:
        set_step(job_id, "Creating demo package — 3 x 20s clips...", 78)
        log(job_id, "Demo package mode — 20 second clips for pitching")
    else:
        set_step(job_id, "Creating package — top 3 moments, 16:9...", 78)
        log(job_id, "Package mode — 3 clips, 16:9 format")

    top3 = moments[:3]
    fmt = "16:9"
    out_dir = CLIPS_DIR / str(job_id)
    out_dir.mkdir(exist_ok=True)

    logo_path = client_logo if client_logo and os.path.exists(client_logo) else str(STATIC_DIR / "cf_watermark.png")
    results = []
    total = len(top3)
    count = 0

    for m_idx, moment in enumerate(top3):
        buf_start = max(0, moment["start"] - 5)
        buf_end = moment["end"] + 5
        buf_segs = [s for s in segments if s["start"] >= buf_start and s["end"] <= buf_end]
        duration = min(moment["end"] - moment["start"], 20) if demo_mode else moment["end"] - moment["start"]
        count += 1
        label = f"Demo Clip {m_idx+1}" if demo_mode else f"Moment {m_idx+1}"
        slug = label.replace(" ", "_").replace(":", "x").replace("—", "-")
        raw_path = str(out_dir / f"pkg_{count:02d}_{slug}_raw.mp4")
        cap_path = str(out_dir / f"pkg_{count:02d}_{slug}_cap.mp4")
        final_path = str(out_dir / f"pkg_{count:02d}_{slug}_final.mp4")
        thumb_path = str(out_dir / f"pkg_{count:02d}_{slug}_thumb.jpg")

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

        # Burn karaoke captions
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

        # Apply watermark
        if apply_watermark:
            wm_text = "ClipForge"
            wm_font = "Arial Rounded MT Bold"
            success = apply_text_watermark(work_path, final_path, wm_text, wm_font)
            if success:
                if os.path.exists(work_path): os.remove(work_path)
                work_path = final_path

        if work_path != final_path:
            os.rename(work_path, final_path)

        # Thumbnail
        subprocess.run([
            "ffmpeg", "-y", "-ss", "2", "-i", final_path,
            "-vframes", "1", "-q:v", "2", "-loglevel", "quiet", thumb_path
        ], capture_output=True)

        # Pre-split chunks for editor
        chunks = split_segments_into_chunks(buf_segs, moment["start"], moment["end"], fmt)

        results.append({
            "clip_path": final_path,
            "thumbnail_path": thumb_path if os.path.exists(thumb_path) else None,
            "start": moment["start"], "end": moment["end"],
            "duration": duration, "score": moment["score"],
            "transcript": moment["transcript"],
            "segments_json": json.dumps(chunks),
            "format": fmt, "label": label,
        })

        pct = 78 + int(count / total * 21)
        set_step(job_id, f"Package {count}/{total}: {label}", pct)
        log(job_id, f"Package clip {count} done: {label}")

    log(job_id, f"Package complete — {len(results)} clips")
    set_step(job_id, f"Package ready — {len(results)} clips", 99)
    return results


def step6_apply_watermark(clips: list, job_id: int, fmt: str,
                           wm_size: str = "large",
                           wm_position: str = "top_right",
                           client_logo: str = None,
                           watermark_text: str = "ClipForge",
                           watermark_font: str = "Arial Rounded MT Bold") -> list:
    """
    Burn text watermark onto each clip.
    White text, dark border+shadow, top right. No PNG — no transparency issues ever.
    Client name used if set, otherwise ClipForge.
    """
    set_step(job_id, "Applying watermark...", 97)
    log(job_id, f"Step 6 — Text watermark: '{watermark_text}' ({watermark_font})")

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


def reburn_captions_legacy():
    """Re-cut from original source with corrected captions."""
    from db.database import get_conn, log as db_log
    conn = get_conn()
    clip_row = conn.execute("SELECT * FROM clips WHERE id=?", (clip_id,)).fetchone()
    if not clip_row:
        conn.close()
        db_log(clip_id, "Reburn failed — clip not found", "error")
        return False
    clip = dict(clip_row)
    job_row = conn.execute("SELECT * FROM jobs WHERE id=?", (clip["job_id"],)).fetchone()
    conn.close()
    if not job_row:
        db_log(clip_id, "Reburn failed — job not found", "error")
        return False
    job = dict(job_row)
    source_path = job.get("source_file") or ""
    if not source_path or not os.path.exists(source_path):
        upload_dir = UPLOADS_DIR / str(clip["job_id"])
        candidates = list(upload_dir.glob("source.*")) if upload_dir.exists() else []
        if candidates:
            source_path = str(candidates[0])
        else:
            db_log(clip_id, "Reburn failed — original source not found", "error")
            return False

    fmt = clip.get("format") or "9:16"
    clip_path = clip["file_path"]
    start_sec = clip["start_sec"]
    end_sec = clip["end_sec"]
    duration = end_sec - start_sec

    db_log(clip_id, "Re-cutting from source with corrected captions...")

    words_list = transcript.strip().split()
    if not words_list:
        db_log(clip_id, "Empty transcript", "error")
        return False

    word_dur = duration / len(words_list)
    fake_segs = [{
        "start": start_sec, "end": end_sec, "text": transcript,
        "words": [{"word": w, "start": start_sec + i*word_dur,
                   "end": start_sec + (i+1)*word_dur}
                  for i, w in enumerate(words_list)]
    }]

    ass_content = build_ass(fake_segs, start_sec, end_sec, fmt, font, color)
    if not ass_content:
        db_log(clip_id, "Could not build ASS file", "error")
        return False

    with tempfile.TemporaryDirectory() as tmp:
        ass_path = os.path.join(tmp, "captions.ass")
        raw_path = os.path.join(tmp, "raw.mp4")
        final_path = os.path.join(tmp, "final.mp4")
        with open(ass_path, "w") as f:
            f.write(ass_content)

        crop = get_crop(fmt)
        r1 = subprocess.run([
            "ffmpeg", "-y", "-ss", str(start_sec), "-i", source_path,
            "-t", str(duration), "-vf", crop,
            "-c:v", "libx264", "-preset", "fast", "-crf", "22",
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
            "-loglevel", "error", raw_path
        ], capture_output=True)
        if r1.returncode != 0:
            db_log(clip_id, f"Reburn cut failed: {r1.stderr.decode()[:200]}", "error")
            return False

        r2 = subprocess.run([
            "ffmpeg", "-y", "-i", raw_path,
            "-vf", f"ass={ass_path}",
            "-c:v", "libx264", "-preset", "fast", "-crf", "22",
            "-c:a", "copy", "-movflags", "+faststart",
            "-loglevel", "error", final_path
        ], capture_output=True)
        if r2.returncode != 0:
            db_log(clip_id, f"Reburn caption burn failed: {r2.stderr.decode()[:200]}", "error")
            return False

        import shutil
        shutil.copy2(final_path, clip_path)
        thumb = clip.get("thumbnail_path") or clip_path.replace(".mp4", "_thumb.jpg")
        subprocess.run([
            "ffmpeg", "-y", "-ss", "2", "-i", clip_path,
            "-vframes", "1", "-q:v", "2", "-loglevel", "quiet", thumb
        ], capture_output=True)
        db_log(clip_id, "Reburn complete.")
        return True


# ─── Helpers ──────────────────────────────────────────────────────────────

def get_crop(fmt: str) -> str:
    """
    Correct crop — no zoom, no distortion.
    9:16: Crop width to 9:16 ratio from center, scale up. Works for any source.
    16:9: Scale to fit, letterbox. No crop.
    1:1:  Crop to square from center, scale.
    """
    if fmt == "9:16":
        return "crop=ih*9/16:ih:(iw-ih*9/16)/2:0,scale=1080:1920"
    elif fmt == "16:9":
        return "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black"
    elif fmt == "1:1":
        return 'crop=min(iw\\,ih):min(iw\\,ih):(iw-min(iw\\,ih))/2:(ih-min(iw\\,ih))/2,scale=1080:1080'
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



def apply_text_watermark(in_path: str, out_path: str, text: str = "ClipForge",
                          font: str = "Arial Rounded MT Bold") -> bool:
    """
    Burn a clean text watermark onto a video.
    No PNG, no transparency issues. White text with dark border/shadow.
    Top right corner. Professional look.
    Winning fonts: Arial Rounded MT Bold, Comic Sans MS, Marker Felt.
    """
    wm_filter = (
        f"drawtext=text='{text}':font='{font}':fontsize=38:"
        f"fontcolor=white:x=w-tw-25:y=25:"
        f"shadowx=2:shadowy=2:shadowcolor=black@1.0:"
        f"borderw=2:bordercolor=black@0.9"
    )
    r = subprocess.run([
        "ffmpeg", "-y", "-i", in_path,
        "-vf", wm_filter,
        "-c:v", "libx264", "-preset", "fast", "-crf", "22",
        "-c:a", "copy",
        "-movflags", "+faststart",
        "-loglevel", "error",
        out_path
    ], capture_output=True)
    return r.returncode == 0

import subprocess, os, tempfile, shutil
from pathlib import Path

def run_diagnostics() -> dict:
    return {
        "ffmpeg": _chk_ffmpeg(),
        "ffprobe": _chk_ffprobe(),
        "yt_dlp": _chk_ytdlp(),
        "faster_whisper": _chk_whisper(),
        "caption_burning": _chk_ass(),
        "disk_space": _chk_disk(),
        "storage": _chk_dirs(),
    }

def _chk_ffmpeg():
    try:
        r = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True)
        if r.returncode == 0:
            return {"status": "ok", "message": r.stdout.split("\n")[0], "fix": None}
        return {"status": "error", "message": "ffmpeg not working", "fix": "brew install ffmpeg-full"}
    except FileNotFoundError:
        return {"status": "error", "message": "ffmpeg not found", "fix": "brew install ffmpeg-full"}

def _chk_ffprobe():
    try:
        r = subprocess.run(["ffprobe", "-version"], capture_output=True, text=True)
        return {"status": "ok" if r.returncode==0 else "error", "message": "ffprobe available" if r.returncode==0 else "not working", "fix": None if r.returncode==0 else "brew install ffmpeg-full"}
    except FileNotFoundError:
        return {"status": "error", "message": "ffprobe not found", "fix": "brew install ffmpeg-full"}

def _chk_ytdlp():
    try:
        r = subprocess.run(["yt-dlp", "--version"], capture_output=True, text=True)
        if r.returncode == 0:
            return {"status": "ok", "message": f"yt-dlp {r.stdout.strip()}", "fix": None}
        return {"status": "error", "message": "not working", "fix": "pip install yt-dlp"}
    except FileNotFoundError:
        return {"status": "error", "message": "yt-dlp not found", "fix": "pip install yt-dlp"}

def _chk_whisper():
    try:
        from faster_whisper import WhisperModel
        return {"status": "ok", "message": "faster-whisper available", "fix": None}
    except ImportError:
        return {"status": "error", "message": "not installed", "fix": "pip install faster-whisper"}
    except Exception as e:
        return {"status": "error", "message": str(e), "fix": "pip install faster-whisper"}

def _chk_ass():
    """Test ASS subtitle burning — the method used in v1.1."""
    try:
        with tempfile.TemporaryDirectory() as tmp:
            vid = os.path.join(tmp, "in.mp4")
            ass = os.path.join(tmp, "test.ass")
            out = os.path.join(tmp, "out.mp4")

            subprocess.run([
                "ffmpeg", "-y", "-f", "lavfi",
                "-i", "color=black:size=1080x1920:duration=1",
                "-c:v", "libx264", "-loglevel", "quiet", vid
            ], capture_output=True, check=True)

            with open(ass, "w") as f:
                f.write("""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, Bold, BorderStyle, Outline, Shadow, Alignment, MarginV, Encoding
Style: CF,Arial,72,&H00FFFFFF,&H00000000,1,1,4,2,2,80,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:01.00,CF,,0,0,0,,TEST CAPTION
""")
            r = subprocess.run([
                "ffmpeg", "-y", "-i", vid, "-vf", f"ass={ass}",
                "-loglevel", "quiet", out
            ], capture_output=True)

            if r.returncode == 0:
                return {"status": "ok", "message": "ASS caption burning works", "fix": None}
            return {"status": "error", "message": f"ASS failed: {r.stderr.decode()[:150]}", "fix": "brew unlink ffmpeg && brew install ffmpeg-full && brew link ffmpeg-full"}
    except Exception as e:
        return {"status": "error", "message": str(e), "fix": "Check ffmpeg installation"}

def _chk_disk():
    try:
        total, used, free = shutil.disk_usage("/")
        gb = free / (1024**3)
        if gb < 2:
            return {"status": "error", "message": f"Only {gb:.1f}GB free", "fix": "Free up disk space"}
        elif gb < 5:
            return {"status": "warning", "message": f"{gb:.1f}GB free — getting low", "fix": "Consider freeing space"}
        return {"status": "ok", "message": f"{gb:.1f}GB free", "fix": None}
    except Exception as e:
        return {"status": "warning", "message": str(e), "fix": None}

def _chk_dirs():
    try:
        base = Path(__file__).parent.parent
        for d in ["clips","uploads","watermarks"]:
            p = base / d
            p.mkdir(exist_ok=True)
            t = p / ".test"
            t.write_text("ok")
            t.unlink()
        return {"status": "ok", "message": "All storage folders writable", "fix": None}
    except Exception as e:
        return {"status": "error", "message": str(e), "fix": "Check folder permissions"}

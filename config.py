"""
ClipForge — central configuration.

All runtime data (database, uploads, clips, archive media) lives under DATA_DIR.
On Railway, mount a volume and set DATA_DIR to its mount path (e.g. /data)
so nothing is lost on redeploy.
"""
import os
from pathlib import Path

VERSION = "7.0"

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR") or BASE_DIR).resolve()

DB_PATH        = DATA_DIR / "clipforge.db"
UPLOADS_DIR    = DATA_DIR / "uploads"
CLIPS_DIR      = DATA_DIR / "clips"
WATERMARKS_DIR = DATA_DIR / "watermarks"
PREVIEWS_DIR   = DATA_DIR / "previews"
ARCHIVE_DIR    = DATA_DIR / "archive"

STATIC_DIR = BASE_DIR / "static"
FONTS_DIR  = BASE_DIR / "fonts"
MUSIC_DIR  = Path(os.environ.get("MUSIC_DIR") or (BASE_DIR / "music"))
VOICES_DIR = Path(os.environ.get("VOICES_DIR") or (BASE_DIR / "voices"))

# Public URL of this deployment, e.g. https://clipforge.up.railway.app
# Used for OAuth redirect URIs and share links in emails.
PUBLIC_BASE_URL = (os.environ.get("PUBLIC_BASE_URL") or "").rstrip("/")

# Comma-separated list of extra origins allowed to call the API from a browser.
CORS_ORIGINS = [o.strip() for o in (os.environ.get("CORS_ORIGINS") or "").split(",") if o.strip()]

# Business identity used on the public site and in outreach emails.
BUSINESS_NAME  = os.environ.get("BUSINESS_NAME") or "ClipForge"
CONTACT_EMAIL  = os.environ.get("CONTACT_EMAIL") or "officialclipforge@gmail.com"

# How many video jobs may render at once. Whisper + ffmpeg are heavy; keep at 1
# unless the server has plenty of RAM/CPU.
MAX_WORKERS = max(1, int(os.environ.get("MAX_WORKERS") or 1))


def ensure_dirs():
    for d in (DATA_DIR, UPLOADS_DIR, CLIPS_DIR, WATERMARKS_DIR, PREVIEWS_DIR, ARCHIVE_DIR):
        d.mkdir(parents=True, exist_ok=True)


ensure_dirs()

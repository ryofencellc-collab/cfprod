"""Autopilot settings (stored in the settings table, editable from the dashboard)."""
from db.database import get_setting, set_setting, log

KEY = "autopilot"
LOG_ID = -1  # autopilot messages go to the logs table under this job id

DEFAULTS = {
    "enabled": False,                         # master switch / kill switch
    "collections": ["universal_newsreels"],   # Internet Archive collections to draw from
    "youtube_enabled": True,
    "tiktok_enabled": True,
    "shorts_per_day": 2,                      # YouTube Shorts
    "tiktok_per_day": 1,                      # TikTok drafts
    "long_per_week": 2,                       # YouTube long-form compilations
    "compilation_size": 5,                    # newsreels per long-form video
    "window_start": 9,                        # posting window, local hour
    "window_end": 21,
    "timezone": "America/New_York",
    "voice_speaker": 0,                       # LibriTTS speaker id (0-903)
    "youtube_privacy": "public",              # public | unlisted | private
}

LIMITS = {
    "shorts_per_day": (0, 4),
    "tiktok_per_day": (0, 3),
    "long_per_week": (0, 7),
    "compilation_size": (3, 8),
    "window_start": (0, 23),
    "window_end": (1, 24),
    "voice_speaker": (0, 903),
}


def get() -> dict:
    cfg = dict(DEFAULTS)
    cfg.update(get_setting(KEY, {}) or {})
    return cfg


def update(changes: dict) -> dict:
    cfg = get()
    for k, v in changes.items():
        if k not in DEFAULTS:
            continue
        if k in LIMITS:
            lo, hi = LIMITS[k]
            v = max(lo, min(hi, int(v)))
        elif isinstance(DEFAULTS[k], bool):
            v = bool(v)
        elif k == "youtube_privacy" and v not in ("public", "unlisted", "private"):
            continue
        elif k == "collections":
            v = [str(c).strip() for c in v if str(c).strip()][:5] or DEFAULTS["collections"]
        cfg[k] = v
    if cfg["window_end"] <= cfg["window_start"]:
        cfg["window_end"] = min(24, cfg["window_start"] + 1)
    set_setting(KEY, cfg)
    return cfg


def alog(message: str, level: str = "info"):
    log(LOG_ID, message, level)

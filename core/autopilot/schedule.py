"""
Posting slots. Daily caps are spread evenly across the posting window with a
random minute inside each slot, so uploads never land at the same time every
day. Long-form videos are spaced evenly across the week.

All times are stored in UTC as 'YYYY-MM-DD HH:MM:SS' (SQLite's format).
"""
import datetime as dt
import random
from zoneinfo import ZoneInfo

from db.database import get_conn

ACTIVE = ("scheduled", "uploading", "posted")
FMT = "%Y-%m-%d %H:%M:%S"


def _tz(cfg: dict):
    try:
        return ZoneInfo(cfg.get("timezone") or "UTC")
    except Exception:
        return ZoneInfo("UTC")


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def to_db(d: dt.datetime) -> str:
    return d.strftime(FMT)


def from_db(s: str) -> dt.datetime:
    return dt.datetime.strptime(s[:19], FMT)


def _existing(platform: str, kind: str) -> list:
    conn = get_conn()
    try:
        rows = conn.execute(
            f"SELECT scheduled_at FROM archive_posts WHERE platform=? AND kind=? "
            f"AND status IN ({','.join('?' * len(ACTIVE))}) AND scheduled_at IS NOT NULL",
            (platform, kind, *ACTIVE)).fetchall()
    finally:
        conn.close()
    return [from_db(r["scheduled_at"]) for r in rows]


def per_day_cap(cfg: dict, platform: str, kind: str) -> int:
    if platform == "youtube" and kind == "short":
        return int(cfg["shorts_per_day"])
    if platform == "tiktok":
        return int(cfg["tiktok_per_day"])
    return 0


def next_daily_slot(cfg: dict, platform: str, kind: str, now: dt.datetime = None,
                    existing: list = None, rnd: random.Random = None) -> dt.datetime:
    """First free slot (UTC, naive) under the per-day cap, at least 10 minutes out."""
    rnd = rnd or random
    cap = per_day_cap(cfg, platform, kind)
    if cap <= 0:
        return None
    tz = _tz(cfg)
    now = now or utcnow()
    existing = existing if existing is not None else _existing(platform, kind)
    earliest = now + dt.timedelta(minutes=10)
    ws, we = int(cfg["window_start"]), int(cfg["window_end"])
    span = (we - ws) * 60 / cap  # minutes per slot
    local_today = now.replace(tzinfo=dt.timezone.utc).astimezone(tz).date()

    for day in range(60):
        d = local_today + dt.timedelta(days=day)
        taken = [e for e in existing
                 if e.replace(tzinfo=dt.timezone.utc).astimezone(tz).date() == d]
        if len(taken) >= cap:
            continue
        taken_slots = set()
        for e in taken:
            loc = e.replace(tzinfo=dt.timezone.utc).astimezone(tz)
            minutes = (loc.hour - ws) * 60 + loc.minute
            taken_slots.add(int(max(0, minutes) // span) if span else 0)
        for slot in range(cap):
            if slot in taken_slots:
                continue
            start_min = ws * 60 + slot * span
            minute = start_min + rnd.uniform(span * 0.1, span * 0.9)
            local = dt.datetime.combine(d, dt.time(0, 0), tzinfo=tz) + dt.timedelta(minutes=minute)
            utc = local.astimezone(dt.timezone.utc).replace(tzinfo=None)
            if utc >= earliest:
                return utc.replace(microsecond=0)
    return None


def next_weekly_slot(cfg: dict, now: dt.datetime = None, existing: list = None,
                     rnd: random.Random = None) -> dt.datetime:
    rnd = rnd or random
    per_week = int(cfg["long_per_week"])
    if per_week <= 0:
        return None
    tz = _tz(cfg)
    now = now or utcnow()
    existing = existing if existing is not None else _existing("youtube", "long")
    gap = dt.timedelta(days=7 / per_week)
    base = now + dt.timedelta(minutes=10)
    if existing:
        base = max(base, max(existing) + gap)
    local = base.replace(tzinfo=dt.timezone.utc).astimezone(tz)
    ws, we = int(cfg["window_start"]), int(cfg["window_end"])
    for day in range(14):
        d = local.date() + dt.timedelta(days=day)
        lo = dt.datetime.combine(d, dt.time(0, 0), tzinfo=tz) + dt.timedelta(hours=ws)
        hi = dt.datetime.combine(d, dt.time(0, 0), tzinfo=tz) + dt.timedelta(hours=we)
        lo = max(lo, local)
        if lo >= hi:
            continue
        pick = lo + dt.timedelta(seconds=rnd.uniform(0, (hi - lo).total_seconds()))
        return pick.astimezone(dt.timezone.utc).replace(tzinfo=None, microsecond=0)
    return None


def schedule_post(episode_id: int, platform: str, kind: str, cfg: dict,
                  title: str = "", body: str = "") -> int:
    when = next_weekly_slot(cfg) if kind == "long" else next_daily_slot(cfg, platform, kind)
    status = "scheduled" if when else "skipped"
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO archive_posts (episode_id, platform, kind, status, scheduled_at, title, body, error) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (episode_id, platform, kind, status, to_db(when) if when else None, title, body,
             None if when else "Posting cap is 0 for this platform"))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def upcoming_count(platform: str, kind: str) -> int:
    conn = get_conn()
    try:
        return conn.execute(
            "SELECT COUNT(*) n FROM archive_posts WHERE platform=? AND kind=? AND status='scheduled'",
            (platform, kind)).fetchone()["n"]
    finally:
        conn.close()


def posted_today(platform: str, kind: str, cfg: dict, now: dt.datetime = None) -> int:
    tz = _tz(cfg)
    now = now or utcnow()
    today = now.replace(tzinfo=dt.timezone.utc).astimezone(tz).date()
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT posted_at FROM archive_posts WHERE platform=? AND kind=? AND status='posted' "
            "AND posted_at IS NOT NULL", (platform, kind)).fetchall()
    finally:
        conn.close()
    return sum(1 for r in rows
               if from_db(r["posted_at"]).replace(tzinfo=dt.timezone.utc).astimezone(tz).date() == today)

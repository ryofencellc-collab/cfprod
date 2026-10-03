"""
Autopilot background loops.

producer:  keeps about two days of Shorts/TikToks and the next long-form video
           rendered ahead of time.
publisher: uploads posts whose scheduled time has arrived, respecting daily caps.

Both loops do nothing while the autopilot is switched off (the kill switch).
"""
import datetime as dt
import json
import os
import threading
import time
import traceback

from db.database import get_conn
from core.autopilot import pipeline, settings
from core.autopilot.publishers import tiktok, youtube
from core.autopilot.schedule import per_day_cap, posted_today, to_db, upcoming_count, utcnow
from core.autopilot.settings import alog

PRODUCER_INTERVAL = 120
PUBLISHER_INTERVAL = 60
MAX_ATTEMPTS = 4
FAILURE_PAUSE_AFTER = 3   # consecutive production failures before backing off for an hour

_started = False
_lock = threading.Lock()
_produce_lock = threading.Lock()
state = {"producer": "idle", "publisher": "idle", "last_error": None, "failures": 0,
         "backoff_until": 0.0}


def start_scheduler():
    global _started
    with _lock:
        if _started or os.environ.get("AUTOPILOT_DISABLED") == "1":
            return
        _started = True
    threading.Thread(target=_loop, args=(producer_tick, PRODUCER_INTERVAL), name="autopilot-producer",
                     daemon=True).start()
    threading.Thread(target=_loop, args=(publisher_tick, PUBLISHER_INTERVAL), name="autopilot-publisher",
                     daemon=True).start()


def _loop(fn, interval):
    time.sleep(10)
    while True:
        try:
            fn()
        except Exception:
            print(traceback.format_exc())
        time.sleep(interval)


# ── Producer ──────────────────────────────────────────────────────────────

def needs_single(cfg: dict) -> bool:
    want = []
    if cfg["youtube_enabled"] and cfg["shorts_per_day"] > 0:
        want.append(upcoming_count("youtube", "short") < 2 * cfg["shorts_per_day"])
    if cfg["tiktok_enabled"] and cfg["tiktok_per_day"] > 0:
        want.append(upcoming_count("tiktok", "tiktok") < 2 * cfg["tiktok_per_day"])
    if cfg["youtube_enabled"] and cfg["long_per_week"] > 0:
        # Keep enough chapters flowing for the next compilation.
        want.append(upcoming_count("youtube", "long") == 0 and
                    len(pipeline.available_chapters()) < cfg["compilation_size"])
    return any(want)


def needs_compilation(cfg: dict) -> bool:
    return (cfg["youtube_enabled"] and cfg["long_per_week"] > 0 and
            upcoming_count("youtube", "long") == 0 and
            len(pipeline.available_chapters()) >= cfg["compilation_size"])


def producer_tick(force: str = None):
    cfg = settings.get()
    if not cfg["enabled"] and not force:
        state["producer"] = "off"
        return
    if not force and time.time() < state["backoff_until"]:
        state["producer"] = "backing off after repeated failures"
        return
    if not _produce_lock.acquire(blocking=False):
        return
    try:
        if force == "compilation" or (not force and needs_compilation(cfg)):
            state["producer"] = "rendering long-form video"
            pipeline.produce_compilation(cfg)
        elif force == "single" or needs_single(cfg):
            state["producer"] = "rendering episode"
            pipeline.produce_single(cfg)
        else:
            state["producer"] = "queue full — waiting"
            housekeeping()
            return
        state["failures"] = 0
        state["last_error"] = None
    except Exception as e:
        state["failures"] += 1
        state["last_error"] = str(e)[:500]
        if state["failures"] >= FAILURE_PAUSE_AFTER:
            state["backoff_until"] = time.time() + 3600
            alog(f"Production failed {state['failures']} times in a row — pausing for an hour. "
                 f"Last error: {state['last_error']}", "error")
    finally:
        state["producer"] = "idle"
        _produce_lock.release()


def run_in_background(kind: str):
    threading.Thread(target=producer_tick, kwargs={"force": kind}, daemon=True).start()


# ── Publisher ─────────────────────────────────────────────────────────────

def _due_posts(now: dt.datetime) -> list:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT p.*, e.short_path, e.tiktok_path, e.long_path, e.thumb_path, e.tags_json "
            "FROM archive_posts p JOIN archive_episodes e ON e.id = p.episode_id "
            "WHERE p.status='scheduled' AND p.scheduled_at <= ? ORDER BY p.scheduled_at",
            (to_db(now),)).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _set_post(post_id: int, **fields):
    conn = get_conn()
    try:
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE archive_posts SET {sets} WHERE id=?", (*fields.values(), post_id))
        conn.commit()
    finally:
        conn.close()


def _mark_episode_posted(episode_id: int):
    conn = get_conn()
    try:
        left = conn.execute("SELECT COUNT(*) n FROM archive_posts WHERE episode_id=? AND status IN "
                            "('scheduled','uploading')", (episode_id,)).fetchone()["n"]
        if left == 0:
            conn.execute("UPDATE archive_episodes SET status='posted' WHERE id=? AND status='ready'",
                         (episode_id,))
            conn.commit()
    finally:
        conn.close()


def publish_post(post: dict, cfg: dict) -> dict:
    """Upload one post. Returns the result dict stored on the post."""
    if post["platform"] == "youtube":
        path = post["long_path"] if post["kind"] == "long" else post["short_path"]
        tags = json.loads(post.get("tags_json") or "[]")
        res = youtube.upload(path, post["title"], post["body"], tags, cfg["youtube_privacy"])
        if post["kind"] == "long" and post.get("thumb_path"):
            res["custom_thumbnail"] = youtube.set_thumbnail(res["id"], post["thumb_path"])
        return res
    if post["platform"] == "tiktok":
        res = tiktok.upload_draft(post["tiktok_path"])
        res["url"] = None
        return res
    raise RuntimeError(f"Unknown platform {post['platform']}")


def publisher_tick():
    cfg = settings.get()
    if not cfg["enabled"]:
        state["publisher"] = "off"
        return
    now = utcnow()
    for post in _due_posts(now):
        platform, kind = post["platform"], post["kind"]
        connected = (youtube.status() if platform == "youtube" else tiktok.status())["connected"]
        if not connected:
            state["publisher"] = f"waiting for {platform} to be connected"
            continue
        cap = per_day_cap(cfg, platform, kind)
        if kind != "long" and posted_today(platform, kind, cfg, now) >= cap:
            continue
        _set_post(post["id"], status="uploading", attempts=post["attempts"] + 1)
        state["publisher"] = f"uploading {platform} {kind}"
        try:
            res = publish_post(post, cfg)
            _set_post(post["id"], status="posted", posted_at=to_db(utcnow()),
                      external_id=str(res.get("id") or res.get("publish_id") or ""),
                      external_url=res.get("url"), response_json=json.dumps(res), error=None)
            note = " (locked private until your YouTube API audit passes)" if res.get("locked_private") else ""
            alog(f"Posted {platform} {kind}: {post['title'][:60]}{note}")
            _mark_episode_posted(post["episode_id"])
        except (youtube.QuotaError, tiktok.RateLimitError) as e:
            retry = utcnow() + dt.timedelta(hours=24 if platform == "youtube" else 2)
            _set_post(post["id"], status="scheduled", scheduled_at=to_db(retry), error=str(e)[:500])
            alog(f"{platform} limit hit — retrying at {to_db(retry)} UTC", "warn")
            break
        except Exception as e:
            attempts = post["attempts"] + 1
            if attempts >= MAX_ATTEMPTS:
                _set_post(post["id"], status="failed", error=str(e)[:1000])
                alog(f"Giving up on {platform} {kind} post {post['id']}: {str(e)[:200]}", "error")
            else:
                retry = utcnow() + dt.timedelta(minutes=30 * attempts)
                _set_post(post["id"], status="scheduled", scheduled_at=to_db(retry), error=str(e)[:1000])
                alog(f"{platform} upload failed (attempt {attempts}), retrying: {str(e)[:200]}", "warn")
    state["publisher"] = "idle"


# ── Housekeeping ──────────────────────────────────────────────────────────

def housekeeping(keep_days: int = 14):
    """Delete rendered files for episodes that are fully posted and no longer needed."""
    cutoff = to_db(utcnow() - dt.timedelta(days=keep_days))
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM archive_episodes WHERE status='posted' AND created_at < ? AND "
            "(kind='compilation' OR compiled_in IS NOT NULL)", (cutoff,)).fetchall()
    finally:
        conn.close()
    for ep in rows:
        ep = dict(ep)
        if ep["kind"] == "single":
            conn = get_conn()
            try:
                parent = conn.execute("SELECT status FROM archive_episodes WHERE id=?",
                                      (ep["compiled_in"],)).fetchone()
            finally:
                conn.close()
            if not parent or parent["status"] != "posted":
                continue
        pipeline.delete_episode_files(ep)
        conn = get_conn()
        try:
            conn.execute("UPDATE archive_episodes SET status='archived', short_path=NULL, tiktok_path=NULL, "
                         "segment_path=NULL, long_path=NULL WHERE id=?", (ep["id"],))
            conn.commit()
        finally:
            conn.close()

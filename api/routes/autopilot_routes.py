"""
Archive autopilot API — settings, status, episodes, posts, and platform OAuth.
All endpoints require the admin session (see core/auth.py).
"""
import json
import os
import secrets
import tempfile
import time
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel

from config import PUBLIC_BASE_URL
from db.database import get_conn, get_setting, set_setting
from core.autopilot import llm, pipeline, scheduler, settings, tts
from core.autopilot.publishers import tiktok, youtube
from core.autopilot.schedule import to_db, utcnow

autopilot_router = APIRouter()
oauth_router = APIRouter()

PUBLISHERS = {"youtube": youtube, "tiktok": tiktok}


def _rows(sql: str, params=()) -> list:
    conn = get_conn()
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


# ─── Status & settings ────────────────────────────────────────────────────

@autopilot_router.get("/status")
def autopilot_status():
    cfg = settings.get()
    counts = {r["status"]: r["n"] for r in _rows(
        "SELECT status, COUNT(*) n FROM archive_posts GROUP BY status")}
    srcs = {r["status"]: r["n"] for r in _rows(
        "SELECT status, COUNT(*) n FROM archive_sources GROUP BY status")}
    checks = {
        "ai_key": llm.available(),
        "voice_model": os.path.exists(tts.voice_path()),
        "youtube": youtube.status(),
        "tiktok": tiktok.status(),
        "public_base_url": bool(PUBLIC_BASE_URL),
    }
    return {
        "settings": cfg,
        "worker": {k: v for k, v in scheduler.state.items() if k != "backoff_until"},
        "backing_off": scheduler.state["backoff_until"] > time.time(),
        "checks": checks,
        "posts": counts,
        "sources": srcs,
        "chapters_ready": len(pipeline.available_chapters()),
    }


class SettingsBody(BaseModel):
    changes: dict


@autopilot_router.put("/settings")
def update_settings(body: SettingsBody):
    cfg = settings.update(body.changes)
    if "enabled" in body.changes:
        settings.alog(f"Autopilot switched {'ON' if cfg['enabled'] else 'OFF'}")
        if cfg["enabled"]:
            scheduler.state["backoff_until"] = 0
            scheduler.state["failures"] = 0
    return cfg


class RunBody(BaseModel):
    kind: str = "single"


@autopilot_router.post("/run")
def run_now(body: RunBody):
    if body.kind not in ("single", "compilation"):
        raise HTTPException(400, "kind must be 'single' or 'compilation'")
    if not llm.available():
        raise HTTPException(400, "Add GROQ_API_KEY (free) so the autopilot can write narration.")
    if body.kind == "compilation" and len(pipeline.available_chapters()) < settings.get()["compilation_size"]:
        raise HTTPException(400, "Not enough chapters yet — make more episodes first.")
    scheduler.run_in_background(body.kind)
    return {"started": body.kind}


@autopilot_router.get("/logs")
def autopilot_logs(limit: int = 100):
    return _rows("SELECT * FROM logs WHERE job_id=? ORDER BY id DESC LIMIT ?",
                 (settings.LOG_ID, min(limit, 500)))


# ─── Episodes & posts ─────────────────────────────────────────────────────

@autopilot_router.get("/episodes")
def list_episodes(limit: int = 50):
    eps = _rows(
        "SELECT e.*, s.title AS source_title, s.source_url, s.license_name, s.license_url, s.rights_basis, "
        "s.date AS source_date FROM archive_episodes e LEFT JOIN archive_sources s ON s.id = e.source_id "
        "ORDER BY e.id DESC LIMIT ?", (min(limit, 200),))
    if eps:
        ids = [e["id"] for e in eps]
        posts = _rows(f"SELECT * FROM archive_posts WHERE episode_id IN ({','.join('?' * len(ids))}) "
                      f"ORDER BY scheduled_at", ids)
        by_ep = {}
        for p in posts:
            by_ep.setdefault(p["episode_id"], []).append(p)
        for e in eps:
            e["posts"] = by_ep.get(e["id"], [])
            for k in ("short_path", "tiktok_path", "long_path", "segment_path", "thumb_path"):
                e["has_" + k.replace("_path", "")] = bool(e.get(k) and os.path.exists(e[k]))
                e.pop(k, None)
    return eps


@autopilot_router.get("/episodes/{ep_id}/file/{which}")
def episode_file(ep_id: int, which: str):
    col = {"short": "short_path", "tiktok": "tiktok_path", "long": "long_path",
           "segment": "segment_path", "thumb": "thumb_path"}.get(which)
    if not col:
        raise HTTPException(404, "Unknown file")
    rows = _rows(f"SELECT {col} AS p FROM archive_episodes WHERE id=?", (ep_id,))
    if not rows or not rows[0]["p"] or not os.path.exists(rows[0]["p"]):
        raise HTTPException(404, "File not found")
    media = "image/jpeg" if which == "thumb" else "video/mp4"
    return FileResponse(rows[0]["p"], media_type=media)


@autopilot_router.get("/episodes/{ep_id}/dispute")
def dispute_text(ep_id: int):
    """Ready-to-paste text for disputing a false Content ID claim."""
    ep = _rows("SELECT * FROM archive_episodes WHERE id=?", (ep_id,))
    if not ep:
        raise HTTPException(404, "Episode not found")
    ids = json.loads(ep[0].get("source_ids_json") or "[]") or [ep[0]["source_id"]]
    srcs = _rows(f"SELECT * FROM archive_sources WHERE id IN ({','.join('?' * len(ids))})", ids)
    lines = [
        "This video uses only public-domain or openly licensed archival footage, with original "
        "narration, editing and captions added by us. The original soundtrack is not used.",
        "",
    ]
    for s in srcs:
        lines += [f"- \"{s['title']}\" ({s['date'] or 'n.d.'}): {s['license_name']} — {s['license_url']}",
                  f"  Source: {s['source_url']}",
                  f"  {s['rights_basis'] or ''}".rstrip()]
    lines += ["", "Please release this claim — the claimant does not hold exclusive rights to public-domain material."]
    return {"text": "\n".join(lines)}


@autopilot_router.post("/posts/{post_id}/{action}")
def post_action(post_id: int, action: str):
    rows = _rows("SELECT * FROM archive_posts WHERE id=?", (post_id,))
    if not rows:
        raise HTTPException(404, "Post not found")
    conn = get_conn()
    try:
        if action == "skip":
            conn.execute("UPDATE archive_posts SET status='skipped' WHERE id=?", (post_id,))
        elif action in ("retry", "post-now"):
            if rows[0]["status"] in ("posted", "uploading"):
                raise HTTPException(400, "Already posted")
            conn.execute("UPDATE archive_posts SET status='scheduled', scheduled_at=?, attempts=0, error=NULL "
                         "WHERE id=?", (to_db(utcnow()), post_id))
        else:
            raise HTTPException(404, "Unknown action")
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


@autopilot_router.delete("/episodes/{ep_id}")
def delete_episode(ep_id: int):
    rows = _rows("SELECT * FROM archive_episodes WHERE id=?", (ep_id,))
    if not rows:
        raise HTTPException(404, "Episode not found")
    pipeline.delete_episode_files(rows[0])
    conn = get_conn()
    try:
        conn.execute("UPDATE archive_posts SET status='skipped' WHERE episode_id=? AND status='scheduled'",
                     (ep_id,))
        conn.execute("UPDATE archive_episodes SET status='deleted', short_path=NULL, tiktok_path=NULL, "
                     "segment_path=NULL, long_path=NULL WHERE id=?", (ep_id,))
        conn.commit()
    finally:
        conn.close()
    return {"deleted": ep_id}


@autopilot_router.get("/sources")
def list_sources(status: str = None, limit: int = 100):
    if status:
        return _rows("SELECT id, identifier, title, date, license_name, license_url, rights_basis, source_url, "
                     "status, reject_reason FROM archive_sources WHERE status=? ORDER BY id DESC LIMIT ?",
                     (status, min(limit, 500)))
    return _rows("SELECT id, identifier, title, date, license_name, license_url, rights_basis, source_url, "
                 "status, reject_reason FROM archive_sources ORDER BY id DESC LIMIT ?", (min(limit, 500),))


class VoiceBody(BaseModel):
    speaker: int = 0


@autopilot_router.post("/voice-preview")
def voice_preview(body: VoiceBody):
    if not 0 <= body.speaker <= 903:
        raise HTTPException(400, "Speaker must be 0-903")
    path = os.path.join(tempfile.gettempdir(), f"voice_preview_{body.speaker}.wav")
    try:
        tts.synthesize("This newsreel was shown in theaters across America. Here is what audiences saw.",
                       path, speaker=body.speaker)
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    return FileResponse(path, media_type="audio/wav")


# ─── OAuth ────────────────────────────────────────────────────────────────

def _redirect_uri(request: Request, platform: str) -> str:
    base = PUBLIC_BASE_URL or str(request.base_url).rstrip("/")
    return f"{base}/api/oauth/{platform}/callback"


@oauth_router.get("/{platform}/start")
def oauth_start(platform: str, request: Request):
    pub = PUBLISHERS.get(platform)
    if not pub:
        raise HTTPException(404, "Unknown platform")
    if not pub.configured():
        raise HTTPException(400, f"Set the {platform.upper()} client ID/secret environment variables first.")
    state_token = secrets.token_urlsafe(24)
    set_setting(f"oauth_state_{platform}", {"state": state_token, "at": time.time()})
    return RedirectResponse(pub.auth_url(_redirect_uri(request, platform), state_token), status_code=303)


@oauth_router.get("/{platform}/callback")
def oauth_callback(platform: str, request: Request, code: str = "", state: str = "",
                   error: str = "", error_description: str = ""):
    pub = PUBLISHERS.get(platform)
    if not pub:
        raise HTTPException(404, "Unknown platform")
    if error:
        return RedirectResponse("/autopilot?error=" + quote(f"{platform}: {error_description or error}"),
                                status_code=303)
    saved = get_setting(f"oauth_state_{platform}") or {}
    if not state or state != saved.get("state") or time.time() - saved.get("at", 0) > 900:
        return RedirectResponse("/autopilot?error=" + quote(f"{platform}: login expired, try again"),
                                status_code=303)
    set_setting(f"oauth_state_{platform}", {})
    try:
        pub.exchange_code(code, _redirect_uri(request, platform))
    except Exception as e:
        return RedirectResponse("/autopilot?error=" + quote(f"{platform}: {str(e)[:150]}"), status_code=303)
    settings.alog(f"{platform} connected")
    return RedirectResponse(f"/autopilot?connected={platform}", status_code=303)


@oauth_router.post("/{platform}/disconnect")
def oauth_disconnect(platform: str):
    pub = PUBLISHERS.get(platform)
    if not pub:
        raise HTTPException(404, "Unknown platform")
    pub.disconnect()
    settings.alog(f"{platform} disconnected")
    return {"ok": True}

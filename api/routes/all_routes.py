"""
ClipForge — API Routes
Clipping-business endpoints (clients, jobs, clips, previews, leads, debug).
"""
import json
import os
import re
import shutil
import secrets
import time
from collections import defaultdict, deque
from typing import Optional
from fastapi import APIRouter, HTTPException, UploadFile, File, Form, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from config import (
    CLIPS_DIR, UPLOADS_DIR, WATERMARKS_DIR, PUBLIC_BASE_URL,
    BUSINESS_NAME, CONTACT_EMAIL, VERSION,
)
from db.database import get_conn
from core.job_runner import run_job, queue_size

clips_router    = APIRouter()
clients_router  = APIRouter()
jobs_router     = APIRouter()
debug_router    = APIRouter()
previews_router = APIRouter()
leads_router    = APIRouter()


def _base_url(request: Request) -> str:
    return PUBLIC_BASE_URL or str(request.base_url).rstrip("/")


# ─── Pydantic Models ──────────────────────────────────────────────────────

class ClientCreate(BaseModel):
    name: str
    email: Optional[str] = None
    prospect_email: Optional[str] = None
    channel_url: Optional[str] = None
    monthly_rate: float = 0.0
    video_limit: int = 20
    caption_font: str = "Bebas Neue"
    caption_color: str = "white"
    auto_approve_threshold: int = 0
    watermark_position: str = "top_right"
    default_format: str = "9:16"

class ClientUpdate(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    prospect_email: Optional[str] = None
    channel_url: Optional[str] = None
    monthly_rate: Optional[float] = None
    video_limit: Optional[int] = None
    caption_font: Optional[str] = None
    caption_color: Optional[str] = None
    auto_approve_threshold: Optional[int] = None
    watermark_position: Optional[str] = None
    default_format: Optional[str] = None

class ClipUpdate(BaseModel):
    status: Optional[str] = None
    title: Optional[str] = None
    transcript: Optional[str] = None

class PreviewCreate(BaseModel):
    client_id: int
    title: str = "Check out these clips!"
    message: str = ""


# ─── Clips ────────────────────────────────────────────────────────────────

@clips_router.get("/")
def list_clips(client_id: Optional[int] = None, status: Optional[str] = None):
    conn = get_conn()
    if client_id and status:
        rows = conn.execute(
            "SELECT * FROM clips WHERE client_id=? AND status=? ORDER BY created_at DESC",
            (client_id, status)
        ).fetchall()
    elif client_id:
        rows = conn.execute(
            "SELECT * FROM clips WHERE client_id=? ORDER BY created_at DESC",
            (client_id,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM clips ORDER BY created_at DESC LIMIT 100").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@clips_router.get("/{clip_id}")
def get_clip(clip_id: int):
    conn = get_conn()
    row = conn.execute("SELECT * FROM clips WHERE id=?", (clip_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "Clip not found")
    return dict(row)


@clips_router.patch("/{clip_id}")
def update_clip(clip_id: int, body: ClipUpdate):
    conn = get_conn()
    if body.status is not None:
        conn.execute("UPDATE clips SET status=? WHERE id=?", (body.status, clip_id))
    if body.title is not None:
        conn.execute("UPDATE clips SET title=? WHERE id=?", (body.title, clip_id))
    if body.transcript is not None:
        conn.execute("UPDATE clips SET transcript=? WHERE id=?", (body.transcript, clip_id))
    conn.commit()
    row = conn.execute("SELECT * FROM clips WHERE id=?", (clip_id,)).fetchone()
    conn.close()
    return dict(row)


@clips_router.get("/{clip_id}/file")
def get_clip_file(clip_id: int):
    conn = get_conn()
    row = conn.execute("SELECT file_path FROM clips WHERE id=?", (clip_id,)).fetchone()
    conn.close()
    if not row or not os.path.exists(row["file_path"]):
        raise HTTPException(404, "Clip file not found")
    return FileResponse(
        row["file_path"],
        media_type="video/mp4",
        headers={"Accept-Ranges": "bytes"}
    )


@clips_router.get("/{clip_id}/thumbnail")
def get_clip_thumbnail(clip_id: int):
    conn = get_conn()
    row = conn.execute("SELECT thumbnail_path FROM clips WHERE id=?", (clip_id,)).fetchone()
    conn.close()
    if not row or not row["thumbnail_path"] or not os.path.exists(row["thumbnail_path"]):
        raise HTTPException(404, "Thumbnail not found")
    return FileResponse(row["thumbnail_path"], media_type="image/jpeg")


@clips_router.post("/approve-all")
def approve_all(client_id: int, min_score: int = 75):
    conn = get_conn()
    rows = conn.execute(
        "SELECT id FROM clips WHERE client_id=? AND status='pending' AND score>=?",
        (client_id, min_score)
    ).fetchall()
    approved = 0
    for r in rows:
        conn.execute("UPDATE clips SET status='approved' WHERE id=?", (r["id"],))
        approved += 1
    conn.commit()
    conn.close()
    return {"approved": approved}


@clips_router.delete("/{clip_id}")
def delete_clip(clip_id: int):
    conn = get_conn()
    row = conn.execute("SELECT file_path, thumbnail_path FROM clips WHERE id=?", (clip_id,)).fetchone()
    if row:
        for f in [row["file_path"], row["thumbnail_path"]]:
            if f and os.path.exists(f):
                os.remove(f)
    conn.execute("DELETE FROM clips WHERE id=?", (clip_id,))
    conn.commit()
    conn.close()
    return {"deleted": clip_id}


@clips_router.post("/{clip_id}/reprocess")
def reprocess_clip_endpoint(
    clip_id: int,
    trim_start: float = Form(0),
    trim_end: float = Form(0),
    caption_font: str = Form("Bebas Neue"),
    highlight_color: str = Form("yellow"),
    font_size: int = Form(0),
    segments_json: str = Form("[]"),
):
    """Re-render a clip from its source video with editor trims + caption style."""
    from core.engine import reprocess_clip
    conn = get_conn()
    try:
        clip = conn.execute("SELECT * FROM clips WHERE id=?", (clip_id,)).fetchone()
        if not clip:
            raise HTTPException(404, "Clip not found")
        job = conn.execute("SELECT * FROM jobs WHERE id=?", (clip["job_id"],)).fetchone()
        client = conn.execute("SELECT name FROM clients WHERE id=?", (clip["client_id"],)).fetchone()
    finally:
        conn.close()
    source = job["source_file"] if job else None
    if not source or not os.path.exists(source):
        raise HTTPException(409, "Original video is no longer on the server, so this clip can't be re-rendered.")
    try:
        segments = json.loads(segments_json or "[]")
        if not isinstance(segments, list):
            segments = []
    except ValueError:
        segments = []

    old_dur = clip["duration_sec"] or (clip["end_sec"] - clip["start_sec"])
    trim_start = max(0.0, trim_start)
    trim_end = old_dur if trim_end <= 0 else min(trim_end, old_dur)
    if trim_end - trim_start < 1:
        raise HTTPException(400, "Clip must be at least 1 second long.")

    watermark = None
    if job and job["apply_watermark"]:
        watermark = (client["name"] if client else None) or BUSINESS_NAME
    try:
        result = reprocess_clip(
            source, os.path.dirname(clip["file_path"]) or str(CLIPS_DIR), clip_id,
            clip["start_sec"] + trim_start, clip["start_sec"] + trim_end,
            clip["format"] or "9:16", segments, trim_start,
            caption_font, highlight_color, font_size,
            position=clip["caption_position"] or "bottom",
            outline_color=clip["outline_color"] or "black",
            watermark_text=watermark,
            face_track=bool(job["face_track"]) if job and job["face_track"] is not None else True,
        )
    except RuntimeError as e:
        raise HTTPException(500, str(e))

    for old in (clip["file_path"], clip["thumbnail_path"]):
        if old and os.path.exists(old) and old not in (result["clip_path"], result["thumbnail_path"]):
            os.remove(old)
    new_start = clip["start_sec"] + trim_start
    new_end = clip["start_sec"] + trim_end
    conn = get_conn()
    conn.execute("""
        UPDATE clips SET file_path=?, thumbnail_path=?, start_sec=?, end_sec=?, duration_sec=?,
               segments_json=?, caption_font=?, font_size=?
        WHERE id=?""", (
        result["clip_path"], result["thumbnail_path"], new_start, new_end,
        new_end - new_start, json.dumps(result["segments"]), caption_font, font_size, clip_id,
    ))
    conn.commit()
    row = conn.execute("SELECT * FROM clips WHERE id=?", (clip_id,)).fetchone()
    conn.close()
    return dict(row)


# ─── Clients ──────────────────────────────────────────────────────────────

@clients_router.get("/")
def list_clients():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM clients ORDER BY created_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@clients_router.post("/")
def create_client(body: ClientCreate):
    conn = get_conn()
    cur = conn.execute(
        """INSERT INTO clients
           (name,email,prospect_email,channel_url,monthly_rate,video_limit,
            caption_font,caption_color,auto_approve_threshold,watermark_position,default_format)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (body.name, body.email, body.prospect_email, body.channel_url,
         body.monthly_rate, body.video_limit, body.caption_font,
         body.caption_color, body.auto_approve_threshold,
         body.watermark_position, body.default_format)
    )
    conn.commit()
    row = conn.execute("SELECT * FROM clients WHERE id=?", (cur.lastrowid,)).fetchone()
    conn.close()
    return dict(row)


@clients_router.patch("/{client_id}")
def update_client(client_id: int, body: ClientUpdate):
    conn = get_conn()
    fields = {k: v for k, v in body.dict().items() if v is not None}
    if not fields:
        conn.close()
        return {"id": client_id}
    sets = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE clients SET {sets} WHERE id=?", (*fields.values(), client_id))
    conn.commit()
    row = conn.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
    conn.close()
    return dict(row)


@clients_router.get("/{client_id}/email-draft")
def get_email_draft(client_id: int, request: Request):
    """Generate the outreach email draft for a prospect, with a live preview link."""
    conn = get_conn()
    client = conn.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
    if not client:
        conn.close()
        raise HTTPException(404, "Client not found")
    clips = conn.execute(
        "SELECT * FROM clips WHERE client_id=? AND status='approved' ORDER BY created_at DESC LIMIT 10",
        (client_id,)
    ).fetchall()
    preview_link = ""
    if clips:
        prev = conn.execute(
            "SELECT token FROM previews WHERE client_id=? ORDER BY created_at DESC LIMIT 1",
            (client_id,)
        ).fetchone()
        token = prev["token"] if prev else secrets.token_urlsafe(16)
        if not prev:
            conn.execute(
                "INSERT INTO previews (client_id, token, title, message) VALUES (?,?,?,?)",
                (client_id, token, f"Clips for {client['name']}", "")
            )
            conn.commit()
        preview_link = f"{_base_url(request)}/preview/{token}"
    conn.close()

    name = client["name"]
    prospect_email = client["prospect_email"] or ""
    clip_count = len(clips)
    clips_note = f"{clip_count} clip{'s' if clip_count != 1 else ''}" if clip_count > 0 else "sample clips"

    subject = "We created something for you — at no cost"
    body = f"""Hi {name},

We came across your page and we have to be honest — your content is good. But it's not reaching the audience it deserves.

That's where we come in.

{BUSINESS_NAME} is a professional video clipping service that transforms long-form content into short, high-impact clips built for TikTok, Instagram Reels, and YouTube Shorts. We handle everything — the cutting, the captions, the formatting. You just post.

We took one of your videos and created {clips_note} for you — completely free, no strings attached.

👉 Your Free Clips: {preview_link or "[Approve clips in the dashboard first — a preview link will appear here]"}

---

A quick note on quality:

The clips above were created from a downloaded version of your video. Downloaded files lose quality in the process — so what you're seeing is actually below our standard delivery.

When you become a {BUSINESS_NAME} client, you send us your original video file directly. We send back your clips at full quality — crisp, clean, and ready to post. What we delivered here is a preview of the concept, not the finished product.

---

Your brand. Protected. Always.

You'll notice a watermark on these clips. Here's why that matters for you.

Content theft is real. Every day, pages download creators' videos and repost them without credit. When you work with {BUSINESS_NAME}, every clip is branded with your watermark — your name, your logo, your brand — permanently embedded into every video.

No matter where your content ends up, no matter who reposts it — your audience always knows where it came from. Your page grows even when someone else is doing the posting.

---

What {BUSINESS_NAME} delivers:

✅ Long videos transformed into short, viral-ready clips
✅ Professional captions that keep viewers watching
✅ Your watermark on every clip — your brand protected permanently
✅ Up to 60 clips per month
✅ Full quality when you send us your original file
✅ You post on your schedule — we handle everything else

---

We're not just a clipping service. We're a content growth partner.

The creators winning on short-form right now aren't posting more — they're posting smarter. Consistent, captioned, branded clips that show up every day without them lifting a finger.

That's exactly what we do.

These sample clips are our gift to you. If you like what you see and want this done consistently and at full quality — packages start at just $100/month.

Reply to this email and let's talk.

— The {BUSINESS_NAME} Team
{CONTACT_EMAIL}"""

    return {
        "to": prospect_email,
        "subject": subject,
        "body": body,
        "client_name": name,
        "clip_count": clip_count,
        "preview_link": preview_link,
    }


@clients_router.post("/{client_id}/watermark")
async def upload_watermark(client_id: int, file: UploadFile = File(...)):
    ext = file.filename.split(".")[-1].lower()
    if ext not in ["png", "jpg", "jpeg"]:
        raise HTTPException(400, "PNG or JPG only")
    dest = WATERMARKS_DIR / f"client_{client_id}.{ext}"
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)
    conn = get_conn()
    conn.execute("UPDATE clients SET watermark_path=? WHERE id=?", (str(dest), client_id))
    conn.commit()
    conn.close()
    return {"status": "ok"}


@clients_router.post("/{client_id}/logo")
async def upload_client_logo(client_id: int, file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".png"):
        raise HTTPException(400, "PNG only")
    dest = WATERMARKS_DIR / f"client_{client_id}_logo.png"
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)
    conn = get_conn()
    conn.execute("UPDATE clients SET logo_path=? WHERE id=?", (str(dest), client_id))
    conn.commit()
    conn.close()
    return {"status": "ok", "logo_path": str(dest)}


@clients_router.post("/{client_id}/reset-usage")
def reset_usage(client_id: int):
    conn = get_conn()
    conn.execute("UPDATE clients SET videos_used=0 WHERE id=?", (client_id,))
    conn.commit()
    conn.close()
    return {"reset": True}


@clients_router.delete("/{client_id}")
def delete_client(client_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM clients WHERE id=?", (client_id,))
    conn.execute("UPDATE clips SET status='pending' WHERE client_id=?", (client_id,))
    conn.commit()
    conn.close()
    return {"deleted": client_id}


# ─── Jobs ─────────────────────────────────────────────────────────────────

def _create_job(conn, client_id: int, source_url: str = None, **kwargs) -> int:
    """Create a job record and return its ID."""
    fields = {
        "client_id": client_id,
        "source_url": source_url,
        "status": "queued",
    }
    fields.update(kwargs)
    cols = ", ".join(fields.keys())
    placeholders = ", ".join("?" for _ in fields)
    cur = conn.execute(
        f"INSERT INTO jobs ({cols}) VALUES ({placeholders})",
        list(fields.values())
    )
    conn.commit()
    return cur.lastrowid


@jobs_router.post("/submit-url")
def submit_url(
    client_id:       int  = Form(...),
    url:             str  = Form(...),
    format:          str  = Form("9:16"),
    burn_captions:   int  = Form(1),
    zoom_punch:      int  = Form(0),
    apply_watermark: int  = Form(1),
    package_mode:    int  = Form(0),
    demo_mode:       int  = Form(0),
    split_mode:      int  = Form(0),
    split_duration:  int  = Form(60),
    add_hooks:       int  = Form(0),
    wm_position:     str  = Form("top_right"),
    caption_font:    str  = Form("Bebas Neue"),
    caption_color:   str  = Form("white"),
    outline_color:   str  = Form("black"),
    highlight_color: str  = Form("yellow"),
    caption_preset:  str  = Form("karaoke"),
    font_size:       int  = Form(0),
    caption_position: str = Form("bottom"),
    process_limit:   int  = Form(0),
    whisper_model:   str  = Form("base"),
    face_track:      int  = Form(1),
):
    conn = get_conn()
    client = conn.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
    if client and (client["video_limit"] or 0) > 0 and (client["videos_used"] or 0) >= client["video_limit"]:
        conn.close()
        raise HTTPException(429, "Client has reached their monthly video limit.")

    job_id = _create_job(conn, client_id, source_url=url,
        format=format, burn_captions=burn_captions, zoom_punch=zoom_punch,
        apply_watermark=apply_watermark, package_mode=package_mode,
        demo_mode=demo_mode, split_mode=split_mode, split_duration=split_duration,
        add_hooks=add_hooks, wm_position=wm_position,
        caption_font=caption_font, caption_color=caption_color,
        outline_color=outline_color, highlight_color=highlight_color,
        caption_preset=caption_preset, font_size=font_size,
        caption_position=caption_position, process_limit=process_limit,
        whisper_model=whisper_model, face_track=face_track,
    )
    conn.close()
    run_job(job_id)
    return {"job_id": job_id, "status": "queued", "queue_position": queue_size()}


@jobs_router.post("/submit-file")
async def submit_file(
    client_id:       int         = Form(...),
    file:            UploadFile  = File(...),
    format:          str         = Form("9:16"),
    burn_captions:   int         = Form(1),
    apply_watermark: int         = Form(1),
    package_mode:    int         = Form(0),
    demo_mode:       int         = Form(0),
    split_mode:      int         = Form(0),
    split_duration:  int         = Form(60),
    add_hooks:       int         = Form(0),
    wm_position:     str         = Form("top_right"),
    caption_font:    str         = Form("Bebas Neue"),
    caption_color:   str         = Form("white"),
    outline_color:   str         = Form("black"),
    highlight_color: str         = Form("yellow"),
    caption_preset:  str         = Form("karaoke"),
    font_size:       int         = Form(0),
    caption_position: str        = Form("bottom"),
    process_limit:   int         = Form(0),
    whisper_model:   str         = Form("base"),
    face_track:      int         = Form(1),
):
    # Save uploaded file
    upload_dir = UPLOADS_DIR / "upload_tmp"
    upload_dir.mkdir(parents=True, exist_ok=True)
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else "mp4"
    if not re.fullmatch(r"[a-z0-9]{1,5}", ext):
        ext = "mp4"
    dest = upload_dir / f"upload_{secrets.token_hex(8)}.{ext}"

    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)

    conn = get_conn()
    job_id = _create_job(conn, client_id, source_url=None,
        source_file=str(dest),
        format=format, burn_captions=burn_captions, zoom_punch=0,
        apply_watermark=apply_watermark, package_mode=package_mode,
        demo_mode=demo_mode, split_mode=split_mode, split_duration=split_duration,
        add_hooks=add_hooks, wm_position=wm_position,
        caption_font=caption_font, caption_color=caption_color,
        outline_color=outline_color, highlight_color=highlight_color,
        caption_preset=caption_preset, font_size=font_size,
        caption_position=caption_position, process_limit=process_limit,
        whisper_model=whisper_model, face_track=face_track,
    )
    conn.close()
    run_job(job_id)
    return {"job_id": job_id, "status": "queued", "queue_position": queue_size()}


@jobs_router.post("/demo")
def submit_demo(client_id: int = Form(...), url: str = Form(...)):
    """3 x 20s demo clips — for prospect pitching."""
    conn = get_conn()
    job_id = _create_job(conn, client_id, source_url=url,
        format="16:9", burn_captions=1, apply_watermark=1,
        package_mode=1, demo_mode=1, whisper_model="base",
    )
    conn.close()
    run_job(job_id)
    return {"job_id": job_id, "status": "queued"}


@jobs_router.post("/channel-demo")
def channel_demo(client_id: int = Form(...)):
    """Auto-pull video from client's channel URL and create demo clips."""
    conn = get_conn()
    client = conn.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
    conn.close()
    if not client:
        raise HTTPException(404, "Client not found")
    channel_url = client["channel_url"]
    if not channel_url:
        raise HTTPException(400, "No channel URL set for this client. Edit the client and add their channel URL.")

    from core.engine import resolve_latest_video
    video_url = resolve_latest_video(channel_url)

    conn2 = get_conn()
    job_id = _create_job(conn2, client_id, source_url=video_url,
        format="16:9", burn_captions=1, apply_watermark=1,
        package_mode=1, demo_mode=1, whisper_model="base",
    )
    conn2.close()
    run_job(job_id)
    return {"job_id": job_id, "status": "queued", "video_url": video_url}


@jobs_router.get("/{job_id}")
def get_job(job_id: int):
    conn = get_conn()
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "Job not found")
    return dict(row)


@jobs_router.get("/")
def list_jobs(client_id: Optional[int] = None):
    conn = get_conn()
    if client_id:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE client_id=? ORDER BY created_at DESC LIMIT 50",
            (client_id,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 50").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ─── Previews ─────────────────────────────────────────────────────────────

@previews_router.post("/")
def create_preview(body: PreviewCreate):
    token = secrets.token_urlsafe(16)
    conn = get_conn()
    conn.execute(
        "INSERT INTO previews (client_id, token, title, message) VALUES (?,?,?,?)",
        (body.client_id, token, body.title, body.message)
    )
    conn.commit()
    conn.close()
    return {"token": token}


@previews_router.get("/{token}")
def get_preview(token: str):
    conn = get_conn()
    preview = conn.execute("SELECT * FROM previews WHERE token=?", (token,)).fetchone()
    if not preview:
        conn.close()
        raise HTTPException(404, "Preview not found")
    clips = conn.execute(
        "SELECT * FROM clips WHERE client_id=? AND status='approved' ORDER BY created_at DESC LIMIT 10",
        (preview["client_id"],)
    ).fetchall()
    conn.close()
    public_fields = ("id", "title", "duration_sec", "format", "thumbnail_path", "created_at")
    return {
        "title": preview["title"],
        "message": preview["message"],
        "clips": [{k: c[k] for k in public_fields} for c in clips],
    }


def _preview_clip(token: str, clip_id: int):
    conn = get_conn()
    try:
        preview = conn.execute("SELECT client_id FROM previews WHERE token=?", (token,)).fetchone()
        if not preview:
            raise HTTPException(404, "Preview not found")
        clip = conn.execute(
            "SELECT * FROM clips WHERE id=? AND client_id=? AND status='approved'",
            (clip_id, preview["client_id"])
        ).fetchone()
    finally:
        conn.close()
    if not clip:
        raise HTTPException(404, "Clip not found")
    return clip


@previews_router.get("/{token}/clips/{clip_id}/file")
def preview_clip_file(token: str, clip_id: int):
    clip = _preview_clip(token, clip_id)
    if not clip["file_path"] or not os.path.exists(clip["file_path"]):
        raise HTTPException(404, "Clip file not found")
    return FileResponse(clip["file_path"], media_type="video/mp4")


@previews_router.get("/{token}/clips/{clip_id}/thumbnail")
def preview_clip_thumbnail(token: str, clip_id: int):
    clip = _preview_clip(token, clip_id)
    if not clip["thumbnail_path"] or not os.path.exists(clip["thumbnail_path"]):
        raise HTTPException(404, "Thumbnail not found")
    return FileResponse(clip["thumbnail_path"], media_type="image/jpeg")


# ─── Leads (public contact form) ──────────────────────────────────────────

class LeadCreate(BaseModel):
    name: str
    email: str
    channel_url: str = ""
    message: str = ""
    website: str = ""  # honeypot — real visitors never fill this in


_lead_hits = defaultdict(deque)


@leads_router.post("")
def create_lead(body: LeadCreate, request: Request):
    from core.auth import client_ip
    if body.website:
        return {"ok": True}  # silently drop bots
    ip = client_ip(request)
    q = _lead_hits[ip]
    now = time.time()
    while q and q[0] < now - 3600:
        q.popleft()
    if len(q) >= 5:
        raise HTTPException(429, "Too many submissions — please email us instead.")
    q.append(now)
    email = body.email.strip()[:200]
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise HTTPException(400, "Please enter a valid email address.")
    conn = get_conn()
    conn.execute(
        "INSERT INTO leads (name, email, channel_url, message) VALUES (?,?,?,?)",
        (body.name.strip()[:200], email, body.channel_url.strip()[:500], body.message.strip()[:4000])
    )
    conn.commit()
    conn.close()
    return {"ok": True}


@leads_router.get("/")
def list_leads():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM leads ORDER BY created_at DESC LIMIT 200").fetchall()
    conn.close()
    return [dict(r) for r in rows]


class LeadUpdate(BaseModel):
    status: str


@leads_router.patch("/{lead_id}")
def update_lead(lead_id: int, body: LeadUpdate):
    conn = get_conn()
    conn.execute("UPDATE leads SET status=? WHERE id=?", (body.status[:30], lead_id))
    conn.commit()
    conn.close()
    return {"ok": True}


@leads_router.post("/{lead_id}/convert")
def convert_lead(lead_id: int):
    """Turn a lead into a client record so you can run a demo for them."""
    conn = get_conn()
    lead = conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    if not lead:
        conn.close()
        raise HTTPException(404, "Lead not found")
    cur = conn.execute(
        "INSERT INTO clients (name, email, prospect_email, channel_url) VALUES (?,?,?,?)",
        (lead["name"] or lead["email"], lead["email"], lead["email"], lead["channel_url"])
    )
    conn.execute("UPDATE leads SET status='converted' WHERE id=?", (lead_id,))
    conn.commit()
    row = conn.execute("SELECT * FROM clients WHERE id=?", (cur.lastrowid,)).fetchone()
    conn.close()
    return dict(row)


# ─── Debug ────────────────────────────────────────────────────────────────

@debug_router.get("/jobs")
def debug_jobs():
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM jobs ORDER BY created_at DESC LIMIT 20"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@debug_router.get("/logs")
def debug_logs(job_id: Optional[int] = None, limit: int = 100):
    conn = get_conn()
    if job_id:
        rows = conn.execute(
            "SELECT * FROM logs WHERE job_id=? ORDER BY created_at DESC LIMIT ?",
            (job_id, limit)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM logs ORDER BY created_at DESC LIMIT ?",
            (limit,)
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@debug_router.get("/summary")
def debug_summary():
    conn = get_conn()
    clients = conn.execute("SELECT COUNT(*) as n FROM clients").fetchone()["n"]
    total_jobs = conn.execute("SELECT COUNT(*) as n FROM jobs").fetchone()["n"]
    completed = conn.execute("SELECT COUNT(*) as n FROM jobs WHERE status='done'").fetchone()["n"]
    errors = conn.execute("SELECT COUNT(*) as n FROM jobs WHERE status='error'").fetchone()["n"]
    processing = conn.execute("SELECT COUNT(*) as n FROM jobs WHERE status='processing'").fetchone()["n"]
    total_clips = conn.execute("SELECT COUNT(*) as n FROM clips").fetchone()["n"]
    approved = conn.execute("SELECT COUNT(*) as n FROM clips WHERE status='approved'").fetchone()["n"]
    conn.close()
    return {
        "clients": clients,
        "total_jobs": total_jobs,
        "completed": completed,
        "errors": errors,
        "processing": processing,
        "clips": total_clips,
        "approved": approved,
        "version": VERSION,
    }


@debug_router.post("/reset")
def debug_reset(confirm: str = ""):
    """Delete all jobs, clips, moments, logs, previews and their media files.
    Clients, leads, settings and the archive autopilot are kept."""
    if confirm != "RESET":
        raise HTTPException(400, "Pass ?confirm=RESET to wipe clipping data.")
    conn = get_conn()
    for table in ("clips", "moments", "jobs", "logs", "previews"):
        conn.execute(f"DELETE FROM {table}")
    conn.execute("UPDATE clients SET videos_used=0")
    conn.commit()
    conn.close()
    for d in (CLIPS_DIR, UPLOADS_DIR):
        if d.exists():
            for child in d.iterdir():
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
    return {"reset": True}


@debug_router.get("/diagnostics")
def diagnostics():
    import subprocess
    results = {}

    # FFmpeg
    try:
        r = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=5)
        results["ffmpeg"] = "ok" if r.returncode == 0 else "not found"
    except Exception:
        results["ffmpeg"] = "not found"

    # yt-dlp
    try:
        r = subprocess.run(["yt-dlp", "--version"], capture_output=True, text=True, timeout=5)
        results["yt_dlp"] = r.stdout.strip() if r.returncode == 0 else "not found"
    except Exception:
        results["yt_dlp"] = "not found"

    # faster-whisper
    try:
        from faster_whisper import WhisperModel
        results["faster_whisper"] = "ok"
    except Exception as e:
        results["faster_whisper"] = f"not available: {str(e)[:50]}"

    # AI keys configured
    results["groq_key"] = "set" if os.environ.get("GROQ_API_KEY") else "not set"
    results["openai_key"] = "set" if os.environ.get("OPENAI_API_KEY") else "not set"
    results["anthropic_key"] = "set" if os.environ.get("ANTHROPIC_API_KEY") else "not set"
    results["proxy"] = os.environ.get("PROXY_URL", "not set").split("@")[-1]
    from core.reframe import MODEL_PATH
    results["face_tracking"] = "ok" if MODEL_PATH.exists() else "model missing"
    results["queue_waiting"] = queue_size()

    # Disk space
    try:
        import shutil as _shutil
        total, used, free = _shutil.disk_usage("/")
        results["disk_free_gb"] = round(free / (1024**3), 1)
    except Exception:
        results["disk_free_gb"] = "unknown"

    return results

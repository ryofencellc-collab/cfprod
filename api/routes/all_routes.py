"""
ClipForge v6.0 — API Routes
All endpoints in one file for simplicity.
"""
import os
import shutil
import secrets
from typing import Optional
from pathlib import Path
from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse
from pydantic import BaseModel
from db.database import get_conn
from core.job_runner import run_job

VERSION = "6.0"

CLIPS_DIR      = Path(__file__).parent.parent.parent / "clips"
UPLOADS_DIR    = Path(__file__).parent.parent.parent / "uploads"
WATERMARKS_DIR = Path(__file__).parent.parent.parent / "watermarks"

clips_router    = APIRouter()
clients_router  = APIRouter()
jobs_router     = APIRouter()
debug_router    = APIRouter()
previews_router = APIRouter()


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
def get_email_draft(client_id: int):
    """Generate the outreach email draft for a prospect."""
    conn = get_conn()
    client = conn.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
    clips = conn.execute(
        "SELECT * FROM clips WHERE client_id=? ORDER BY created_at DESC LIMIT 3",
        (client_id,)
    ).fetchall()
    conn.close()

    if not client:
        raise HTTPException(404, "Client not found")

    name = client["name"]
    prospect_email = client["prospect_email"] or ""
    clip_count = len(clips)
    clips_note = f"{clip_count} clip{'s' if clip_count != 1 else ''}" if clip_count > 0 else "sample clips"

    subject = "We created something for you — at no cost"
    body = f"""Hi {name},

We came across your page and we have to be honest — your content is good. But it's not reaching the audience it deserves.

That's where we come in.

ClipForge is a professional video clipping service that transforms long-form content into short, high-impact clips built for TikTok, Instagram Reels, and YouTube Shorts. We handle everything — the cutting, the captions, the formatting. You just post.

We took one of your videos and created {clips_note} for you — completely free, no strings attached.

👉 Your Free Clips: [Attach clips or paste Drive link]

---

A quick note on quality:

The clips above were created from a downloaded version of your video. Downloaded files lose quality in the process — so what you're seeing is actually below our standard delivery.

When you become a ClipForge client, you send us your original video file directly. We send back your clips at full quality — crisp, clean, and ready to post. What we delivered here is a preview of the concept, not the finished product.

---

Your brand. Protected. Always.

You'll notice our ClipForge watermark on these clips. Here's why that matters for you.

Content theft is real. Every day, pages download creators' videos and repost them without credit. When you work with ClipForge, every clip is branded with your watermark — your name, your logo, your brand — permanently embedded into every video.

No matter where your content ends up, no matter who reposts it — your audience always knows where it came from. Your page grows even when someone else is doing the posting.

---

What ClipForge delivers:

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

— The ClipForge Team
officialclipforge@gmail.com"""

    return {
        "to": prospect_email,
        "subject": subject,
        "body": body,
        "client_name": name,
        "clip_count": clip_count,
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
        whisper_model=whisper_model,
    )
    conn.close()
    run_job(job_id)
    return {"job_id": job_id, "status": "queued"}


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
):
    # Save uploaded file
    upload_dir = UPLOADS_DIR / "upload_tmp"
    upload_dir.mkdir(exist_ok=True)
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else "mp4"
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
        whisper_model=whisper_model,
    )
    conn.close()
    run_job(job_id)
    return {"job_id": job_id, "status": "queued"}


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

    conn2 = get_conn()
    job_id = _create_job(conn2, client_id, source_url=channel_url,
        format="16:9", burn_captions=1, apply_watermark=1,
        package_mode=1, demo_mode=1, whisper_model="base",
    )
    conn2.close()
    run_job(job_id)
    return {"job_id": job_id, "status": "queued"}


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
    return {
        "title": preview["title"],
        "message": preview["message"],
        "clips": [dict(c) for c in clips],
    }


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

    # Disk space
    try:
        import shutil as _shutil
        total, used, free = _shutil.disk_usage("/")
        results["disk_free_gb"] = round(free / (1024**3), 1)
    except Exception:
        results["disk_free_gb"] = "unknown"

    return results

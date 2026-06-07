from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional
from db.database import get_conn, log
from core.job_runner import run_job
from core.debugger import run_diagnostics
from core.engine import generate_title
from pathlib import Path
import shutil, os, secrets

UPLOADS_DIR = Path(__file__).parent.parent.parent / "uploads"
WATERMARKS_DIR = Path(__file__).parent.parent.parent / "watermarks"
WATERMARKS_DIR.mkdir(exist_ok=True)

# ── Clips ──────────────────────────────────────────────────────────────────
clips_router = APIRouter()

class ClipUpdate(BaseModel):
    status: Optional[str] = None
    caption: Optional[str] = None
    title: Optional[str] = None

class ReburnRequest(BaseModel):
    transcript: str
    font: Optional[str] = "Bebas Neue"
    color: Optional[str] = "white"

class ChunksUpdateRequest(BaseModel):
    chunks: list
    caption_font: Optional[str] = None
    caption_color: Optional[str] = None
    outline_color: Optional[str] = None
    caption_preset: Optional[str] = None
    font_size: Optional[int] = None
    caption_position: Optional[str] = None

@clips_router.get("/{clip_id}/chunks")
def get_chunks(clip_id: int):
    """Get pre-split caption chunks for the editor."""
    import json as json_lib
    conn = get_conn()
    row = conn.execute("SELECT * FROM clips WHERE id=?", (clip_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "Clip not found")
    try:
        chunks = json_lib.loads(row["segments_json"] or "[]")
    except Exception:
        chunks = []
    return {"clip_id": clip_id, "chunks": chunks, "clip": dict(row)}

@clips_router.post("/{clip_id}/chunks")
def save_chunks(clip_id: int, body: ChunksUpdateRequest):
    """Save edited chunks and re-burn captions from source."""
    import json as json_lib
    from core.engine import build_ass, get_crop, get_duration, CLIPS_DIR, UPLOADS_DIR
    import subprocess, os, tempfile
    conn = get_conn()
    row = conn.execute("SELECT * FROM clips WHERE id=?", (clip_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Clip not found")
    clip = dict(row)

    # Find source video
    job_row = conn.execute("SELECT * FROM jobs WHERE id=?", (clip["job_id"],)).fetchone()
    conn.close()
    if not job_row:
        raise HTTPException(404, "Job not found")
    job = dict(job_row)
    source_path = job.get("source_file") or ""
    if not source_path or not os.path.exists(source_path):
        upload_dir = UPLOADS_DIR / str(clip["job_id"])
        candidates = list(upload_dir.glob("source.*")) if upload_dir.exists() else []
        if candidates:
            source_path = str(candidates[0])
        else:
            raise HTTPException(500, "Original source video not found")

    fmt = clip.get("format") or "9:16"
    # Use new style if provided, otherwise fall back to stored values
    font = body.caption_font or clip.get("caption_font") or "Bebas Neue"
    text_color = body.caption_color or clip.get("caption_color") or "white"
    outline_color = body.outline_color or clip.get("outline_color") or "black"
    preset = body.caption_preset or clip.get("caption_preset") or "bold"
    font_size = body.font_size if body.font_size is not None else (clip.get("font_size") or None)
    position = body.caption_position or clip.get("caption_position") or "bottom"
    start_sec = clip["start_sec"]
    end_sec = clip["end_sec"]
    duration = end_sec - start_sec
    clip_path = clip["file_path"]

    # Build ASS from edited chunks
    ass_content = build_ass(body.chunks, 0, duration, fmt, font, text_color, outline_color, preset, font_size, position)
    if not ass_content:
        raise HTTPException(400, "No valid caption chunks")

    with tempfile.TemporaryDirectory() as tmp:
        ass_path = os.path.join(tmp, "captions.ass")
        raw_path = os.path.join(tmp, "raw.mp4")
        final_path = os.path.join(tmp, "final.mp4")

        with open(ass_path, "w", encoding="utf-8") as f:
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
            raise HTTPException(500, f"Re-cut failed: {r1.stderr.decode()[:200]}")

        r2 = subprocess.run([
            "ffmpeg", "-y", "-i", raw_path,
            "-vf", f"ass={ass_path}",
            "-c:v", "libx264", "-preset", "fast", "-crf", "22",
            "-c:a", "copy", "-movflags", "+faststart",
            "-loglevel", "error", final_path
        ], capture_output=True)
        if r2.returncode != 0:
            raise HTTPException(500, f"Caption burn failed: {r2.stderr.decode()[:200]}")

        import shutil as sh
        sh.copy2(final_path, clip_path)

        # Regenerate thumbnail
        thumb = clip.get("thumbnail_path") or clip_path.replace(".mp4", "_thumb.jpg")
        subprocess.run(["ffmpeg", "-y", "-ss", "2", "-i", clip_path, "-vframes", "1", "-q:v", "2", "-loglevel", "quiet", thumb], capture_output=True)

    # Save updated chunks and style settings to DB
    conn = get_conn()
    conn.execute("""UPDATE clips SET segments_json=?, caption_font=?, caption_color=?,
        outline_color=?, caption_preset=?, font_size=?, caption_position=? WHERE id=?""",
        (json_lib.dumps(body.chunks), font, text_color, outline_color, preset, font_size or 0, position, clip_id))
    conn.commit()
    conn.close()

    return {"status": "ok", "message": "Captions updated"}

@clips_router.get("/")
def list_clips(client_id: Optional[int] = None, status: Optional[str] = None):
    conn = get_conn()
    q = "SELECT * FROM clips WHERE 1=1"
    p = []
    if client_id: q += " AND client_id=?"; p.append(client_id)
    if status: q += " AND status=?"; p.append(status)
    q += " ORDER BY score DESC"
    rows = [dict(r) for r in conn.execute(q, p).fetchall()]
    conn.close()
    return rows

@clips_router.patch("/{clip_id}")
def update_clip(clip_id: int, body: ClipUpdate):
    conn = get_conn()
    fields, vals = [], []
    if body.status is not None: fields.append("status=?"); vals.append(body.status)
    if body.caption is not None: fields.append("caption=?"); vals.append(body.caption)
    if body.title is not None: fields.append("title=?"); vals.append(body.title)
    if not fields: raise HTTPException(400, "Nothing to update")
    vals.append(clip_id)
    conn.execute(f"UPDATE clips SET {', '.join(fields)} WHERE id=?", vals)
    conn.commit()
    row = conn.execute("SELECT * FROM clips WHERE id=?", (clip_id,)).fetchone()
    conn.close()
    return dict(row)

@clips_router.post("/approve-all")
def approve_all(client_id: int, min_score: int = 80):
    conn = get_conn()
    conn.execute("UPDATE clips SET status='approved' WHERE client_id=? AND score>=? AND status='pending'", (client_id, min_score))
    count = conn.execute("SELECT COUNT(*) FROM clips WHERE client_id=? AND status='approved'", (client_id,)).fetchone()[0]
    conn.commit()
    conn.close()
    return {"approved": count}

@clips_router.get("/{clip_id}/file")
def get_clip_file(clip_id: int):
    conn = get_conn()
    row = conn.execute("SELECT file_path, title FROM clips WHERE id=?", (clip_id,)).fetchone()
    conn.close()
    if not row or not os.path.exists(row["file_path"]):
        raise HTTPException(404, "Clip file not found")
    return FileResponse(row["file_path"], media_type="video/mp4", headers={"Accept-Ranges": "bytes"})

@clips_router.get("/{clip_id}/thumbnail")
def serve_thumbnail(clip_id: int):
    conn = get_conn()
    row = conn.execute("SELECT * FROM clips WHERE id=?", (clip_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "Clip not found")

    thumb = row["thumbnail_path"]

    # If thumbnail missing but clip exists, generate it
    if (not thumb or not os.path.exists(thumb)) and row["file_path"] and os.path.exists(row["file_path"]):
        thumb = row["file_path"].rsplit(".", 1)[0] + "_thumb.jpg"
        import subprocess
        subprocess.run([
            "ffmpeg", "-y", "-ss", "2", "-i", row["file_path"],
            "-vframes", "1", "-q:v", "2", "-loglevel", "quiet", thumb
        ], capture_output=True)
        if os.path.exists(thumb):
            conn = get_conn()
            conn.execute("UPDATE clips SET thumbnail_path=? WHERE id=?", (thumb, clip_id))
            conn.commit()
            conn.close()

    if not thumb or not os.path.exists(thumb):
        raise HTTPException(404, "No thumbnail")
    return FileResponse(thumb, media_type="image/jpeg")

@clips_router.delete("/{clip_id}")
def delete_clip(clip_id: int):
    conn = get_conn()
    row = conn.execute("SELECT * FROM clips WHERE id=?", (clip_id,)).fetchone()
    if row and row["file_path"] and os.path.exists(row["file_path"]):
        os.remove(row["file_path"])
    conn.execute("DELETE FROM clips WHERE id=?", (clip_id,))
    conn.commit()
    conn.close()
    return {"deleted": clip_id}


# ── Clients ────────────────────────────────────────────────────────────────
clients_router = APIRouter()

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

@clients_router.get("/")
def list_clients():
    conn = get_conn()
    rows = [dict(r) for r in conn.execute("SELECT * FROM clients ORDER BY name").fetchall()]
    for r in rows:
        r["clip_count"] = conn.execute("SELECT COUNT(*) FROM clips WHERE client_id=?", (r["id"],)).fetchone()[0]
        r["approved_count"] = conn.execute("SELECT COUNT(*) FROM clips WHERE client_id=? AND status='approved'", (r["id"],)).fetchone()[0]
    conn.close()
    return rows

@clients_router.post("/")
def create_client(body: ClientCreate):
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO clients (name,email,prospect_email,channel_url,monthly_rate,video_limit,caption_font,caption_color,auto_approve_threshold,watermark_position,default_format) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (body.name, body.email, body.prospect_email, body.channel_url, body.monthly_rate, body.video_limit, body.caption_font, body.caption_color, body.auto_approve_threshold, body.watermark_position, body.default_format)
    )
    conn.commit()
    row = conn.execute("SELECT * FROM clients WHERE id=?", (cur.lastrowid,)).fetchone()
    conn.close()
    return dict(row)

@clients_router.patch("/{client_id}")
def update_client(client_id: int, body: ClientUpdate):
    conn = get_conn()
    fields, vals = [], []
    for f, v in body.model_dump(exclude_none=True).items():
        fields.append(f"{f}=?"); vals.append(v)
    if not fields: raise HTTPException(400, "Nothing to update")
    vals.append(client_id)
    conn.execute(f"UPDATE clients SET {', '.join(fields)} WHERE id=?", vals)
    conn.commit()
    row = conn.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
    conn.close()
    return dict(row)

@clients_router.get("/{client_id}/email-draft")
def get_email_draft(client_id: int):
    """Generate the outreach email draft for a prospect client."""
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

    # Count clips
    clip_count = len(clips)
    clips_note = f"{clip_count} clip{'s' if clip_count != 1 else ''}" if clip_count > 0 else "clips"

    subject = f"We created something for you — at no cost"

    body = f"""Hi {name},

We came across your page and we have to be honest — your content is good. But it's not reaching the audience it deserves.

That's where we come in.

ClipForge is a professional video clipping service that transforms long-form content into short, high-impact clips built for TikTok, Instagram Reels, and YouTube Shorts. We handle everything — the cutting, the captions, the formatting. You just post.

We took one of your videos and created {clips_note} for you — completely free, no strings attached.

👉 View Your Free Clips Here: [Attach clips or paste Drive link]

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
        "clip_count": clip_count
    }


@clients_router.post("/{client_id}/watermark")
async def upload_watermark(client_id: int, file: UploadFile = File(...)):
    ext = file.filename.split(".")[-1].lower()
    if ext not in ["png","jpg","jpeg"]:
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
    """Upload a transparent PNG logo for a specific client."""
    if not file.filename.lower().endswith('.png'):
        raise HTTPException(400, "PNG only — must have transparent background")
    dest = WATERMARKS_DIR / f"client_{client_id}_logo.png"
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)
    conn = get_conn()
    conn.execute("UPDATE clients SET logo_path=? WHERE id=?", (str(dest), client_id))
    conn.commit()
    conn.close()
    return {"status": "ok", "logo_path": str(dest)}

@clients_router.delete("/{client_id}/watermark")
def delete_watermark(client_id: int):
    conn = get_conn()
    row = conn.execute("SELECT watermark_path FROM clients WHERE id=?", (client_id,)).fetchone()
    if row and row["watermark_path"] and os.path.exists(row["watermark_path"]):
        os.remove(row["watermark_path"])
    conn.execute("UPDATE clients SET watermark_path=NULL WHERE id=?", (client_id,))
    conn.commit()
    conn.close()
    return {"status": "ok"}

@clients_router.post("/{client_id}/reset-usage")
def reset_usage(client_id: int):
    conn = get_conn()
    conn.execute("UPDATE clients SET videos_used=0 WHERE id=?", (client_id,))
    conn.commit()
    conn.close()
    return {"status": "ok"}

@clients_router.delete("/{client_id}")
def delete_client(client_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM clients WHERE id=?", (client_id,))
    conn.commit()
    conn.close()
    return {"deleted": client_id}


# ── Jobs ───────────────────────────────────────────────────────────────────
jobs_router = APIRouter()

@jobs_router.post("/channel-demo")
def channel_demo(client_id: int = Form(...)):
    """Auto-pull video from client's channel URL and generate demo clips."""
    conn = get_conn()
    client = conn.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
    conn.close()
    if not client:
        raise HTTPException(404, "Client not found")
    channel_url = client["channel_url"]
    if not channel_url:
        raise HTTPException(400, "No channel URL set for this client. Edit the client first.")
    cur_conn = get_conn()
    cur = cur_conn.execute(
        "INSERT INTO jobs (client_id,source_url,format,burn_captions,apply_watermark,package_mode,demo_mode,status) VALUES (?,?,'16:9',1,1,1,1,'queued')",
        (client_id, channel_url)
    )
    job_id = cur.lastrowid
    cur_conn.commit()
    cur_conn.close()
    run_job(job_id)
    return {"job_id": job_id, "status": "queued"}


@jobs_router.post("/demo")
def submit_demo(client_id: int = Form(...), url: str = Form(...)):
    """Generate a demo package — 3 x 20 second clips for prospect pitching."""
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO jobs (client_id,source_url,format,burn_captions,apply_watermark,package_mode,demo_mode,status) VALUES (?,?,'16:9',1,1,1,1,'queued')",
        (client_id, url)
    )
    job_id = cur.lastrowid
    conn.commit()
    conn.close()
    run_job(job_id)
    return {"job_id": job_id, "status": "queued"}


@jobs_router.post("/submit-url")
def submit_url(
    client_id: int = Form(...),
    url: str = Form(...),
    format: str = Form("9:16"),
    burn_captions: int = Form(1),
    zoom_punch: int = Form(0),
    preview_mode: int = Form(0),
    watermark_mode: int = Form(0),
    apply_watermark: int = Form(0),
    package_mode: int = Form(0),
    demo_mode: int = Form(0),
    wm_size: str = Form("medium"),
    wm_position: str = Form("bottom_right"),
    caption_font: str = Form("Bebas Neue"),
    caption_color: str = Form("white"),
    outline_color: str = Form("black"),
    highlight_color: str = Form("yellow"),
    caption_preset: str = Form("karaoke"),
    font_size: int = Form(0),
    caption_position: str = Form("bottom"),
    process_limit: int = Form(0),
):
    conn = get_conn()
    client = conn.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
    if client and (client["video_limit"] or 0) > 0 and (client["videos_used"] or 0) >= client["video_limit"]:
        conn.close()
        raise HTTPException(429, "Client has reached their monthly limit.")
    cur = conn.execute(
        "INSERT INTO jobs (client_id,source_url,format,burn_captions,zoom_punch,preview_mode,watermark_mode,apply_watermark,package_mode,demo_mode,wm_size,wm_position,caption_font,caption_color,outline_color,highlight_color,caption_preset,font_size,caption_position,process_limit,status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'queued')",
        (client_id, url, format, burn_captions, zoom_punch, preview_mode, watermark_mode, apply_watermark, package_mode, demo_mode, wm_size, wm_position, caption_font, caption_color, outline_color, highlight_color, caption_preset, font_size, caption_position, process_limit)
    )
    job_id = cur.lastrowid
    conn.commit()
    conn.close()
    run_job(job_id)
    return {"job_id": job_id, "status": "queued"}

@jobs_router.post("/submit-file")
async def submit_file(
    client_id: int = Form(...),
    format: str = Form("9:16"),
    burn_captions: int = Form(1),
    zoom_punch: int = Form(0),
    preview_mode: int = Form(0),
    watermark_mode: int = Form(0),
    apply_watermark: int = Form(0),
    package_mode: int = Form(0),
    wm_size: str = Form("medium"),
    wm_position: str = Form("bottom_right"),
    caption_font: str = Form("Bebas Neue"),
    caption_color: str = Form("white"),
    outline_color: str = Form("black"),
    highlight_color: str = Form("yellow"),
    caption_preset: str = Form("karaoke"),
    font_size: int = Form(0),
    caption_position: str = Form("bottom"),
    process_limit: int = Form(0),
    file: UploadFile = File(...),
):
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO jobs (client_id,format,burn_captions,zoom_punch,preview_mode,watermark_mode,apply_watermark,package_mode,wm_size,wm_position,caption_font,caption_color,outline_color,highlight_color,caption_preset,font_size,caption_position,process_limit,status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'uploading')",
        (client_id, format, burn_captions, zoom_punch, preview_mode, watermark_mode, apply_watermark, package_mode, wm_size, wm_position, caption_font, caption_color, outline_color, highlight_color, caption_preset, font_size, caption_position, process_limit)
    )
    job_id = cur.lastrowid
    conn.commit()
    conn.close()
    dest = UPLOADS_DIR / str(job_id)
    dest.mkdir(parents=True, exist_ok=True)
    fp = dest / file.filename
    with open(fp, "wb") as f:
        shutil.copyfileobj(file.file, f)
    conn = get_conn()
    conn.execute("UPDATE jobs SET source_file=?, status='queued' WHERE id=?", (str(fp), job_id))
    conn.commit()
    conn.close()
    run_job(job_id)
    return {"job_id": job_id, "status": "queued"}

@jobs_router.get("/{job_id}")
def get_job(job_id: int):
    conn = get_conn()
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    conn.close()
    if not row: raise HTTPException(404, "Job not found")
    return dict(row)

@jobs_router.get("/")
def list_jobs(client_id: Optional[int] = None):
    conn = get_conn()
    if client_id:
        rows = conn.execute("SELECT * FROM jobs WHERE client_id=? ORDER BY created_at DESC", (client_id,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 50").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Previews ───────────────────────────────────────────────────────────────
previews_router = APIRouter()

class PreviewCreate(BaseModel):
    client_id: int
    job_id: Optional[int] = None
    title: Optional[str] = None
    message: Optional[str] = None

@previews_router.post("/")
def create_preview(body: PreviewCreate):
    token = secrets.token_urlsafe(12)
    conn = get_conn()
    conn.execute("INSERT INTO previews (token,client_id,job_id,title,message) VALUES (?,?,?,?,?)",
                 (token, body.client_id, body.job_id, body.title, body.message))
    conn.commit()
    conn.close()
    return {"token": token, "url": f"/preview/{token}"}

@previews_router.get("/{token}")
def get_preview(token: str):
    conn = get_conn()
    row = conn.execute("SELECT * FROM previews WHERE token=?", (token,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Preview not found")
    preview = dict(row)
    if preview["job_id"]:
        clips = [dict(r) for r in conn.execute(
            "SELECT * FROM clips WHERE job_id=? AND status='approved' ORDER BY score DESC", (preview["job_id"],)
        ).fetchall()]
    else:
        clips = [dict(r) for r in conn.execute(
            "SELECT * FROM clips WHERE client_id=? AND status='approved' ORDER BY score DESC LIMIT 10", (preview["client_id"],)
        ).fetchall()]
    conn.close()
    return {"preview": preview, "clips": clips}


# ── Debug ──────────────────────────────────────────────────────────────────
debug_router = APIRouter()

@debug_router.get("/logs")
def get_logs(job_id: Optional[int] = None, limit: int = 100):
    conn = get_conn()
    if job_id:
        rows = conn.execute("SELECT * FROM logs WHERE job_id=? ORDER BY created_at DESC LIMIT ?", (job_id, limit)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM logs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@debug_router.get("/jobs")
def all_jobs():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 20").fetchall()
    conn.close()
    return [dict(r) for r in rows]

@debug_router.get("/summary")
def summary():
    conn = get_conn()
    d = {
        "clients": conn.execute("SELECT COUNT(*) FROM clients").fetchone()[0],
        "jobs_total": conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
        "jobs_done": conn.execute("SELECT COUNT(*) FROM jobs WHERE status='done'").fetchone()[0],
        "jobs_error": conn.execute("SELECT COUNT(*) FROM jobs WHERE status='error'").fetchone()[0],
        "jobs_processing": conn.execute("SELECT COUNT(*) FROM jobs WHERE status='processing'").fetchone()[0],
        "clips_total": conn.execute("SELECT COUNT(*) FROM clips").fetchone()[0],
        "clips_approved": conn.execute("SELECT COUNT(*) FROM clips WHERE status='approved'").fetchone()[0],
    }
    conn.close()
    return d

@debug_router.get("/diagnostics")
def diagnostics():
    return run_diagnostics()

@debug_router.delete("/reset")
def reset_all():
    conn = get_conn()
    conn.executescript("DELETE FROM logs; DELETE FROM clips; DELETE FROM moments; DELETE FROM jobs; DELETE FROM previews;")
    conn.commit()
    conn.execute("UPDATE clients SET videos_used=0")
    conn.commit()
    conn.close()
    base = Path(__file__).parent.parent.parent
    for folder in ["clips", "uploads"]:
        p = base / folder
        if p.exists(): shutil.rmtree(p)
        p.mkdir()
    return {"status": "reset complete"}

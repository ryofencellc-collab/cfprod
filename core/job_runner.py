"""
ClipForge v6.0 — Job Runner
Orchestrates the full pipeline for every job type:
  - process: full pipeline, all moments
  - package: top 3 moments, 16:9
  - demo: top 3 moments, 16:9, 20s clips
  - split: sequential parts for archive content
"""
import os
import threading
import traceback
from db.database import get_conn, log, set_step
from core.engine import (
    step1_download,
    step2_transcribe,
    step3_detect_moments,
    step4_cut_clips,
    step5_burn_captions,
    step6_apply_watermark,
    step7_apply_watermark,
    step_package_mode,
    step_split_mode,
    generate_title,
)

VERSION = "6.0"


def run_job(job_id: int):
    """Start job processing in a background thread."""
    threading.Thread(target=_process, args=(job_id,), daemon=True).start()


def _process(job_id: int):
    log(job_id, f"Job {job_id} starting (ClipForge v{VERSION})...")
    conn = get_conn()

    try:
        c = conn.cursor()
        c.execute("SELECT * FROM jobs WHERE id=?", (job_id,))
        row = c.fetchone()
        if not row:
            raise RuntimeError(f"Job {job_id} not found")

        job = dict(row)
        client_id     = job["client_id"]
        fmt           = job.get("format") or "9:16"
        burn_captions = bool(job.get("burn_captions", 1))
        zoom_punch    = bool(job.get("zoom_punch", 0))
        package_mode  = bool(job.get("package_mode", 0))
        demo_mode     = bool(job.get("demo_mode", 0))
        split_mode    = bool(job.get("split_mode", 0))
        split_duration = int(job.get("split_duration") or 60)
        add_hooks     = bool(job.get("add_hooks", 0))
        font          = job.get("caption_font") or "Bebas Neue"
        text_color    = job.get("caption_color") or "white"
        outline_color = job.get("outline_color") or "black"
        highlight_color = job.get("highlight_color") or "yellow"
        preset        = job.get("caption_preset") or "karaoke"
        font_size     = job.get("font_size") or None
        position      = job.get("caption_position") or "bottom"
        apply_wm      = bool(job.get("apply_watermark", 1))
        process_limit = int(job.get("process_limit") or 0)
        whisper_model = job.get("whisper_model") or "base"

        # Load client
        c.execute("SELECT * FROM clients WHERE id=?", (client_id,))
        client_row = c.fetchone()
        client = dict(client_row) if client_row else {}

        # Watermark text — client name or ClipForge
        watermark_text = client.get("name") or "ClipForge"
        watermark_font = "Arial Rounded MT Bold"

        # Check client video limit
        limit = client.get("video_limit", 0)
        used  = client.get("videos_used", 0)
        if limit > 0 and used >= limit:
            raise RuntimeError(f"Client has reached their monthly limit of {limit} videos.")

        # ── Step 1: Download or use uploaded file ────────────────────────
        if job.get("source_url"):
            video_path = step1_download(job["source_url"], job_id)
            conn.execute("UPDATE jobs SET source_file=? WHERE id=?", (video_path, job_id))
            conn.commit()
        else:
            video_path = job.get("source_file")
            if not video_path or not os.path.exists(video_path):
                raise RuntimeError("No video file found. Upload failed or file was deleted.")
            set_step(job_id, "Using uploaded file", 25)
            log(job_id, f"Using uploaded file: {video_path}")

        # Apply process limit (trim before transcription to save time)
        if process_limit > 0:
            from core.engine import get_duration
            import subprocess as _sp
            dur = get_duration(video_path)
            limit_sec = process_limit * 60
            if dur > limit_sec:
                log(job_id, f"Trimming to first {process_limit} minutes")
                trimmed = video_path.rsplit(".", 1)[0] + "_trimmed.mp4"
                r = _sp.run([
                    "ffmpeg", "-y", "-i", video_path,
                    "-t", str(limit_sec), "-c", "copy",
                    "-loglevel", "error", trimmed
                ], capture_output=True)
                if r.returncode == 0 and os.path.exists(trimmed):
                    video_path = trimmed
                    log(job_id, f"Trimmed to {limit_sec}s")

        # ── Step 2: Transcribe ───────────────────────────────────────────
        segments = step2_transcribe(video_path, job_id, whisper_model=whisper_model)

        # ── Step 3+: Branch by mode ──────────────────────────────────────

        if split_mode:
            # SPLIT MODE — sequential parts for archive content
            log(job_id, f"Split mode — {split_duration}s parts, {fmt} format")
            final_clips = step_split_mode(
                video_path, segments, job_id,
                fmt=fmt,
                clip_duration=split_duration,
                apply_watermark_flag=apply_wm,
                watermark_text=watermark_text,
                watermark_font=watermark_font,
                font=font,
                outline_color=outline_color,
                font_size=font_size,
                position=position,
            )
            if not final_clips:
                raise RuntimeError("No clips generated in split mode.")

        elif package_mode or demo_mode:
            # PACKAGE / DEMO MODE — top 3 moments, 16:9
            log(job_id, f"{'Demo' if demo_mode else 'Package'} mode — top 3 moments, 16:9")

            # Detect moments for package/demo
            moments = step3_detect_moments(video_path, segments, job_id, num_clips=5)
            if not moments:
                raise RuntimeError("No moments detected in this video.")

            # Store moments
            for m in moments:
                conn.execute(
                    "INSERT INTO moments (job_id, start_sec, end_sec, score, transcript) VALUES (?,?,?,?,?)",
                    (job_id, m["start"], m["end"], m["score"], m["transcript"])
                )
            conn.commit()

            final_clips = step_package_mode(
                video_path, moments, segments, job_id,
                font=font,
                text_color=text_color,
                outline_color=outline_color,
                highlight_color=highlight_color,
                font_size=font_size,
                position=position,
                apply_watermark=apply_wm,
                watermark_text=watermark_text,
                watermark_font=watermark_font,
                demo_mode=demo_mode,
                add_hooks=add_hooks,
            )
            if not final_clips:
                raise RuntimeError("No clips generated.")

        else:
            # STANDARD PROCESS MODE — full pipeline, all moments
            moments = step3_detect_moments(video_path, segments, job_id, num_clips=10)
            if not moments:
                raise RuntimeError("No moments detected in this video.")

            for m in moments:
                conn.execute(
                    "INSERT INTO moments (job_id, start_sec, end_sec, score, transcript) VALUES (?,?,?,?,?)",
                    (job_id, m["start"], m["end"], m["score"], m["transcript"])
                )
            conn.commit()

            raw_clips = step4_cut_clips(video_path, moments, job_id, fmt, zoom_punch)
            if not raw_clips:
                raise RuntimeError("No clips were successfully cut.")

            if burn_captions:
                captioned = step5_burn_captions(
                    raw_clips, video_path, segments, job_id,
                    font, text_color, outline_color, preset, font_size, position
                )
            else:
                captioned = raw_clips
                log(job_id, "Captions disabled — skipping step 5")

            if apply_wm and captioned:
                final_clips = step7_apply_watermark(
                    captioned, job_id,
                    watermark_text=watermark_text,
                    watermark_font=watermark_font,
                )
            else:
                final_clips = captioned

        # ── Save clips to DB ─────────────────────────────────────────────
        set_step(job_id, "Saving clips...", 99)
        saved = 0

        for clip in final_clips:
            title = clip.get("label") or generate_title(clip.get("transcript", ""))
            moment_row = conn.execute(
                "SELECT id FROM moments WHERE job_id=? AND start_sec=?",
                (job_id, clip["start"])
            ).fetchone()
            moment_id = moment_row["id"] if moment_row else None

            conn.execute("""
                INSERT INTO clips (
                    job_id, moment_id, client_id, title, file_path, thumbnail_path,
                    start_sec, end_sec, duration_sec, score, transcript, segments_json,
                    format, status, caption_font, caption_color, outline_color,
                    caption_preset, font_size, caption_position
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                job_id, moment_id, client_id, title,
                clip["clip_path"], clip.get("thumbnail_path"),
                clip["start"], clip["end"], clip["duration"],
                clip["score"], clip.get("transcript", ""),
                clip.get("segments_json", "[]"),
                clip["format"], "pending",
                font, text_color, outline_color, preset, font_size or 0, position
            ))
            saved += 1

        conn.execute("UPDATE clients SET videos_used = videos_used + 1 WHERE id=?", (client_id,))
        conn.execute(
            "UPDATE jobs SET status='done', progress=100, current_step='Complete' WHERE id=?",
            (job_id,)
        )
        conn.commit()
        log(job_id, f"Job {job_id} complete — {saved} clips saved.")

    except Exception as e:
        err = str(e)
        log(job_id, f"FAILED: {err}", "error")
        print(traceback.format_exc())
        try:
            conn.execute(
                "UPDATE jobs SET status='error', current_step='Failed', error=? WHERE id=?",
                (err, job_id)
            )
            conn.commit()
        except Exception:
            pass
    finally:
        conn.close()

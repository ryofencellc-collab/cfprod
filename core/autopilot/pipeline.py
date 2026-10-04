"""
Archive autopilot production pipeline.

produce_single():      one licensed newsreel → YouTube Short + TikTok video +
                       a 16:9 narrated chapter saved for later compilations.
produce_compilation(): several chapters → one long-form YouTube video with
                       intro, outro and YouTube chapter timestamps.
"""
import json
import os
import random
import shutil

from config import ARCHIVE_DIR
from db.database import get_conn
from core.autopilot import render, script, sources, tts
from core.autopilot.schedule import schedule_post
from core.autopilot.settings import alog

SRC_DIR = ARCHIVE_DIR / "sources"
OUT_DIR = ARCHIVE_DIR / "episodes"

SHORT_MAX = 58.5      # YouTube Shorts sweet spot (< 60s)
TIKTOK_MIN = 61.5     # TikTok Creator Rewards needs videos over 1 minute
TIKTOK_MAX = 90.0


def _update_episode(ep_id: int, **fields):
    conn = get_conn()
    try:
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE archive_episodes SET {sets} WHERE id=?", (*fields.values(), ep_id))
        conn.commit()
    finally:
        conn.close()


def _transcribe_original(path: str) -> str:
    """Transcript of the newsreel's own narration — used only as a fact source."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return ""
    try:
        model = WhisperModel("base", device="cpu", compute_type="int8")
        segs, _ = model.transcribe(path, beam_size=1, vad_filter=True)
        return " ".join(s.text.strip() for s in segs)[:6000]
    except Exception as e:
        alog(f"Original-audio transcription skipped: {str(e)[:120]}", "warn")
        return ""


def _year(source: dict) -> str:
    return (source.get("date") or "")[:4]


def _date_label(source: dict) -> str:
    import datetime as dt
    d = sources._date_from_identifier(source.get("identifier"))
    if d:
        return f"{dt.date(d.year, d.month, d.day):%B} {d.day}, {d.year}"
    return _year(source) or "From the archive"


def _credits(source_list: list, music: dict = None) -> str:
    lines = [sources.credit_line(s) for s in source_list]
    bases = []
    for s in source_list:
        if s.get("rights_basis") and s["rights_basis"] not in bases:
            bases.append(s["rights_basis"])
    lines += bases
    lines.append(tts.VOICE_CREDIT)
    if music:
        lines.append(f"Music: {music['credit']}")
    return "\n".join(lines)


def _hashtags(tags: list, base: list) -> str:
    seen, out = set(), []
    for t in base + tags:
        h = "#" + "".join(ch for ch in t.title() if ch.isalnum())
        if len(h) > 2 and h.lower() not in seen:
            seen.add(h.lower())
            out.append(h)
    return " ".join(out[:6])


def produce_single(cfg: dict) -> int:
    """Make one episode. Returns the episode id, or raises RuntimeError."""
    conn = get_conn()
    try:
        fresh = conn.execute("SELECT COUNT(*) n FROM archive_sources WHERE status='new'").fetchone()["n"]
    finally:
        conn.close()
    if fresh < 5:
        added = sources.refresh_candidates(cfg["collections"])
        alog(f"Searched the archive — {added} new candidate items")

    source = None
    for cand in sources.pick_next_candidates(limit=15):
        try:
            source = sources.resolve_item(cand)
            break
        except ValueError as e:
            alog(f"Skipped {cand['identifier']}: {e}")
        except Exception as e:
            alog(f"Could not check {cand['identifier']}: {str(e)[:150]}", "warn")
    if not source:
        raise RuntimeError("No usable public-domain footage found — try adding another collection.")

    sources.set_source_status(source["id"], "processing")
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO archive_episodes (source_id, status, progress, kind, source_ids_json) "
            "VALUES (?, 'rendering', 'Downloading footage', 'single', ?)",
            (source["id"], json.dumps([source["id"]])))
        ep_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()
    alog(f"Episode {ep_id}: {source['title']} ({source['license_name']})")

    out_dir = OUT_DIR / str(ep_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    SRC_DIR.mkdir(parents=True, exist_ok=True)
    footage = str(SRC_DIR / f"{source['identifier']}.mp4")
    try:
        if not os.path.exists(footage):
            sources.download(source["file_url"], footage)

        _update_episode(ep_id, progress="Reading the original newsreel")
        transcript = _transcribe_original(footage)
        usable = render.usable_footage(footage)

        _update_episode(ep_id, progress="Writing narration")
        s = script.write_episode(source, transcript, usable)

        _update_episode(ep_id, progress="Recording narration")
        speaker = int(cfg.get("voice_speaker", 0))
        narr = {}
        for key in ("script_short", "script_tiktok", "script_segment"):
            wav = str(out_dir / f"{key}.wav")
            narr[key] = (wav, tts.synthesize(s[key], wav, speaker=speaker))

        style = render.pick_style()
        tracks = render.music_tracks()
        music = random.choice(tracks) if tracks else None
        badge = f"Archive · {_year(source)}" if _year(source) else "From the archive"

        _update_episode(ep_id, progress="Rendering Short")
        wav, n = narr["script_short"]
        short = render.render_vertical(footage, str(out_dir / "short.mp4"), wav, n["duration"], n["words"],
                                       s["hook"], badge, style, music, max_len=SHORT_MAX)

        _update_episode(ep_id, progress="Rendering TikTok")
        wav, n = narr["script_tiktok"]
        tiktok_style = render.pick_style()  # look different from the Short
        tiktok = render.render_vertical(footage, str(out_dir / "tiktok.mp4"), wav, n["duration"], n["words"],
                                        s["hook"], badge, tiktok_style, music,
                                        min_len=TIKTOK_MIN, max_len=TIKTOK_MAX)

        _update_episode(ep_id, progress="Rendering chapter")
        wav, n = narr["script_segment"]
        segment = render.render_segment(footage, str(out_dir / "segment.mp4"), wav, n["duration"],
                                        n["words"], s["segment_title"], _date_label(source), style, music)
        thumb = render.extract_frame(segment["path"], str(out_dir / "frame.jpg"), at=4.0)

        credits = _credits([source], music)
        description = f"{s['description']}\n\n{credits}"
        tiktok_caption = (f"{s['title_short']} — {s['description']} "
                          f"{_hashtags(s['tags'], ['history', 'archive', 'newsreel'])}\n\n"
                          f"{sources.credit_line(source)}")[:2200]
        _update_episode(
            ep_id, status="ready", progress="Ready",
            title_short=s["title_short"], segment_title=s["segment_title"], hook=s["hook"],
            description=description, tags_json=json.dumps(s["tags"]),
            script_short=s["script_short"], script_tiktok=s["script_tiktok"],
            script_long=s["script_segment"],
            short_path=short["path"], tiktok_path=tiktok["path"], segment_path=segment["path"],
            thumb_path=thumb, caption_tiktok=tiktok_caption,
        )
        sources.set_source_status(source["id"], "used")
        for f in out_dir.glob("*.wav"):
            f.unlink()
        if os.path.exists(footage):
            os.remove(footage)
    except Exception as e:
        _update_episode(ep_id, status="failed", progress="Failed", error=str(e)[:1000])
        sources.set_source_status(source["id"], "failed", str(e)[:300])
        alog(f"Episode {ep_id} failed: {str(e)[:300]}", "error")
        raise RuntimeError(str(e))

    yt_title = f"{s['title_short']} #Shorts"
    yt_body = (f"{description}\n\n{_hashtags(s['tags'], ['history', 'shorts'])}")
    if cfg.get("youtube_enabled"):
        schedule_post(ep_id, "youtube", "short", cfg, title=yt_title, body=yt_body)
    if cfg.get("tiktok_enabled"):
        schedule_post(ep_id, "tiktok", "tiktok", cfg, title=s["title_short"], body=tiktok_caption)
    alog(f"Episode {ep_id} ready: \"{s['title_short']}\" "
         f"(Short {short['duration']:.0f}s, TikTok {tiktok['duration']:.0f}s, chapter {segment['duration']:.0f}s)")
    return ep_id


def available_chapters() -> list:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT e.*, s.date AS source_date, s.identifier, s.title AS source_title, s.license_name, "
            "s.source_url, s.rights_basis "
            "FROM archive_episodes e JOIN archive_sources s ON s.id = e.source_id "
            "WHERE e.kind='single' AND e.status IN ('ready','posted') AND e.compiled_in IS NULL "
            "AND e.segment_path IS NOT NULL ORDER BY e.id"
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows if r["segment_path"] and os.path.exists(r["segment_path"])]


def _fmt_ts(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


def produce_compilation(cfg: dict) -> int:
    chapters = available_chapters()[: int(cfg["compilation_size"])]
    if len(chapters) < int(cfg["compilation_size"]):
        raise RuntimeError("Not enough chapters yet for a long-form video.")
    chapters.sort(key=lambda c: c["identifier"])  # identifiers start with the date
    for c in chapters:
        c["date"] = c["source_date"]

    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO archive_episodes (status, progress, kind, source_ids_json) "
            "VALUES ('rendering', 'Writing intro', 'compilation', ?)",
            (json.dumps([c["source_id"] for c in chapters]),))
        ep_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()
    out_dir = OUT_DIR / f"long_{ep_id}"
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        meta = script.write_compilation(chapters)
        speaker = int(cfg.get("voice_speaker", 0))
        style = render.pick_style()
        bg = chapters[0].get("thumb_path")
        years = sorted({(c["date"] or "")[:4] for c in chapters if c["date"]})
        span = f"{years[0]}–{years[-1]}" if len(years) > 1 else (years[0] if years else "")

        _update_episode(ep_id, progress="Recording intro and outro")
        intro_wav, outro_wav = str(out_dir / "intro.wav"), str(out_dir / "outro.wav")
        ni = tts.synthesize(meta["intro"], intro_wav, speaker=speaker)
        no = tts.synthesize(meta["outro"], outro_wav, speaker=speaker)
        intro = render.render_card(str(out_dir / "intro.mp4"), meta["title"],
                                   f"{len(chapters)} stories from the archive", intro_wav,
                                   ni["duration"], ni["words"], style, background=bg)
        outro = render.render_card(str(out_dir / "outro.mp4"), "Thanks for watching",
                                   "Subscribe for more from the archive", outro_wav,
                                   no["duration"], no["words"], style,
                                   background=chapters[-1].get("thumb_path"))

        _update_episode(ep_id, progress="Joining chapters")
        parts = [intro["path"]] + [c["segment_path"] for c in chapters] + [outro["path"]]
        long = render.concat_videos(parts, str(out_dir / "long.mp4"))

        # YouTube chapters: first at 0:00, each at least 10 seconds.
        t, stamps = 0.0, ["0:00 Intro"]
        t += intro["duration"]
        for c in chapters:
            stamps.append(f"{_fmt_ts(t)} {c['segment_title']}")
            t += render.get_duration(c["segment_path"])
        stamps.append(f"{_fmt_ts(t)} Outro")

        thumb = render.make_thumbnail(bg, str(out_dir / "thumb.jpg"), span or "Archive") if bg else None
        src_dicts = [{"title": c["source_title"], "date": c["date"], "license_name": c["license_name"],
                      "source_url": c["source_url"], "rights_basis": c["rights_basis"]} for c in chapters]
        description = (f"{meta['description']}\n\nChapters\n" + "\n".join(stamps) +
                       f"\n\nSources\n{_credits(src_dicts)}\n\n"
                       f"{_hashtags(meta['tags'], ['history', 'newsreel'])}")
        _update_episode(ep_id, status="ready", progress="Ready", title_long=meta["title"],
                        description=description, tags_json=json.dumps(meta["tags"]),
                        long_path=long["path"], thumb_path=thumb)
        conn = get_conn()
        try:
            conn.executemany("UPDATE archive_episodes SET compiled_in=? WHERE id=?",
                             [(ep_id, c["id"]) for c in chapters])
            conn.commit()
        finally:
            conn.close()
        for f in out_dir.glob("*.wav"):
            f.unlink()
        for p in (intro["path"], outro["path"]):
            os.remove(p)
    except Exception as e:
        _update_episode(ep_id, status="failed", progress="Failed", error=str(e)[:1000])
        alog(f"Compilation {ep_id} failed: {str(e)[:300]}", "error")
        raise RuntimeError(str(e))

    if cfg.get("youtube_enabled"):
        schedule_post(ep_id, "youtube", "long", cfg, title=meta["title"], body=description)
    alog(f"Long-form {ep_id} ready: \"{meta['title']}\" ({long['duration'] / 60:.1f} min)")
    return ep_id


def delete_episode_files(ep: dict):
    for key in ("short_path", "tiktok_path", "segment_path", "long_path", "thumb_path"):
        p = ep.get(key)
        if p and os.path.exists(p):
            os.remove(p)
    d = OUT_DIR / (f"long_{ep['id']}" if ep.get("kind") == "compilation" else str(ep["id"]))
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)

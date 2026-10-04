"""
Video rendering for the archive autopilot.

  render_vertical()  9:16 Short / TikTok: footage over a blurred fill, a slow pan,
                     hook text, year badge, narration, karaoke captions.
  render_segment()   16:9 chapter for long-form compilations.
  render_card()      16:9 intro/outro card with narration.
  concat_videos()    joins chapters into one long-form video.

Original footage audio is always dropped (newsreel soundtracks can contain
separately-owned music). Only our narration and licensed music are heard.
Visual style varies per video so the channel doesn't look templated.
"""
import json
import os
import random
import subprocess
import tempfile
from pathlib import Path

from config import MUSIC_DIR
from core.engine import build_ass_karaoke, build_hook_ass, get_duration
from core.autopilot.tts import words_to_segments

FPS = 30
CAPTION_FONTS = ["Bebas Neue", "Anton", "Archivo Black"]
HIGHLIGHTS = ["yellow", "cyan", "orange", "green"]
X264 = ["-c:v", "libx264", "-preset", "fast", "-crf", "21", "-pix_fmt", "yuv420p", "-r", str(FPS)]
AAC = ["-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2"]


def pick_style(seed=None) -> dict:
    rnd = random.Random(seed)
    return {
        "font": rnd.choice(CAPTION_FONTS),
        "highlight": rnd.choice(HIGHLIGHTS),
        "pan": rnd.choice(["left", "right", "none"]),
        "dim": rnd.choice([0.10, 0.18, 0.25]),
    }


def music_tracks() -> list:
    """Only tracks that ship with a license file next to them are used."""
    if not MUSIC_DIR.exists():
        return []
    out = []
    for p in sorted(MUSIC_DIR.iterdir()):
        if p.suffix.lower() in (".mp3", ".wav", ".m4a", ".ogg"):
            lic = p.with_suffix(".license.txt")
            if lic.exists():
                out.append({"path": str(p), "credit": lic.read_text().strip().splitlines()[0]})
    return out


def _run(cmd: list, what: str):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"{what} failed: {r.stderr[-600:]}")


def _textfile(tmp: str, name: str, text: str) -> str:
    p = os.path.join(tmp, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    return p


def _ass_file(tmp: str, words: list, offset: float, length: float, fmt: str,
              font: str, highlight: str, position: str = "bottom") -> str:
    segs = words_to_segments(words, offset)
    size = {"16:9": 66, "9:16": 84}.get(fmt)  # a bit larger than the clipping defaults
    ass = build_ass_karaoke(segs, 0.0, length, fmt, font, "black", size, position, highlight)
    if not ass:
        return None
    return _textfile(tmp, f"cap_{fmt.replace(':', 'x')}.ass", ass)


_content_cache = {}


def content_start(footage: str, scan_secs: float = 15.0) -> float:
    """Seconds to skip so we start after the newsreel's opening title card.

    Title cards carry the original studio's logo, which TikTok treats as
    unoriginal/watermarked content. The card ends at the first hard cut after
    the opening seconds; with no cut found we just skip the first 2 seconds.
    """
    if footage in _content_cache:
        return _content_cache[footage]
    start = 2.0
    try:
        import cv2
        import numpy as np
        cap = cv2.VideoCapture(footage)
        prev, t = None, 0.0
        while t <= scan_secs:
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
            ok, frame = cap.read()
            if not ok:
                break
            g = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (64, 48)).astype(np.float32)
            if prev is not None and t >= 3.0 and float(np.abs(g - prev).mean()) > 30:
                start = t + 0.3
                break
            prev, t = g, t + 0.25
        cap.release()
    except Exception:
        pass
    _content_cache[footage] = start
    return start


def _footage_plan(footage: str, footage_dur: float, needed: float, max_slow: float = 1.3):
    """Return (start, slow_factor, loop) to cover `needed` seconds of output."""
    skip = content_start(footage) if footage_dur > 20 else 0.0
    tail = 3.0 if footage_dur > 30 else 0.0   # end cards
    usable = max(1.0, footage_dur - skip - tail)
    if usable >= needed:
        return skip + random.uniform(0, min(usable - needed, 6.0)), 1.0, False
    slow = min(max_slow, needed / usable)
    return skip, slow, usable * slow < needed


def _video_input(footage: str, tmp: str, start: float, loop: bool) -> list:
    """ffmpeg input args. Looping re-uses only the clean (card-free) part."""
    if not loop:
        return ["-ss", f"{start:.2f}", "-i", footage]
    dur = get_duration(footage)
    usable = max(1.0, dur - start - (3.0 if dur > 30 else 0.0))
    trimmed = os.path.join(tmp, "loop_src.mp4")
    _run(["ffmpeg", "-y", "-ss", f"{start:.2f}", "-i", footage, "-t", f"{usable:.2f}", "-an",
          "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", "-loglevel", "error", trimmed],
         "Footage trim")
    return ["-stream_loop", "-1", "-i", trimmed]


def usable_footage(footage: str) -> float:
    dur = get_duration(footage)
    skip = content_start(footage) if dur > 20 else 0.0
    return max(1.0, dur - skip - (3.0 if dur > 30 else 0.0))


def _scale_words(words: list, factor: float) -> list:
    return [{"word": w["word"], "start": w["start"] / factor, "end": w["end"] / factor} for w in words]


def _audio_inputs(narration: str, length: float, delay: float, tempo: float, music: dict):
    """Return (extra input args, audio filter graph producing [aout])."""
    args = ["-i", narration]
    chain = f"[1:a]aresample=48000,atempo={tempo:.4f},adelay={int(delay*1000)}:all=1,apad[narr]"
    if music:
        args += ["-stream_loop", "-1", "-i", music["path"]]
        chain += (f";[2:a]aresample=48000,volume=0.10,afade=t=out:st={max(0, length-2):.2f}:d=2[mus]"
                  f";[narr][mus]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[mixed]"
                  f";[mixed]atrim=0:{length:.3f}[aout]")
    else:
        chain += f";[narr]atrim=0:{length:.3f}[aout]"
    return args, chain


def render_vertical(footage: str, out: str, narration: str, narr_dur: float, words: list,
                    hook: str, badge: str, style: dict, music: dict = None,
                    min_len: float = 0, max_len: float = 0) -> dict:
    tempo = 1.0
    if max_len and narr_dur + 1.3 > max_len:
        tempo = (narr_dur + 1.3) / max_len
        if tempo > 1.2:
            raise RuntimeError(f"Narration too long ({narr_dur:.1f}s) for a {max_len}s video")
    spoken = narr_dur / tempo
    length = max(spoken + 1.3, min_len or 0)
    if max_len:
        length = min(length, max_len)
    delay = 0.3

    fdur = get_duration(footage)
    start, slow, loop = _footage_plan(footage, fdur, length)
    pan = style["pan"]
    pan_x = {"left": f"(iw-ow)*(1-t/{length:.2f})", "right": f"(iw-ow)*t/{length:.2f}",
             "none": "(iw-ow)/2"}[pan]

    with tempfile.TemporaryDirectory() as tmp:
        ass = _ass_file(tmp, _scale_words(words, tempo), delay, length, "9:16",
                        style["font"], style["highlight"])
        hook_ass = build_hook_ass(hook, "9:16", duration=3.2, y_frac=0.19)
        hook_f = _textfile(tmp, "hook.ass", hook_ass) if hook_ass else None
        badge_f = _textfile(tmp, "badge.txt", badge.upper())
        vin = _video_input(footage, tmp, start, loop)
        v = (
            f"[0:v]setpts={slow:.4f}*(PTS-STARTPTS),fps={FPS},split[a][b];"
            f"[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
            f"boxblur=24:4,eq=brightness=-{style['dim']:.2f}:saturation=0.8[bg];"
            f"[b]scale=1188:-2,crop=1080:ih:{pan_x}:0,setsar=1[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2-40:shortest=1,"
            f"drawtext=textfile='{badge_f}':font='Archivo Black':fontsize=38:fontcolor=white@0.85:"
            f"x=(w-tw)/2:y=130:box=1:boxcolor=black@0.45:boxborderw=14"
        )
        if hook_f:
            v += f",ass='{hook_f}'"
        if ass:
            v += f",ass='{ass}'"
        v += "[vout]"
        a_args, a_chain = _audio_inputs(narration, length, delay, tempo, music)
        cmd = (["ffmpeg", "-y"] + vin + a_args +
               ["-filter_complex", f"{v};{a_chain}", "-map", "[vout]", "-map", "[aout]",
                "-t", f"{length:.3f}"] + X264 + AAC + ["-movflags", "+faststart", "-loglevel", "error", out])
        _run(cmd, "Vertical render")
    return {"path": out, "duration": get_duration(out), "tempo": tempo}


def render_segment(footage: str, out: str, narration: str, narr_dur: float, words: list,
                   chapter: str, date_label: str, style: dict, music: dict = None) -> dict:
    delay = 1.0
    fdur = get_duration(footage)
    usable = usable_footage(footage)
    # Narration plus a short tail; let the footage breathe a few extra seconds if there's more of it.
    length = narr_dur + delay + 1.2
    if usable > length:
        length = min(usable, length + 4)
    start, slow, loop = _footage_plan(footage, fdur, length, max_slow=1.4)

    with tempfile.TemporaryDirectory() as tmp:
        ass = _ass_file(tmp, words, delay, length, "16:9", style["font"], style["highlight"])
        ch_f = _textfile(tmp, "chapter.txt", chapter)
        dt_f = _textfile(tmp, "date.txt", date_label)
        vin = _video_input(footage, tmp, start, loop)
        v = (
            f"[0:v]setpts={slow:.4f}*(PTS-STARTPTS),fps={FPS},split[a][b];"
            f"[a]scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,"
            f"boxblur=30:4,eq=brightness=-0.2:saturation=0.7[bg];"
            f"[b]scale=-2:1080,setsar=1[fg];"
            f"[bg][fg]overlay=(W-w)/2:0:shortest=1,"
            f"drawbox=x=0:y=0:w=iw:h=ih:color=black@0.55:t=fill:enable='lt(t,3)',"
            f"drawtext=textfile='{ch_f}':font='Anton':fontsize=96:fontcolor=white:"
            f"x=(w-tw)/2:y=(h/2)-90:enable='lt(t,3)',"
            f"drawtext=textfile='{dt_f}':font='Archivo Black':fontsize=40:fontcolor=white@0.8:"
            f"x=(w-tw)/2:y=(h/2)+40:enable='lt(t,3)'"
        )
        if ass:
            v += f",ass='{ass}'"
        v += "[vout]"
        a_args, a_chain = _audio_inputs(narration, length, delay, 1.0, music)
        cmd = (["ffmpeg", "-y"] + vin + a_args +
               ["-filter_complex", f"{v};{a_chain}", "-map", "[vout]", "-map", "[aout]",
                "-t", f"{length:.3f}"] + X264 + AAC + ["-movflags", "+faststart", "-loglevel", "error", out])
        _run(cmd, "Segment render")
    return {"path": out, "duration": get_duration(out)}


def render_card(out: str, title: str, subtitle: str, narration: str, narr_dur: float,
                words: list, style: dict, background: str = None) -> dict:
    """16:9 card with title text over a blurred still (or dark background)."""
    delay = 0.5
    length = narr_dur + delay + 1.0
    with tempfile.TemporaryDirectory() as tmp:
        ass = _ass_file(tmp, words, delay, length, "16:9", style["font"], style["highlight"])
        t_f = _textfile(tmp, "title.txt", title)
        s_f = _textfile(tmp, "sub.txt", subtitle)
        if background:
            vin = ["-loop", "1", "-i", background]
            base = ("[0:v]scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,"
                    "boxblur=30:4,eq=brightness=-0.3,setsar=1,format=yuv420p")
        else:
            vin = ["-f", "lavfi", "-i", f"color=c=0x101010:s=1920x1080:r={FPS}"]
            base = "[0:v]format=yuv420p"
        v = (f"{base},fps={FPS},"
             f"drawtext=textfile='{t_f}':font='Anton':fontsize=88:fontcolor=white:"
             f"x=(w-tw)/2:y=(h/2)-120,"
             f"drawtext=textfile='{s_f}':font='Archivo Black':fontsize=38:fontcolor=white@0.75:"
             f"x=(w-tw)/2:y=(h/2)+10")
        if ass:
            v += f",ass='{ass}'"
        v += "[vout]"
        a_args, a_chain = _audio_inputs(narration, length, delay, 1.0, None)
        cmd = (["ffmpeg", "-y"] + vin + a_args +
               ["-filter_complex", f"{v};{a_chain}", "-map", "[vout]", "-map", "[aout]",
                "-t", f"{length:.3f}"] + X264 + AAC + ["-movflags", "+faststart", "-loglevel", "error", out])
        _run(cmd, "Card render")
    return {"path": out, "duration": get_duration(out)}


def concat_videos(paths: list, out: str) -> dict:
    """Join same-format clips (all rendered with X264/AAC settings above)."""
    with tempfile.TemporaryDirectory() as tmp:
        lst = os.path.join(tmp, "list.txt")
        with open(lst, "w") as f:
            for p in paths:
                f.write(f"file '{Path(p).resolve()}'\n")
        r = subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst,
                            "-c", "copy", "-movflags", "+faststart", "-loglevel", "error", out],
                           capture_output=True, text=True)
        if r.returncode != 0:
            _run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst] + X264 + AAC +
                 ["-movflags", "+faststart", "-loglevel", "error", out], "Concat")
    return {"path": out, "duration": get_duration(out)}


def extract_frame(video: str, out_jpg: str, at: float = 3.0) -> str:
    subprocess.run(["ffmpeg", "-y", "-ss", f"{at:.2f}", "-i", video, "-vframes", "1",
                    "-q:v", "2", "-loglevel", "quiet", out_jpg], capture_output=True)
    return out_jpg if os.path.exists(out_jpg) else None


def make_thumbnail(frame: str, out_jpg: str, text: str) -> str:
    """1280x720 thumbnail: archive frame, darkened, big title text."""
    with tempfile.TemporaryDirectory() as tmp:
        t_f = _textfile(tmp, "t.txt", text.upper())
        r = subprocess.run([
            "ffmpeg", "-y", "-i", frame, "-vf",
            "scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720,eq=contrast=1.15:brightness=-0.08,"
            f"drawtext=textfile='{t_f}':font='Anton':fontsize=110:fontcolor=white:borderw=8:bordercolor=black:"
            "x=(w-tw)/2:y=h-th-60",
            "-frames:v", "1", "-q:v", "2", "-loglevel", "error", out_jpg,
        ], capture_output=True)
    return out_jpg if r.returncode == 0 and os.path.exists(out_jpg) else None

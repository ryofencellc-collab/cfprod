"""
ClipForge — face-tracking reframe.

For vertical (9:16) and square (1:1) clips cut from wide footage, a plain
center crop often shows empty space between speakers. This module samples the
clip, finds the main face with OpenCV's YuNet detector, smooths the path, and
returns an ffmpeg crop filter whose x position follows the speaker.

Falls back (returns None) when the detector is unavailable, the source is
already narrow, or too few faces are found — the caller then uses the
regular center crop.
"""
from pathlib import Path

from config import BASE_DIR

MODEL_PATH = BASE_DIR / "models" / "face_detection_yunet_2023mar.onnx"

OUT_SIZE = {"9:16": (1080, 1920), "1:1": (1080, 1080)}
SAMPLE_FPS = 2.0
MIN_FACE_COVERAGE = 0.3     # fraction of samples that must contain a face
DEAD_ZONE = 0.08            # ignore moves smaller than this fraction of crop width
SMOOTH_WINDOW = 5           # samples in the moving average (2.5s at 2 fps)
STATIC_THRESHOLD = 0.04     # use a fixed crop if total travel is below this

_detector_cache = {}


def _detector(w: int, h: int):
    import cv2
    if not MODEL_PATH.exists():
        return None
    key = (w, h)
    if key not in _detector_cache:
        _detector_cache[key] = cv2.FaceDetectorYN.create(
            str(MODEL_PATH), "", (w, h), score_threshold=0.7
        )
    return _detector_cache[key]


def sample_face_centers(video_path: str, start: float, duration: float):
    """Return (src_w, src_h, [(t, cx_or_None), ...]) with cx in source pixels."""
    try:
        import cv2
    except ImportError:
        return None
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return None
    src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    if not src_w or not src_h:
        cap.release()
        return None

    # Detect on a downscaled frame for speed.
    scale = min(1.0, 480.0 / src_w)
    dw, dh = int(src_w * scale), int(src_h * scale)
    det = _detector(dw, dh)
    if det is None:
        cap.release()
        return None

    samples = []
    n = max(1, int(duration * SAMPLE_FPS))
    for i in range(n):
        t = i / SAMPLE_FPS
        cap.set(cv2.CAP_PROP_POS_MSEC, (start + t) * 1000.0)
        ok, frame = cap.read()
        if not ok:
            samples.append((t, None))
            continue
        small = cv2.resize(frame, (dw, dh))
        _, faces = det.detect(small)
        if faces is None or len(faces) == 0:
            samples.append((t, None))
            continue
        # Largest face = most likely the active speaker / main subject.
        x, y, fw, fh = max(faces, key=lambda f: f[2] * f[3])[:4]
        samples.append((t, (x + fw / 2.0) / scale))
    cap.release()
    return src_w, src_h, samples


def smooth_path(samples, crop_w: int, src_w: int):
    """Fill gaps, smooth, and apply a dead zone. Returns [(t, x_left_px)]."""
    found = [c for _, c in samples if c is not None]
    if not samples or len(found) < max(2, MIN_FACE_COVERAGE * len(samples)):
        return None

    # Fill gaps: hold last seen center; leading gap takes the first seen one.
    filled, last = [], found[0]
    for t, c in samples:
        if c is not None:
            last = c
        filled.append((t, last))

    # Moving average.
    half = SMOOTH_WINDOW // 2
    centers = [c for _, c in filled]
    avg = []
    for i in range(len(centers)):
        win = centers[max(0, i - half): i + half + 1]
        avg.append(sum(win) / len(win))

    # Dead zone: the virtual camera only moves for meaningful shifts.
    held, cam = [], avg[0]
    for c in avg:
        if abs(c - cam) > DEAD_ZONE * crop_w:
            cam = c
        held.append(cam)

    max_x = src_w - crop_w
    path = []
    for (t, _), c in zip(filled, held):
        x = int(min(max(c - crop_w / 2.0, 0), max_x))
        path.append((t, x))
    return path


def build_x_expr(path) -> str:
    """Piecewise-linear ffmpeg expression for x(t) through the path keyframes."""
    # Collapse consecutive equal positions into keyframes.
    keys = [path[0]]
    for t, x in path[1:]:
        if x != keys[-1][1]:
            keys.append((t, x))
    if len(keys) == 1:
        return str(keys[0][1])
    # Ease over 0.5s into each new position.
    expr = str(keys[-1][1])
    for i in range(len(keys) - 1, 0, -1):
        t1, x1 = keys[i]
        _, x0 = keys[i - 1]
        ramp = 0.5
        t0 = max(0.0, t1 - ramp)
        seg = f"if(lt(t\\,{t1:.2f})\\,{x0}+({x1}-{x0})*(t-{t0:.2f})/{ramp}\\,{expr})"
        expr = f"if(lt(t\\,{t0:.2f})\\,{x0}\\,{seg})"
    return expr


def face_track_filter(video_path: str, start: float, duration: float, fmt: str):
    """ffmpeg -vf chain for a face-following crop, or None to fall back."""
    if fmt not in OUT_SIZE:
        return None
    try:
        res = sample_face_centers(video_path, start, duration)
    except Exception as e:
        print(f"[reframe] face sampling failed: {e}")
        return None
    if not res:
        return None
    src_w, src_h, samples = res
    ow, oh = OUT_SIZE[fmt]
    crop_w = int(src_h * ow / oh) // 2 * 2
    if crop_w >= src_w:
        return None  # source is already as narrow as the target
    path = smooth_path(samples, crop_w, src_w)
    if not path:
        return None
    xs = [x for _, x in path]
    if (max(xs) - min(xs)) < STATIC_THRESHOLD * src_w:
        x_expr = str(int(sum(xs) / len(xs)))
    else:
        x_expr = build_x_expr(path)
    return f"crop={crop_w}:{src_h}:{x_expr}:0,scale={ow}:{oh},setsar=1"

import subprocess

from core.reframe import build_x_expr, smooth_path


def test_too_few_faces_falls_back():
    samples = [(i / 2, None) for i in range(20)] + [(10.0, 300.0)]
    assert smooth_path(samples, 270, 640) is None


def test_dead_zone_holds_steady_for_small_moves():
    samples = [(i / 2, 320 + (5 if i % 2 else -5)) for i in range(20)]
    path = smooth_path(samples, 270, 640)
    assert len({x for _, x in path}) == 1


def test_path_is_clamped_to_frame():
    samples = [(i / 2, 5.0) for i in range(10)] + [(5 + i / 2, 635.0) for i in range(10)]
    xs = [x for _, x in smooth_path(samples, 270, 640)]
    assert min(xs) >= 0 and max(xs) <= 640 - 270


def test_expression_is_valid_ffmpeg():
    samples = [(i / 2, 150.0 if i < 10 else 500.0) for i in range(20)]
    expr = build_x_expr(smooth_path(samples, 270, 640))
    r = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=s=640x480:d=10",
                        "-vf", f"crop=270:480:{expr}:0", "-f", "null", "-"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr

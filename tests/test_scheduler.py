from core.autopilot import scheduler, settings
from core.autopilot.publishers import youtube
from core.autopilot.schedule import to_db, utcnow
from db.database import get_conn, set_setting


def _make_episode(tmp_path):
    f = tmp_path / "short.mp4"
    f.write_bytes(b"x")
    conn = get_conn()
    cur = conn.execute("INSERT INTO archive_episodes (status, kind, short_path, tags_json) "
                       "VALUES ('ready', 'single', ?, '[\"history\"]')", (str(f),))
    ep = cur.lastrowid
    cur = conn.execute("INSERT INTO archive_posts (episode_id, platform, kind, status, scheduled_at, title, body) "
                       "VALUES (?, 'youtube', 'short', 'scheduled', ?, 'T', 'B')", (ep, to_db(utcnow())))
    post = cur.lastrowid
    conn.commit()
    conn.close()
    return ep, post


def _skip(post):
    conn = get_conn()
    conn.execute("UPDATE archive_posts SET status='skipped' WHERE id=?", (post,))
    conn.commit()
    conn.close()


def _row(table, id_):
    conn = get_conn()
    r = dict(conn.execute(f"SELECT * FROM {table} WHERE id=?", (id_,)).fetchone())
    conn.close()
    return r


def test_publisher_does_nothing_when_off(tmp_path, monkeypatch):
    settings.update({"enabled": False})
    ep, post = _make_episode(tmp_path)
    monkeypatch.setattr(youtube, "upload", lambda *a, **k: (_ for _ in ()).throw(AssertionError("posted while off")))
    scheduler.publisher_tick()
    assert _row("archive_posts", post)["status"] == "scheduled"
    _skip(post)


def test_publisher_posts_due_video(tmp_path, monkeypatch):
    settings.update({"enabled": True, "shorts_per_day": 4})
    set_setting(youtube.SETTING, {"refresh_token": "r"})
    ep, post = _make_episode(tmp_path)
    calls = []
    monkeypatch.setattr(youtube, "upload", lambda path, title, body, tags, privacy: calls.append(title) or
                        {"id": "vid1", "url": "https://youtu.be/vid1", "locked_private": False})
    scheduler.publisher_tick()
    p = _row("archive_posts", post)
    assert p["status"] == "posted" and p["external_id"] == "vid1"
    assert _row("archive_episodes", ep)["status"] == "posted"
    assert calls == ["T"]
    settings.update({"enabled": False})


def test_quota_error_reschedules(tmp_path, monkeypatch):
    settings.update({"enabled": True, "shorts_per_day": 4})
    set_setting(youtube.SETTING, {"refresh_token": "r"})
    ep, post = _make_episode(tmp_path)

    def boom(*a, **k):
        raise youtube.QuotaError("quota")
    monkeypatch.setattr(youtube, "upload", boom)
    scheduler.publisher_tick()
    p = _row("archive_posts", post)
    assert p["status"] == "scheduled" and p["scheduled_at"] > to_db(utcnow())
    _skip(post)
    settings.update({"enabled": False})


def test_waits_when_platform_not_connected(tmp_path):
    settings.update({"enabled": True})
    set_setting(youtube.SETTING, {})
    ep, post = _make_episode(tmp_path)
    scheduler.publisher_tick()
    assert _row("archive_posts", post)["status"] == "scheduled"
    _skip(post)
    settings.update({"enabled": False})

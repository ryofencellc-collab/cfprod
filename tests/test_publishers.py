import json
import os

from core.autopilot.publishers import tiktok, youtube
from db.database import set_setting


def test_tiktok_chunk_plan():
    assert tiktok.chunk_plan(3 * 1024 * 1024) == (3 * 1024 * 1024, 1)
    size, count = tiktok.chunk_plan(25 * 1024 * 1024)
    assert size == tiktok.CHUNK and count == 2      # last chunk absorbs the 5 MB remainder
    size, count = tiktok.chunk_plan(10 * 1024 * 1024)
    assert count == 1


def test_youtube_text_cleaning():
    assert youtube.clean_text("<b>Hi</b>", 100) == "‹b›Hi‹/b›"
    tags = youtube.clean_tags(["history"] * 100)
    assert sum(len(t) + 2 for t in tags) <= 450


class FakeResp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body or {}, headers or {}
        self.ok = status < 400
        self.text = json.dumps(self._body)
        self.content = self.text.encode()

    def json(self):
        return self._body

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(self.status_code)


def test_youtube_upload_sends_required_status_fields(monkeypatch, tmp_path):
    os.environ["YOUTUBE_CLIENT_ID"] = "id"
    os.environ["YOUTUBE_CLIENT_SECRET"] = "secret"
    set_setting(youtube.SETTING, {"refresh_token": "r", "access_token": "a", "expires_at": 9e12})
    video = tmp_path / "v.mp4"
    video.write_bytes(b"0" * 1000)
    sent = {}

    def fake_post(url, params=None, headers=None, json=None, timeout=None, **kw):
        sent["meta"] = json
        return FakeResp(200, {}, {"Location": "https://upload.example/session"})

    def fake_put(url, data=None, headers=None, timeout=None):
        assert url == "https://upload.example/session"
        return FakeResp(200, {"id": "abc123", "status": {"privacyStatus": "private"}})

    monkeypatch.setattr(youtube.requests, "post", fake_post)
    monkeypatch.setattr(youtube.requests, "put", fake_put)
    res = youtube.upload(str(video), "Title <x>", "Desc", ["history"], "public")
    st = sent["meta"]["status"]
    assert st["selfDeclaredMadeForKids"] is False and st["containsSyntheticMedia"] is False
    assert "<" not in sent["meta"]["snippet"]["title"]
    assert res["id"] == "abc123" and res["locked_private"] is True
    os.environ.pop("YOUTUBE_CLIENT_ID")
    os.environ.pop("YOUTUBE_CLIENT_SECRET")


def test_youtube_quota_error(monkeypatch, tmp_path):
    set_setting(youtube.SETTING, {"refresh_token": "r", "access_token": "a", "expires_at": 9e12})
    video = tmp_path / "v.mp4"
    video.write_bytes(b"0")
    monkeypatch.setattr(youtube.requests, "post", lambda *a, **k: FakeResp(
        403, {"error": {"message": "quota", "errors": [{"reason": "quotaExceeded"}]}}))
    try:
        youtube.upload(str(video), "t", "d", [], "public")
        assert False, "expected QuotaError"
    except youtube.QuotaError:
        pass

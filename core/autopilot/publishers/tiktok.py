"""
TikTok publisher — Content Posting API, Upload (draft) flow.

TikTok's Direct Post guidelines require the creator to preview each post,
pick its privacy level manually and give express consent, so unattended
public posting is not permitted. The compliant way to automate is the
Upload API: the video lands in the creator's TikTok inbox as a draft and
one tap in the TikTok app publishes it (paste the caption from the
dashboard). See docs/COMPLIANCE.md section 3.

Env: TIKTOK_CLIENT_KEY, TIKTOK_CLIENT_SECRET (app with the video.upload scope,
redirect URI = {PUBLIC_BASE_URL}/api/oauth/tiktok/callback).
"""
import math
import os
import time
import urllib.parse

import requests

from db.database import get_setting, set_setting

AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
REVOKE_URL = "https://open.tiktokapis.com/v2/oauth/revoke/"
USER_URL = "https://open.tiktokapis.com/v2/user/info/"
INBOX_INIT_URL = "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/"
STATUS_URL = "https://open.tiktokapis.com/v2/post/publish/status/fetch/"
SCOPES = "user.info.basic,video.upload"
SETTING = "tiktok_auth"

MIN_CHUNK = 5 * 1024 * 1024
CHUNK = 10 * 1024 * 1024


class RateLimitError(RuntimeError):
    pass


def configured() -> bool:
    return bool(os.environ.get("TIKTOK_CLIENT_KEY") and os.environ.get("TIKTOK_CLIENT_SECRET"))


def status() -> dict:
    auth = get_setting(SETTING) or {}
    return {
        "configured": configured(),
        "connected": bool(auth.get("refresh_token")),
        "account": auth.get("display_name"),
    }


def auth_url(redirect_uri: str, state: str) -> str:
    return AUTH_URL + "?" + urllib.parse.urlencode({
        "client_key": os.environ["TIKTOK_CLIENT_KEY"],
        "scope": SCOPES,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "state": state,
    })


def _save_tokens(tok: dict, auth: dict = None) -> dict:
    auth = dict(auth or {})
    auth.update({
        "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token", auth.get("refresh_token")),
        "open_id": tok.get("open_id", auth.get("open_id")),
        "expires_at": time.time() + int(tok.get("expires_in", 86400)) - 120,
    })
    set_setting(SETTING, auth)
    return auth


def _token_request(data: dict) -> dict:
    data = dict(data, client_key=os.environ["TIKTOK_CLIENT_KEY"],
                client_secret=os.environ["TIKTOK_CLIENT_SECRET"])
    r = requests.post(TOKEN_URL, data=data, timeout=30,
                      headers={"Content-Type": "application/x-www-form-urlencoded"})
    body = r.json() if r.content else {}
    if not r.ok or "access_token" not in body:
        raise RuntimeError(f"TikTok auth failed: {body.get('error_description') or body.get('error') or r.text[:200]}")
    return body


def exchange_code(code: str, redirect_uri: str) -> dict:
    tok = _token_request({"code": code, "grant_type": "authorization_code", "redirect_uri": redirect_uri})
    auth = _save_tokens(tok)
    try:
        u = requests.get(USER_URL, params={"fields": "open_id,display_name"},
                         headers={"Authorization": f"Bearer {auth['access_token']}"}, timeout=30)
        if u.ok:
            auth["display_name"] = u.json().get("data", {}).get("user", {}).get("display_name")
            set_setting(SETTING, auth)
    except requests.RequestException:
        pass
    return auth


def disconnect():
    auth = get_setting(SETTING) or {}
    if auth.get("access_token") and configured():
        try:
            requests.post(REVOKE_URL, data={
                "client_key": os.environ["TIKTOK_CLIENT_KEY"],
                "client_secret": os.environ["TIKTOK_CLIENT_SECRET"],
                "token": auth["access_token"]}, timeout=15)
        except requests.RequestException:
            pass
    set_setting(SETTING, {})


def _access_token() -> str:
    auth = get_setting(SETTING) or {}
    if not auth.get("refresh_token"):
        raise RuntimeError("TikTok is not connected.")
    if auth.get("access_token") and auth.get("expires_at", 0) > time.time():
        return auth["access_token"]
    tok = _token_request({"grant_type": "refresh_token", "refresh_token": auth["refresh_token"]})
    return _save_tokens(tok, auth)["access_token"]


def chunk_plan(size: int):
    """(chunk_size, total_chunk_count) per TikTok's media transfer rules:
    files under 5 MB go in one chunk; otherwise 5-64 MB chunks, with the
    remainder folded into the last chunk."""
    if size < MIN_CHUNK:
        return size, 1
    count = max(1, math.floor(size / CHUNK))
    return CHUNK, count


def _check(r: requests.Response) -> dict:
    try:
        body = r.json()
    except ValueError:
        body = {}
    err = body.get("error", {}) if isinstance(body, dict) else {}
    code = err.get("code", "ok")
    if r.status_code == 429 or code in ("rate_limit_exceeded", "spam_risk_too_many_pending_share"):
        raise RateLimitError(f"TikTok limit: {err.get('message') or code}")
    if not r.ok or code != "ok":
        raise RuntimeError(f"TikTok error {r.status_code}: {err.get('message') or code or r.text[:200]}")
    return body.get("data", {})


def upload_draft(path: str) -> dict:
    """Send a video to the creator's TikTok inbox as a draft."""
    token = _access_token()
    size = os.path.getsize(path)
    chunk_size, count = chunk_plan(size)
    data = _check(requests.post(
        INBOX_INIT_URL,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"},
        json={"source_info": {"source": "FILE_UPLOAD", "video_size": size,
                              "chunk_size": chunk_size, "total_chunk_count": count}},
        timeout=60))
    upload_url, publish_id = data["upload_url"], data["publish_id"]

    with open(path, "rb") as f:
        for i in range(count):
            start = i * chunk_size
            end = size - 1 if i == count - 1 else start + chunk_size - 1
            f.seek(start)
            body = f.read(end - start + 1)
            r = requests.put(upload_url, data=body, timeout=600, headers={
                "Content-Type": "video/mp4",
                "Content-Length": str(len(body)),
                "Content-Range": f"bytes {start}-{end}/{size}",
            })
            if r.status_code not in (200, 201, 206):
                raise RuntimeError(f"TikTok chunk {i + 1}/{count} failed: {r.status_code} {r.text[:200]}")

    state = fetch_status(publish_id)
    return {"publish_id": publish_id, "status": state}


def fetch_status(publish_id: str) -> str:
    token = _access_token()
    for _ in range(10):
        data = _check(requests.post(
            STATUS_URL, headers={"Authorization": f"Bearer {token}",
                                 "Content-Type": "application/json; charset=UTF-8"},
            json={"publish_id": publish_id}, timeout=30))
        st = data.get("status", "")
        if st == "FAILED":
            raise RuntimeError(f"TikTok processing failed: {data.get('fail_reason') or 'unknown'}")
        if st in ("SEND_TO_USER_INBOX", "PUBLISH_COMPLETE"):
            return st
        time.sleep(3)
    return "PROCESSING_UPLOAD"

"""
YouTube publisher — OAuth 2.0 + resumable upload via the YouTube Data API v3.

Uploading to your OWN channel on a schedule is allowed by the YouTube API
developer policies. Until Google completes the API compliance audit for your
Cloud project, uploads are locked to private; the dashboard shows this.

Env: YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET (OAuth client of type "Web application",
redirect URI = {PUBLIC_BASE_URL}/api/oauth/youtube/callback).
"""
import os
import time
import urllib.parse

import requests

from db.database import get_setting, set_setting

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
API = "https://www.googleapis.com/youtube/v3"
SCOPES = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly"
SETTING = "youtube_auth"
CATEGORY_EDUCATION = "27"


class QuotaError(RuntimeError):
    """Daily upload quota or upload limit reached — retry tomorrow."""


def configured() -> bool:
    return bool(os.environ.get("YOUTUBE_CLIENT_ID") and os.environ.get("YOUTUBE_CLIENT_SECRET"))


def status() -> dict:
    auth = get_setting(SETTING) or {}
    return {
        "configured": configured(),
        "connected": bool(auth.get("refresh_token")),
        "channel": auth.get("channel_title"),
        "channel_id": auth.get("channel_id"),
    }


def auth_url(redirect_uri: str, state: str) -> str:
    return AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": os.environ["YOUTUBE_CLIENT_ID"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    })


def exchange_code(code: str, redirect_uri: str) -> dict:
    r = requests.post(TOKEN_URL, data={
        "code": code,
        "client_id": os.environ["YOUTUBE_CLIENT_ID"],
        "client_secret": os.environ["YOUTUBE_CLIENT_SECRET"],
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }, timeout=30)
    r.raise_for_status()
    tok = r.json()
    if not tok.get("refresh_token"):
        raise RuntimeError("Google did not return a refresh token — remove the app at "
                           "myaccount.google.com/permissions and connect again.")
    auth = {
        "access_token": tok["access_token"],
        "refresh_token": tok["refresh_token"],
        "expires_at": time.time() + int(tok.get("expires_in", 3600)) - 60,
    }
    ch = requests.get(f"{API}/channels", params={"part": "snippet", "mine": "true"},
                      headers={"Authorization": f"Bearer {auth['access_token']}"}, timeout=30)
    if ch.ok and ch.json().get("items"):
        item = ch.json()["items"][0]
        auth["channel_id"] = item["id"]
        auth["channel_title"] = item["snippet"]["title"]
    set_setting(SETTING, auth)
    return auth


def disconnect():
    auth = get_setting(SETTING) or {}
    if auth.get("refresh_token"):
        try:
            requests.post("https://oauth2.googleapis.com/revoke",
                          params={"token": auth["refresh_token"]}, timeout=15)
        except requests.RequestException:
            pass
    set_setting(SETTING, {})


def _access_token() -> str:
    auth = get_setting(SETTING) or {}
    if not auth.get("refresh_token"):
        raise RuntimeError("YouTube is not connected.")
    if auth.get("access_token") and auth.get("expires_at", 0) > time.time():
        return auth["access_token"]
    r = requests.post(TOKEN_URL, data={
        "client_id": os.environ["YOUTUBE_CLIENT_ID"],
        "client_secret": os.environ["YOUTUBE_CLIENT_SECRET"],
        "refresh_token": auth["refresh_token"],
        "grant_type": "refresh_token",
    }, timeout=30)
    if r.status_code in (400, 401):
        raise RuntimeError("YouTube access was revoked or expired — reconnect in the dashboard.")
    r.raise_for_status()
    tok = r.json()
    auth["access_token"] = tok["access_token"]
    auth["expires_at"] = time.time() + int(tok.get("expires_in", 3600)) - 60
    set_setting(SETTING, auth)
    return auth["access_token"]


def clean_text(s: str, limit: int) -> str:
    # YouTube rejects < and > in titles/descriptions.
    return (s or "").replace("<", "‹").replace(">", "›").strip()[:limit]


def clean_tags(tags: list) -> list:
    out, total = [], 0
    for t in tags or []:
        t = clean_text(str(t), 30).replace(",", " ")
        if t and total + len(t) + 2 <= 450:
            out.append(t)
            total += len(t) + 2
    return out


def _raise_for_youtube(r: requests.Response):
    if r.ok:
        return
    try:
        err = r.json().get("error", {})
        reasons = {e.get("reason") for e in err.get("errors", [])}
        msg = err.get("message") or r.text[:300]
    except ValueError:
        reasons, msg = set(), r.text[:300]
    if reasons & {"quotaExceeded", "uploadLimitExceeded", "rateLimitExceeded", "dailyLimitExceeded"}:
        raise QuotaError(f"YouTube limit reached: {msg}")
    raise RuntimeError(f"YouTube error {r.status_code}: {msg}")


def upload(path: str, title: str, description: str, tags: list, privacy: str = "public") -> dict:
    token = _access_token()
    size = os.path.getsize(path)
    meta = {
        "snippet": {
            "title": clean_text(title, 100),
            "description": clean_text(description, 4900),
            "tags": clean_tags(tags),
            "categoryId": CATEGORY_EDUCATION,
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
            # Narration voice is production assistance, not realistic synthetic
            # media of a real person — see docs/COMPLIANCE.md section 2.
            "containsSyntheticMedia": False,
            "embeddable": True,
            "license": "youtube",
        },
    }
    init = requests.post(
        UPLOAD_URL, params={"uploadType": "resumable", "part": "snippet,status"},
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8",
                 "X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(size)},
        json=meta, timeout=60)
    _raise_for_youtube(init)
    session = init.headers["Location"]
    with open(path, "rb") as f:
        r = requests.put(session, data=f, headers={"Content-Type": "video/mp4",
                                                   "Content-Length": str(size)}, timeout=3600)
    _raise_for_youtube(r)
    video = r.json()
    vid = video["id"]
    locked = video.get("status", {}).get("privacyStatus") == "private" and privacy != "private"
    return {
        "id": vid,
        "url": f"https://www.youtube.com/watch?v={vid}",
        "privacy": video.get("status", {}).get("privacyStatus"),
        "locked_private": locked,
    }


def set_thumbnail(video_id: str, jpg: str) -> bool:
    """Custom thumbnails need a phone-verified channel; failure is non-fatal."""
    try:
        token = _access_token()
        with open(jpg, "rb") as f:
            r = requests.post("https://www.googleapis.com/upload/youtube/v3/thumbnails/set",
                              params={"videoId": video_id},
                              headers={"Authorization": f"Bearer {token}", "Content-Type": "image/jpeg"},
                              data=f, timeout=120)
        return r.ok
    except Exception:
        return False

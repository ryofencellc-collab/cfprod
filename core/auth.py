"""
ClipForge — admin authentication.

Single shared admin password (ADMIN_PASSWORD env var). A successful login sets
an HMAC-signed, HttpOnly session cookie. Everything except the public pages
listed in PUBLIC_* below requires that cookie.
"""
import base64
import hashlib
import hmac
import os
import re
import secrets
import time
from collections import defaultdict, deque

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse

from config import DATA_DIR

COOKIE_NAME = "cf_session"
SESSION_TTL = 60 * 60 * 24 * 14  # 14 days

PUBLIC_EXACT = {
    "/", "/health", "/login", "/privacy", "/terms",
    "/api/auth/login", "/api/auth/logout", "/api/leads",
    "/static/login.html", "/static/landing.html", "/static/legal.html",
    "/static/preview.html", "/static/cf_watermark.png",
}
PUBLIC_PATTERNS = [
    re.compile(r"^/preview/[A-Za-z0-9_\-]+$"),
    re.compile(r"^/api/previews/[A-Za-z0-9_\-]+$"),
    re.compile(r"^/api/previews/[A-Za-z0-9_\-]+/clips/\d+/(file|thumbnail)$"),
]


def _load_password() -> str:
    pw = os.environ.get("ADMIN_PASSWORD", "")
    if pw:
        return pw
    # No password configured: generate one so the app is never left open.
    pw_file = DATA_DIR / ".admin_password"
    if pw_file.exists():
        pw = pw_file.read_text().strip()
    else:
        pw = secrets.token_urlsafe(12)
        pw_file.write_text(pw)
    print("=" * 70)
    print(f"ADMIN_PASSWORD is not set. Generated admin password: {pw}")
    print("Set ADMIN_PASSWORD in your environment to choose your own.")
    print("=" * 70)
    return pw


def _load_secret() -> bytes:
    env = os.environ.get("SECRET_KEY", "")
    if env:
        return env.encode()
    f = DATA_DIR / ".secret_key"
    if not f.exists():
        f.write_text(secrets.token_hex(32))
    return f.read_text().strip().encode()


ADMIN_PASSWORD = _load_password()
SECRET = _load_secret()


def check_password(candidate: str) -> bool:
    return hmac.compare_digest(
        hashlib.sha256(candidate.encode()).digest(),
        hashlib.sha256(ADMIN_PASSWORD.encode()).digest(),
    )


def make_token(now: float = None) -> str:
    exp = int((now or time.time()) + SESSION_TTL)
    payload = str(exp).encode()
    sig = hmac.new(SECRET, payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(payload).decode() + "." + base64.urlsafe_b64encode(sig).decode()


def verify_token(token: str) -> bool:
    try:
        p64, s64 = token.split(".", 1)
        payload = base64.urlsafe_b64decode(p64)
        sig = base64.urlsafe_b64decode(s64)
    except Exception:
        return False
    expected = hmac.new(SECRET, payload, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        return False
    try:
        return int(payload.decode()) > time.time()
    except ValueError:
        return False


def is_public(path: str) -> bool:
    if path in PUBLIC_EXACT:
        return True
    return any(p.match(path) for p in PUBLIC_PATTERNS)


def is_authenticated(request: Request) -> bool:
    token = request.cookies.get(COOKIE_NAME)
    return bool(token) and verify_token(token)


async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if request.method == "OPTIONS" or is_public(path) or is_authenticated(request):
        return await call_next(request)
    if path.startswith("/api/") or path.startswith("/clips-files/"):
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    return RedirectResponse(f"/login?next={path}", status_code=303)


# ── Login rate limiting (per client IP) ──────────────────────────────────

_failures = defaultdict(deque)
MAX_FAILURES = 5
WINDOW_SEC = 300


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def too_many_failures(ip: str) -> bool:
    q = _failures[ip]
    cutoff = time.time() - WINDOW_SEC
    while q and q[0] < cutoff:
        q.popleft()
    return len(q) >= MAX_FAILURES


def record_failure(ip: str):
    _failures[ip].append(time.time())


def is_https(request: Request) -> bool:
    return (request.headers.get("x-forwarded-proto") or request.url.scheme) == "https"

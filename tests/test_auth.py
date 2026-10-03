import time

from core import auth


def test_token_roundtrip_and_tamper():
    tok = auth.make_token()
    assert auth.verify_token(tok)
    payload, sig = tok.split(".")
    assert not auth.verify_token(payload + "." + sig[:-2] + "AA")
    assert not auth.verify_token("garbage")


def test_token_expiry():
    old = auth.make_token(now=time.time() - auth.SESSION_TTL - 10)
    assert not auth.verify_token(old)


def test_public_paths():
    for p in ("/", "/health", "/login", "/privacy", "/terms", "/api/leads",
              "/preview/abc_DEF-1", "/api/previews/tok123", "/api/previews/tok/clips/5/file"):
        assert auth.is_public(p), p
    for p in ("/dashboard", "/autopilot", "/api/clients/", "/api/clips/1/file",
              "/static/index.html", "/api/previews/", "/clips-files/1/a.mp4"):
        assert not auth.is_public(p), p


def test_protected_routes_require_login(client):
    assert client.get("/api/clients/").status_code == 401
    r = client.get("/dashboard", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert client.get("/").status_code == 200


def test_login_flow(client):
    assert client.post("/api/auth/login", json={"password": "wrong"}).status_code == 401
    r = client.post("/api/auth/login", json={"password": "test-password", "next": "//evil.com"})
    assert r.status_code == 200 and r.json()["redirect"] == "/dashboard"
    assert client.get("/api/clients/").status_code == 200
    client.post("/api/auth/logout")
    client.cookies.clear()
    assert client.get("/api/clients/").status_code == 401


def test_login_rate_limit():
    ip = "203.0.113.9"
    for _ in range(auth.MAX_FAILURES):
        assert not auth.too_many_failures(ip)
        auth.record_failure(ip)
    assert auth.too_many_failures(ip)

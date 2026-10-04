from db.database import get_conn


def test_lead_form(client):
    assert client.post("/api/leads", json={"name": "A", "email": "a@b.co"}).status_code == 200
    assert client.post("/api/leads", json={"name": "A", "email": "nope"}).status_code == 400
    # Honeypot: accepted silently, never stored.
    client.post("/api/leads", json={"name": "bot", "email": "bot@x.io", "website": "spam"})
    conn = get_conn()
    names = [r["name"] for r in conn.execute("SELECT name FROM leads")]
    conn.close()
    assert "A" in names and "bot" not in names


def test_preview_only_exposes_approved_clips_of_that_client(authed, tmp_path):
    f = tmp_path / "c.mp4"
    f.write_bytes(b"video")
    conn = get_conn()
    conn.execute("INSERT INTO clients (id, name) VALUES (50, 'P'), (51, 'Q')")
    conn.execute("INSERT INTO clips (id, client_id, title, file_path, status) VALUES "
                 "(500, 50, 'ok', ?, 'approved'), (501, 50, 'pending', ?, 'pending'), "
                 "(502, 51, 'other', ?, 'approved')", (str(f), str(f), str(f)))
    conn.commit()
    conn.close()
    token = authed.post("/api/previews/", json={"client_id": 50}).json()["token"]
    authed.cookies.clear()
    data = authed.get(f"/api/previews/{token}").json()
    assert [c["id"] for c in data["clips"]] == [500]
    assert "file_path" not in data["clips"][0]
    assert authed.get(f"/api/previews/{token}/clips/500/file").status_code == 200
    assert authed.get(f"/api/previews/{token}/clips/501/file").status_code == 404
    assert authed.get(f"/api/previews/{token}/clips/502/file").status_code == 404
    assert authed.get("/api/clips/500/file").status_code == 401


def test_reset_requires_confirmation(authed):
    assert authed.post("/api/debug/reset").status_code == 400


def test_autopilot_status_and_settings(authed):
    s = authed.get("/api/autopilot/status").json()
    assert s["settings"]["enabled"] is False
    assert s["checks"]["ai_key"] is False
    r = authed.put("/api/autopilot/settings", json={"changes": {"shorts_per_day": 99, "youtube_privacy": "bogus"}})
    assert r.json()["shorts_per_day"] == 4
    assert r.json()["youtube_privacy"] == "public"
    # Can't start production without an AI key.
    assert authed.post("/api/autopilot/run", json={"kind": "single"}).status_code == 400


def test_oauth_start_requires_configuration(authed):
    assert authed.get("/api/oauth/youtube/start", follow_redirects=False).status_code == 400
    assert authed.get("/api/oauth/nope/start", follow_redirects=False).status_code == 404


def test_oauth_callback_rejects_bad_state(authed):
    r = authed.get("/api/oauth/youtube/callback?code=x&state=forged", follow_redirects=False)
    assert r.status_code == 303 and "error=" in r.headers["location"]

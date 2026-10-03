"""
ClipForge — Main Application
FastAPI web app, Railway-ready.

Public:   /  (sales site)  /preview/{token}  /privacy  /terms  /login
Private:  /dashboard  /autopilot  /editor/{id}  /debug  and all /api/* except
          the few public endpoints listed in core/auth.py
"""
import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from config import CLIPS_DIR, CORS_ORIGINS, STATIC_DIR, VERSION, ensure_dirs
from core import auth
from db.database import init_db
from api.routes.all_routes import (
    clips_router, clients_router, jobs_router,
    debug_router, previews_router, leads_router,
)
from api.routes.autopilot_routes import autopilot_router, oauth_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_dirs()
    init_db()
    from core.job_runner import start_workers
    start_workers()
    from core.autopilot.scheduler import start_scheduler
    start_scheduler()
    print(f"ClipForge v{VERSION} started.")
    yield


app = FastAPI(title="ClipForge", version=VERSION, lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)

# Order matters: the auth check must run inside CORS so preflights are answered.
app.middleware("http")(auth.auth_middleware)
if CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

app.include_router(clips_router,     prefix="/api/clips",     tags=["clips"])
app.include_router(clients_router,   prefix="/api/clients",   tags=["clients"])
app.include_router(jobs_router,      prefix="/api/jobs",      tags=["jobs"])
app.include_router(debug_router,     prefix="/api/debug",     tags=["debug"])
app.include_router(previews_router,  prefix="/api/previews",  tags=["previews"])
app.include_router(leads_router,     prefix="/api/leads",     tags=["leads"])
app.include_router(autopilot_router, prefix="/api/autopilot", tags=["autopilot"])
app.include_router(oauth_router,     prefix="/api/oauth",     tags=["oauth"])

app.mount("/clips-files", StaticFiles(directory=str(CLIPS_DIR)), name="clips")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ─── Auth ─────────────────────────────────────────────────────────────────

class LoginBody(BaseModel):
    password: str
    next: str = "/dashboard"


@app.post("/api/auth/login")
def login(body: LoginBody, request: Request):
    ip = auth.client_ip(request)
    if auth.too_many_failures(ip):
        return JSONResponse({"detail": "Too many attempts. Try again in a few minutes."}, status_code=429)
    if not auth.check_password(body.password):
        auth.record_failure(ip)
        return JSONResponse({"detail": "Incorrect password"}, status_code=401)
    nxt = body.next if body.next.startswith("/") and not body.next.startswith("//") else "/dashboard"
    resp = JSONResponse({"ok": True, "redirect": nxt})
    resp.set_cookie(
        auth.COOKIE_NAME, auth.make_token(),
        max_age=auth.SESSION_TTL, httponly=True, samesite="lax",
        secure=auth.is_https(request),
    )
    return resp


@app.post("/api/auth/logout")
def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE_NAME)
    return resp


# ─── Pages ────────────────────────────────────────────────────────────────

def _page(name: str) -> FileResponse:
    return FileResponse(str(STATIC_DIR / name))


@app.get("/health")
def health():
    return {"status": "ok", "version": VERSION}


@app.get("/")
def landing():
    return _page("landing.html")


@app.get("/login")
def login_page(request: Request):
    if auth.is_authenticated(request):
        return RedirectResponse("/dashboard", status_code=303)
    return _page("login.html")


@app.get("/privacy")
def privacy_page():
    return _page("legal.html")


@app.get("/terms")
def terms_page():
    return _page("legal.html")


@app.get("/dashboard")
def dashboard():
    return _page("index.html")


@app.get("/autopilot")
def autopilot_page():
    return _page("autopilot.html")


@app.get("/debug")
def debug_page():
    return _page("debug.html")


@app.get("/editor/{clip_id}")
def editor_page(clip_id: int):
    return _page("editor.html")


@app.get("/preview/{token}")
def preview_page(token: str):
    return _page("preview.html")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    reload = os.environ.get("RAILWAY_ENVIRONMENT") is None
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=reload)

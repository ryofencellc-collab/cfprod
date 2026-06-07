import uvicorn
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from db.database import init_db
from api.routes.all_routes import (
    clips_router, clients_router, jobs_router,
    debug_router, previews_router
)

VERSION = "5.5"

WORKER_PASSWORD = os.environ.get("WORKER_PASSWORD", "6969")
ADMIN_PASSWORD  = os.environ.get("ADMIN_PASSWORD",  "3757")

# Simple cookie-based auth
def check_auth(request: Request, require_admin: bool = False):
    token = request.cookies.get("cf_token", "")
    if require_admin:
        return token == f"admin_{ADMIN_PASSWORD}"
    return token in [f"worker_{WORKER_PASSWORD}", f"admin_{ADMIN_PASSWORD}"]

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    for d in ["uploads", "clips", "watermarks", "previews"]:
        os.makedirs(d, exist_ok=True)
    print(f"ClipForge v{VERSION} started.")
    yield

app = FastAPI(title="ClipForge", version=VERSION, lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

app.include_router(clips_router,    prefix="/api/clips",    tags=["clips"])
app.include_router(clients_router,  prefix="/api/clients",  tags=["clients"])
app.include_router(jobs_router,     prefix="/api/jobs",     tags=["jobs"])
app.include_router(debug_router,    prefix="/api/debug",    tags=["debug"])
app.include_router(previews_router, prefix="/api/previews", tags=["previews"])

app.mount("/clips-files", StaticFiles(directory="clips"), name="clips")
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.post("/api/auth/login")
async def login(request: Request):
    body = await request.json()
    pw = body.get("password", "")
    # Always use secure on Railway — it always runs behind HTTPS
    is_prod = os.environ.get("RAILWAY_ENVIRONMENT") is not None
    if pw == ADMIN_PASSWORD:
        resp = JSONResponse({"redirect": "/admin"})
        resp.set_cookie("cf_token", f"admin_{ADMIN_PASSWORD}", max_age=86400*30, httponly=False, secure=is_prod, samesite="lax")
        return resp
    elif pw == WORKER_PASSWORD:
        resp = JSONResponse({"redirect": "/worker"})
        resp.set_cookie("cf_token", f"worker_{WORKER_PASSWORD}", max_age=86400*30, httponly=False, secure=is_prod, samesite="lax")
        return resp
    return JSONResponse({"error": "Invalid password"}, status_code=401)

@app.get("/health")
def health():
    return {"status": "ok", "version": VERSION}

@app.get("/")
def root(request: Request):
    if check_auth(request):
        return RedirectResponse("/worker")
    return FileResponse("static/login.html")

@app.get("/worker")
def worker_page(request: Request):
    if not check_auth(request):
        return RedirectResponse("/")
    return FileResponse("static/worker.html")

@app.get("/admin")
def admin_page(request: Request):
    if not check_auth(request, require_admin=True):
        return RedirectResponse("/")
    return FileResponse("static/admin.html")

@app.get("/debug")
def debug_page(request: Request):
    if not check_auth(request, require_admin=True):
        return RedirectResponse("/")
    return FileResponse("static/debug.html")

@app.get("/editor/{clip_id}")
def editor_page(clip_id: int):
    return FileResponse("static/editor.html")

@app.get("/preview/{token}")
def preview_page(token: str):
    return FileResponse("static/preview.html")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    reload = os.environ.get("RAILWAY_ENVIRONMENT") is None
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=reload)

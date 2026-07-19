"""
ClipForge v6.0 — Main Application
FastAPI web app, Railway-ready.
"""
import uvicorn
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from db.database import init_db, VERSION as DB_VERSION
from api.routes.all_routes import (
    clips_router, clients_router, jobs_router,
    debug_router, previews_router
)

VERSION = "6.0"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    for d in ["uploads", "clips", "watermarks", "previews"]:
        os.makedirs(d, exist_ok=True)
    print(f"ClipForge v{VERSION} started.")
    yield


app = FastAPI(title="ClipForge", version=VERSION, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(clips_router,    prefix="/api/clips",    tags=["clips"])
app.include_router(clients_router,  prefix="/api/clients",  tags=["clients"])
app.include_router(jobs_router,     prefix="/api/jobs",     tags=["jobs"])
app.include_router(debug_router,    prefix="/api/debug",    tags=["debug"])
app.include_router(previews_router, prefix="/api/previews", tags=["previews"])

app.mount("/clips-files", StaticFiles(directory="clips"), name="clips")
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/health")
def health():
    return {"status": "ok", "version": VERSION}


@app.get("/debug")
def debug_page():
    return FileResponse("static/debug.html")


@app.get("/editor/{clip_id}")
def editor_page(clip_id: int):
    return FileResponse("static/editor.html")


@app.get("/preview/{token}")
def preview_page(token: str):
    return FileResponse("static/preview.html")


@app.get("/")
def root():
    return FileResponse("static/index.html")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    reload = os.environ.get("RAILWAY_ENVIRONMENT") is None
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=reload)

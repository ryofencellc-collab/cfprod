"""
ClipForge v6.0 — Database
SQLite schema with full migration support.
Every column added after v1 has a migration so existing DBs upgrade cleanly.
"""
import sqlite3
import os
from pathlib import Path

VERSION = "6.0"
DB_PATH = Path(__file__).parent.parent / "clipforge.db"


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS clients (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            name                    TEXT NOT NULL,
            email                   TEXT,
            prospect_email          TEXT,
            channel_url             TEXT,
            monthly_rate            REAL DEFAULT 0,
            video_limit             INTEGER DEFAULT 20,
            videos_used             INTEGER DEFAULT 0,
            caption_font            TEXT DEFAULT 'Bebas Neue',
            caption_color           TEXT DEFAULT 'white',
            auto_approve_threshold  INTEGER DEFAULT 0,
            watermark_path          TEXT,
            logo_path               TEXT,
            watermark_position      TEXT DEFAULT 'top_right',
            default_format          TEXT DEFAULT '9:16',
            created_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS jobs (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id           INTEGER,
            source_url          TEXT,
            source_file         TEXT,
            format              TEXT DEFAULT '9:16',
            burn_captions       INTEGER DEFAULT 1,
            zoom_punch          INTEGER DEFAULT 0,
            preview_mode        INTEGER DEFAULT 0,
            watermark_mode      INTEGER DEFAULT 0,
            apply_watermark     INTEGER DEFAULT 1,
            package_mode        INTEGER DEFAULT 0,
            demo_mode           INTEGER DEFAULT 0,
            split_mode          INTEGER DEFAULT 0,
            split_duration      INTEGER DEFAULT 60,
            add_hooks           INTEGER DEFAULT 0,
            wm_size             TEXT DEFAULT 'large',
            wm_position         TEXT DEFAULT 'top_right',
            caption_font        TEXT DEFAULT 'Bebas Neue',
            caption_color       TEXT DEFAULT 'white',
            outline_color       TEXT DEFAULT 'black',
            highlight_color     TEXT DEFAULT 'yellow',
            caption_preset      TEXT DEFAULT 'karaoke',
            font_size           INTEGER DEFAULT 0,
            caption_position    TEXT DEFAULT 'bottom',
            process_limit       INTEGER DEFAULT 0,
            whisper_model       TEXT DEFAULT 'base',
            status              TEXT DEFAULT 'queued',
            progress            INTEGER DEFAULT 0,
            current_step        TEXT DEFAULT 'Waiting',
            error               TEXT,
            created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS moments (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id      INTEGER,
            start_sec   REAL,
            end_sec     REAL,
            score       INTEGER,
            transcript  TEXT,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS clips (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id              INTEGER,
            moment_id           INTEGER,
            client_id           INTEGER,
            title               TEXT,
            file_path           TEXT,
            thumbnail_path      TEXT,
            start_sec           REAL,
            end_sec             REAL,
            duration_sec        REAL,
            score               INTEGER DEFAULT 0,
            transcript          TEXT,
            segments_json       TEXT DEFAULT '[]',
            format              TEXT DEFAULT '9:16',
            status              TEXT DEFAULT 'pending',
            caption_font        TEXT,
            caption_color       TEXT,
            outline_color       TEXT,
            caption_preset      TEXT,
            font_size           INTEGER DEFAULT 0,
            caption_position    TEXT DEFAULT 'bottom',
            created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS previews (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id   INTEGER,
            token       TEXT UNIQUE,
            title       TEXT,
            message     TEXT,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS logs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id      INTEGER,
            message     TEXT,
            level       TEXT DEFAULT 'info',
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.commit()

    # ── Migrations — safely add columns added after initial schema ────────
    _run_migrations(conn)
    conn.close()


def _run_migrations(conn):
    """Add new columns to existing tables without breaking existing data."""

    # Clients table migrations
    _add_column_if_missing(conn, "clients", "prospect_email",     "TEXT")
    _add_column_if_missing(conn, "clients", "channel_url",        "TEXT")
    _add_column_if_missing(conn, "clients", "logo_path",          "TEXT")
    _add_column_if_missing(conn, "clients", "watermark_position", "TEXT DEFAULT 'top_right'")

    # Jobs table migrations — all new v6 columns
    _add_column_if_missing(conn, "jobs", "demo_mode",      "INTEGER DEFAULT 0")
    _add_column_if_missing(conn, "jobs", "split_mode",     "INTEGER DEFAULT 0")
    _add_column_if_missing(conn, "jobs", "split_duration", "INTEGER DEFAULT 60")
    _add_column_if_missing(conn, "jobs", "add_hooks",      "INTEGER DEFAULT 0")
    _add_column_if_missing(conn, "jobs", "outline_color",  "TEXT DEFAULT 'black'")
    _add_column_if_missing(conn, "jobs", "whisper_model",  "TEXT DEFAULT 'base'")
    _add_column_if_missing(conn, "jobs", "error",          "TEXT")

    # Clips table migrations
    _add_column_if_missing(conn, "clips", "outline_color",     "TEXT")
    _add_column_if_missing(conn, "clips", "caption_position",  "TEXT DEFAULT 'bottom'")
    _add_column_if_missing(conn, "clips", "segments_json",     "TEXT DEFAULT '[]'")

    conn.commit()


def _add_column_if_missing(conn, table: str, column: str, col_def: str):
    existing = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in existing:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_def}")
        except Exception as e:
            print(f"Migration warning: {table}.{column}: {e}")


# ── Logging helpers ───────────────────────────────────────────────────────

def log(job_id: int, message: str, level: str = "info"):
    try:
        conn = get_conn()
        conn.execute(
            "INSERT INTO logs (job_id, message, level) VALUES (?,?,?)",
            (job_id, message, level)
        )
        # Update job current_step for info messages
        if level == "info":
            conn.execute(
                "UPDATE jobs SET current_step=? WHERE id=?",
                (message[:200], job_id)
            )
        conn.commit()
        conn.close()
        print(f"[v{VERSION}] [Job {job_id}] [{level.upper()}] {message}")
    except Exception as e:
        print(f"[LOG ERROR] {e}: {message}")


def set_step(job_id: int, step: str, progress: int):
    try:
        conn = get_conn()
        conn.execute(
            "UPDATE jobs SET current_step=?, progress=?, status=? WHERE id=?",
            (step, progress, "processing" if progress < 100 else "done", job_id)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[SET_STEP ERROR] {e}")

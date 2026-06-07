import sqlite3
from pathlib import Path

VERSION = "5.4"
DB_PATH = Path(__file__).parent.parent / "clipforge.db"

def get_conn():
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS clients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT,
            prospect_email TEXT,
            channel_url TEXT,
            monthly_rate REAL DEFAULT 0,
            video_limit INTEGER DEFAULT 20,
            videos_used INTEGER DEFAULT 0,
            caption_font TEXT DEFAULT 'Bebas Neue',
            caption_color TEXT DEFAULT 'white',
            auto_approve_threshold INTEGER DEFAULT 0,
            watermark_path TEXT,
            logo_path TEXT,
            watermark_position TEXT DEFAULT 'top_right',
            default_format TEXT DEFAULT '9:16',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id INTEGER NOT NULL,
            source_url TEXT,
            source_file TEXT,
            format TEXT DEFAULT '9:16',
            burn_captions INTEGER DEFAULT 1,
            zoom_punch INTEGER DEFAULT 0,
            preview_mode INTEGER DEFAULT 0,
            caption_font TEXT DEFAULT 'Bebas Neue',
            caption_color TEXT DEFAULT 'white',
            outline_color TEXT DEFAULT 'black',
            caption_preset TEXT DEFAULT 'bold',
            font_size INTEGER DEFAULT 0,
            caption_position TEXT DEFAULT 'bottom',
            apply_watermark INTEGER DEFAULT 0,
            watermark_mode INTEGER DEFAULT 0,
            package_mode INTEGER DEFAULT 0,
            logo_mode INTEGER DEFAULT 0,
            demo_mode INTEGER DEFAULT 0,
            wm_size TEXT DEFAULT 'medium',
            wm_position TEXT DEFAULT 'bottom_right',
            highlight_color TEXT DEFAULT 'yellow',
            process_limit INTEGER DEFAULT 0,
            status TEXT DEFAULT 'queued',
            current_step TEXT DEFAULT 'Waiting',
            progress INTEGER DEFAULT 0,
            error TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS moments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            start_sec REAL NOT NULL,
            end_sec REAL NOT NULL,
            score INTEGER DEFAULT 0,
            transcript TEXT,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS clips (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            moment_id INTEGER,
            client_id INTEGER NOT NULL,
            title TEXT,
            file_path TEXT,
            thumbnail_path TEXT,
            start_sec REAL,
            end_sec REAL,
            duration_sec REAL,
            score INTEGER DEFAULT 0,
            transcript TEXT,
            segments_json TEXT DEFAULT '[]',
            status TEXT DEFAULT 'pending',
            format TEXT DEFAULT '9:16',
            caption TEXT DEFAULT '',
            caption_font TEXT DEFAULT 'Bebas Neue',
            caption_color TEXT DEFAULT 'white',
            outline_color TEXT DEFAULT 'black',
            caption_preset TEXT DEFAULT 'bold',
            font_size INTEGER DEFAULT 0,
            caption_position TEXT DEFAULT 'bottom',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS previews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT UNIQUE NOT NULL,
            client_id INTEGER NOT NULL,
            job_id INTEGER,
            title TEXT,
            message TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER,
            level TEXT DEFAULT 'info',
            message TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.commit()

    # Migrations — add columns if they don't exist
    existing_jobs = [r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()]
    new_cols = {
        "zoom_punch": "ALTER TABLE jobs ADD COLUMN zoom_punch INTEGER DEFAULT 0",
        "preview_mode": "ALTER TABLE jobs ADD COLUMN preview_mode INTEGER DEFAULT 0",
        "outline_color": "ALTER TABLE jobs ADD COLUMN outline_color TEXT DEFAULT 'black'",
        "caption_preset": "ALTER TABLE jobs ADD COLUMN caption_preset TEXT DEFAULT 'bold'",
        "font_size": "ALTER TABLE jobs ADD COLUMN font_size INTEGER DEFAULT 0",
        "caption_position": "ALTER TABLE jobs ADD COLUMN caption_position TEXT DEFAULT 'bottom'",
        "apply_watermark": "ALTER TABLE jobs ADD COLUMN apply_watermark INTEGER DEFAULT 0",
        "watermark_mode": "ALTER TABLE jobs ADD COLUMN watermark_mode INTEGER DEFAULT 0",
        "package_mode": "ALTER TABLE jobs ADD COLUMN package_mode INTEGER DEFAULT 0",
        "logo_mode": "ALTER TABLE jobs ADD COLUMN logo_mode INTEGER DEFAULT 0",
        "color_test_mode": "ALTER TABLE jobs ADD COLUMN color_test_mode INTEGER DEFAULT 0",
        "font_test_mode": "ALTER TABLE jobs ADD COLUMN font_test_mode INTEGER DEFAULT 0",
        "demo_mode": "ALTER TABLE jobs ADD COLUMN demo_mode INTEGER DEFAULT 0",
        "wm_size": "ALTER TABLE jobs ADD COLUMN wm_size TEXT DEFAULT 'medium'",
        "wm_position": "ALTER TABLE jobs ADD COLUMN wm_position TEXT DEFAULT 'bottom_right'",
        "highlight_color": "ALTER TABLE jobs ADD COLUMN highlight_color TEXT DEFAULT 'yellow'",
        "process_limit": "ALTER TABLE jobs ADD COLUMN process_limit INTEGER DEFAULT 0",
    }
    for col, sql in new_cols.items():
        if col not in existing_jobs:
            conn.execute(sql)
            conn.commit()

    # Migrations for clips table
    existing_clips = [r[1] for r in conn.execute("PRAGMA table_info(clips)").fetchall()]

    # Migrations for clients table
    existing_clients = [r[1] for r in conn.execute("PRAGMA table_info(clients)").fetchall()]
    new_client_cols = {
        "logo_path": "ALTER TABLE clients ADD COLUMN logo_path TEXT",
        "watermark_position": "ALTER TABLE clients ADD COLUMN watermark_position TEXT DEFAULT 'top_right'",
        "channel_url": "ALTER TABLE clients ADD COLUMN channel_url TEXT",
        "prospect_email": "ALTER TABLE clients ADD COLUMN prospect_email TEXT",
    }
    for col, sql in new_client_cols.items():
        if col not in existing_clients:
            conn.execute(sql)
            conn.commit()
    new_clip_cols = {
        "segments_json": "ALTER TABLE clips ADD COLUMN segments_json TEXT DEFAULT '[]'",
        "caption_font": "ALTER TABLE clips ADD COLUMN caption_font TEXT DEFAULT 'Bebas Neue'",
        "caption_color": "ALTER TABLE clips ADD COLUMN caption_color TEXT DEFAULT 'white'",
        "outline_color": "ALTER TABLE clips ADD COLUMN outline_color TEXT DEFAULT 'black'",
        "caption_preset": "ALTER TABLE clips ADD COLUMN caption_preset TEXT DEFAULT 'bold'",
        "font_size": "ALTER TABLE clips ADD COLUMN font_size INTEGER DEFAULT 0",
        "caption_position": "ALTER TABLE clips ADD COLUMN caption_position TEXT DEFAULT 'bottom'",
    }
    for col, sql in new_clip_cols.items():
        if col not in existing_clips:
            conn.execute(sql)
            conn.commit()

    if conn.execute("SELECT COUNT(*) FROM clients").fetchone()[0] == 0:
        conn.executemany("INSERT INTO clients (name, email, monthly_rate) VALUES (?,?,?)", [
            ("TriniBiz TV", "trinibiz@email.com", 400.0),
            ("KayMarie", "kaymarie@email.com", 300.0),
            ("SportsPort", "sportsport@email.com", 200.0),
        ])
        conn.commit()
    conn.close()

def log(job_id, message, level="info"):
    try:
        conn = get_conn()
        conn.execute("INSERT INTO logs (job_id, level, message) VALUES (?,?,?)", (job_id, level, message))
        conn.commit()
        conn.close()
        print(f"[v{VERSION}] [Job {job_id}] [{level.upper()}] {message}")
    except Exception:
        pass

def set_step(job_id, step, progress, status="processing"):
    try:
        conn = get_conn()
        conn.execute(
            "UPDATE jobs SET current_step=?, progress=?, status=? WHERE id=?",
            (step, progress, status, job_id)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"set_step error: {e}")

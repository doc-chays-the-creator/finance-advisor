import sqlite3
import json
import os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), 'finance_advisor.db')


def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db():
    with _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS profile (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS analyses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                source_files TEXT,
                date_start TEXT,
                date_end TEXT,
                result TEXT
            )
        """)
        conn.commit()


_init_db()


def save_profile(answers: dict):
    now = datetime.now().isoformat()
    with _get_conn() as conn:
        for key, value in answers.items():
            conn.execute(
                "INSERT OR REPLACE INTO profile (key, value, updated_at) VALUES (?, ?, ?)",
                (key, json.dumps(value), now)
            )
        conn.execute(
            "INSERT OR REPLACE INTO profile (key, value, updated_at) VALUES (?, ?, ?)",
            ("last_updated", json.dumps(now), now)
        )
        conn.commit()


def load_profile() -> dict:
    with _get_conn() as conn:
        rows = conn.execute("SELECT key, value FROM profile").fetchall()
    return {row["key"]: json.loads(row["value"]) for row in rows}


def save_analysis(result: dict, file_names: list, date_range: dict = None):
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO analyses (timestamp, source_files, date_start, date_end, result) VALUES (?, ?, ?, ?, ?)",
            (
                datetime.now().isoformat(),
                json.dumps(file_names),
                date_range.get("start") if date_range else None,
                date_range.get("end") if date_range else None,
                json.dumps(result)
            )
        )
        # Keep last 12 analyses
        conn.execute("""
            DELETE FROM analyses WHERE id NOT IN (
                SELECT id FROM analyses ORDER BY id DESC LIMIT 12
            )
        """)
        conn.commit()


def load_analyses() -> list:
    with _get_conn() as conn:
        rows = conn.execute("SELECT * FROM analyses ORDER BY id DESC").fetchall()
    return [
        {
            "timestamp": row["timestamp"],
            "source_files": json.loads(row["source_files"]),
            "date_start": row["date_start"],
            "date_end": row["date_end"],
            "result": json.loads(row["result"])
        }
        for row in rows
    ]


def get_previous_analysis() -> dict:
    """Returns the most recent saved analysis, or None if no history exists."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT result, date_start, date_end FROM analyses ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if not row:
        return None
    return {
        "result": json.loads(row["result"]),
        "date_start": row["date_start"],
        "date_end": row["date_end"]
    }


def has_profile() -> bool:
    profile = load_profile()
    return bool(profile and len(profile) > 1)


def get_profile_context() -> str:
    profile = load_profile()
    if not profile:
        return ""
    lines = ["USER PROFILE (from previous sessions):"]
    skip = {"last_updated"}
    for key, value in profile.items():
        if key in skip:
            continue
        label = key.replace("_", " ").title()
        if isinstance(value, list):
            lines.append(f"  {label}: {', '.join(value)}")
        else:
            lines.append(f"  {label}: {value}")
    return "\n".join(lines)

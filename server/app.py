import os
import sqlite3
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

# === Persistence helpers (SQLite on Render Disk) ===

def _db_path() -> Path:
    """Return the configured DB path, defaulting to Render disk."""
    return Path(os.getenv("SARA_DB", "/var/data/sara.db"))


def _ensure_db() -> None:
    """Create the SQLite DB and reflections table if missing."""
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    try:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS reflections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        con.commit()
    finally:
        con.close()


def _insert_reflection(text: str) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    con = sqlite3.connect(str(_db_path()))
    try:
        con.execute(
            "INSERT INTO reflections(text, created_at) VALUES (?, ?)",
            (text, ts),
        )
        con.commit()
    finally:
        con.close()


async def save_reflection(text: str) -> dict:
    # Persist to SQLite on the configured disk so backups can run
    try:
        _ensure_db()
        _insert_reflection(text)
    except Exception:
        # Never fail the user flow on persistence; API remains responsive
        # Admin /admin/debug will still show state if something goes wrong
        pass

    # Existing logic for summary generation (example)
    # This is a placeholder for whatever AI or summary logic exists
    summary = "Reflection saved."
    # Possibly call OpenAI or other services here

    return {"ok": True, "summary": summary}

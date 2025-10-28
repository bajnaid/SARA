# server/services/ai_coach.py
import os, sqlite3, logging
from pathlib import Path
from typing import List, Dict, Any
from services import compose_current_card  # ok if services/__init__.py re-exports it

DB_PATH = Path(os.getenv("SARA_DB", "/var/data/sara.db"))

def svc_list_reflections(limit: int = 10) -> Dict[str, Any]:
    """Return the most recent reflections."""
    if not DB_PATH.exists():
        return {"ok": True, "items": []}
    con = sqlite3.connect(str(DB_PATH))
    try:
        cur = con.execute(
            "SELECT id, text, created_at FROM reflections ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        rows = [
            {"id": rid, "text": text, "created_at": created_at}
            for (rid, text, created_at) in cur.fetchall()
        ]
        return {"ok": True, "items": rows}
    finally:
        con.close()

def svc_export_reflections() -> Dict[str, Any]:
    """Return all reflections ascending (for export)."""
    if not DB_PATH.exists():
        return {"ok": True, "items": []}
    con = sqlite3.connect(str(DB_PATH))
    try:
        cur = con.execute(
            "SELECT id, text, created_at FROM reflections ORDER BY id ASC"
        )
        items = [
            {"id": rid, "text": text, "created_at": created_at}
            for (rid, text, created_at) in cur.fetchall()
        ]
        return {"ok": True, "items": items}
    finally:
        con.close()

def svc_current_card() -> dict:
    """Thin wrapper around compose_current_card (no HTTP fallback here)."""
    return compose_current_card()
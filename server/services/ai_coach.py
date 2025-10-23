from __future__ import annotations

import os
import sqlite3
import json
import io
from pathlib import Path
from fastapi.responses import StreamingResponse

# === SQLite path helper (local to this module) ===
def _db_path() -> Path:
    """
    Return the configured DB path, defaulting to Render disk.
    Mirrors the helper in app.py so this module can work independently.
    """
    return Path(os.getenv("SARA_DB", "/var/data/sara.db"))

# === Pure functions used by API layer ===

def svc_list_reflections(limit: int = 10) -> dict:
    """
    Read-only listing of the most recent reflections.
    Returns a JSON-serializable dict: {"ok": True, "items": [...]}
    """
    path = _db_path()
    if not path.exists():
        return {"ok": True, "items": []}

    rows: list[dict] = []
    con = sqlite3.connect(str(path))
    try:
        cur = con.execute(
            "SELECT id, text, created_at FROM reflections ORDER BY id DESC LIMIT ?",
            (int(limit),),
        )
        for rid, text, created_at in cur.fetchall():
            rows.append({"id": rid, "text": text, "created_at": created_at})
    finally:
        con.close()

    return {"ok": True, "items": rows}


def svc_export_reflections() -> StreamingResponse:
    """
    Stream a JSON export of all reflections (oldest first) as a downloadable file.
    """
    path = _db_path()
    payload = {"ok": True, "items": []}

    if path.exists():
        con = sqlite3.connect(str(path))
        try:
            cur = con.execute(
                "SELECT id, text, created_at FROM reflections ORDER BY id ASC"
            )
            payload["items"] = [
                {"id": rid, "text": text, "created_at": created_at}
                for rid, text, created_at in cur.fetchall()
            ]
        finally:
            con.close()

    buf = io.BytesIO(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
    headers = {"Content-Disposition": 'attachment; filename="sara-reflections.json"'}
    return StreamingResponse(buf, media_type="application/json", headers=headers)

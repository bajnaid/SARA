import os
import json
import shutil
import sqlite3
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, List

from fastapi import FastAPI, Body, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from dotenv import load_dotenv
from fastapi.responses import StreamingResponse
from io import BytesIO
from openai import OpenAI

import inspect
import logging

# Optional: import service-layer functions (no circular imports)
try:
    from services.ai_coach import (  # type: ignore
        svc_current_card,
        svc_list_reflections,
        svc_export_reflections,
    )
except Exception:
    # Safe fallbacks if import path differs
    def svc_current_card() -> dict:
        return {
            "type": "plan",
            "title": "Plan Today",
            "body": "No fixed events. 3 MITs: Sketch variant, Update deck, 45m study.",
            "cta": "Start Focus",
        }

    def svc_list_reflections(limit: int = 10):
        return {"ok": True, "items": []}

    def svc_export_reflections():
        return {"ok": True, "items": []}

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
_oai = OpenAI(api_key=OPENAI_API_KEY)

# -------------------------------------------------------------------
# Config
# -------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
HUD_DIR = BASE_DIR / "web-hud"
SARA_DB_DEFAULT = "/var/data/sara.db"
BACKUPS_DIR_DEFAULT = "/var/data/backups"

API_KEY = os.getenv("API_KEY", "").strip()

# Force-enable docs in prod
app = FastAPI(
    title="SARA MVP API",
    version="0.1.0",
    docs_url="/docs",
    redoc_url=None,
    openapi_url="/openapi.json",
)

# CORS
origins_env = os.getenv("CORS_ALLOW_ORIGINS", "*")
if origins_env.strip() == "" or origins_env.strip() == "*":
    allow_origins: List[str] = ["*"]
else:
    allow_origins = [o.strip() for o in origins_env.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve static HUD
app.mount("/hud", StaticFiles(directory=str(HUD_DIR), html=True), name="hud")

# -------------------------------------------------------------------
# Persistence helpers (SQLite on Render Disk)
# -------------------------------------------------------------------
def _db_path() -> Path:
    """Return the configured DB path, defaulting to Render disk."""
    return Path(os.getenv("SARA_DB", SARA_DB_DEFAULT))


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
    """Persist a reflection and return a simple summary."""
    try:
        _ensure_db()
        _insert_reflection(text)
    except Exception:
        # Keep API resilient; admin endpoints surface issues
        pass

    return {
        "ok": True,
        "summary": "Reflection saved. Money: Week-to-date: $96.30. Tomorrow: keep MITs <= 3; start with the hardest task.",
    }


# -------------------------------------------------------------------
# Auth helper
# -------------------------------------------------------------------
def _require_api_key(auth_header: Optional[str]) -> None:
    """If API_KEY is set, require 'Bearer &lt;key&gt;' to match."""
    if not API_KEY:
        return
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    token = auth_header.split(" ", 1)[1].strip()
    if token != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


# -------------------------------------------------------------------
# Routes
# -------------------------------------------------------------------
@app.get("/")
def root():
    return {
        "status": "ok",
        "service": "sara-mvp-api",
        "version": "0.1.0",
        "docs": "/docs",
        "health": "/api/health",
        "hud": "/hud",
    }


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/currentCard")
async def current_card():
    """Return the current card. Never 500s; falls back to a safe card on error."""
    try:
        # Call sync or async composer safely
        result = svc_current_card()
        # If the composer is an async function or returned a coroutine, await it
        if inspect.iscoroutine(result):
            result = await result
        if not isinstance(result, dict):
            raise TypeError(f"compose_current_card returned {type(result)}")
        return result
    except Exception:
        logging.exception("/api/currentCard failed; returning fallback")
        return {
            "type": "plan",
            "title": "Plan Today",
            "body": "(fallback) Endpoint error. No fixed events. 3 MITs: Sketch variant, Update deck, 45m study.",
            "cta": "Start Focus",
        }


@app.post("/api/reflect")
async def reflect(payload: dict = Body(...), authorization: Optional[str] = Header(None)):
    _require_api_key(authorization)
    text = (payload.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "No reflection text."}
    return await save_reflection(text)


@app.get("/api/reflections")
def list_reflections(limit: int = 10, authorization: Optional[str] = Header(None)):
    # Require API key if configured
    _require_api_key(authorization)
    return svc_list_reflections(limit=limit)


@app.get("/api/export")
def export_reflections(authorization: Optional[str] = Header(None)):
    # Require API key if configured
    _require_api_key(authorization)
    data = svc_export_reflections()
    return JSONResponse(
        content=data,
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="sara-reflections.json"'},
    )


@app.post("/api/tts")
async def api_tts(payload: dict = Body(...), authorization: Optional[str] = Header(None)):
    _require_api_key(authorization)
    if not OPENAI_API_KEY:
        raise HTTPException(500, "OPENAI_API_KEY missing")

    text = (payload.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "text required")

    try:
        # OpenAI TTS: "gpt-4o-mini-tts" voices: alloy, verse, coral, etc.
        resp = _oai.audio.speech.create(
            model="gpt-4o-mini-tts",
            voice=payload.get("voice", "alloy"),
            input=text,
            format="mp3",
        )
        audio_bytes = resp.read()
        buf = BytesIO(audio_bytes)
        return StreamingResponse(buf, media_type="audio/mpeg")
    except Exception as e:
        logging.exception("TTS failed")
        raise HTTPException(500, f"TTS error: {e}")

@app.post("/api/stt")
async def api_stt(authorization: Optional[str] = Header(None)):
    _require_api_key(authorization)
    if not OPENAI_API_KEY:
        raise HTTPException(500, "OPENAI_API_KEY missing")

    from fastapi import UploadFile, File

    async def _read(file: UploadFile = File(...)):
        return file

    file = await _read()  # type: ignore
    try:
        tr = _oai.audio.transcriptions.create(
            model="whisper-1",
            file=(file.filename, await file.read(), file.content_type or "audio/mpeg"),
        )
        return {"ok": True, "text": tr.text}
    except Exception as e:
        logging.exception("STT failed")
        raise HTTPException(500, f"STT error: {e}")


# ------------------- Admin (backups) -------------------
@app.get("/admin/debug")
def admin_debug(authorization: Optional[str] = Header(None)):
    _require_api_key(authorization)
    db = _db_path()
    return {
        "ok": True,
        "db_path": str(db),
        "db_exists": db.exists(),
        "db_size": db.stat().st_size if db.exists() else 0,
        "backups_dir": os.getenv("SARA_BACKUPS_DIR", BACKUPS_DIR_DEFAULT),
        "backups_count": len(list(Path(os.getenv("SARA_BACKUPS_DIR", BACKUPS_DIR_DEFAULT)).glob("*.db"))) if Path(os.getenv("SARA_BACKUPS_DIR", BACKUPS_DIR_DEFAULT)).exists() else 0,
    }


@app.post("/admin/backup")
def admin_backup(authorization: Optional[str] = Header(None)):
    _require_api_key(authorization)
    db = _db_path()
    if not db.exists():
        raise HTTPException(status_code=404, detail=f"DB not found at {db}")
    backups_dir = Path(os.getenv("SARA_BACKUPS_DIR", BACKUPS_DIR_DEFAULT))
    backups_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    dest = backups_dir / f"sara-{ts}.db"
    shutil.copyfile(str(db), str(dest))
    return {"ok": True, "backup": str(dest)}


@app.get("/admin/backups")
def admin_backups(authorization: Optional[str] = Header(None)):
    _require_api_key(authorization)
    backups_dir = Path(os.getenv("SARA_BACKUPS_DIR", BACKUPS_DIR_DEFAULT))
    backups_dir.mkdir(parents=True, exist_ok=True)
    files = sorted([str(p) for p in backups_dir.glob("*.db")])
    return {"ok": True, "backups": files}


# Local dev entrypoint
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "8000")))

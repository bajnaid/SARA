import os
from pathlib import Path
from fastapi import FastAPI, Body, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from services.cards import compose_current_card, save_reflection
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from datetime import datetime, timezone
import shutil, time

load_dotenv()
# Explicitly enable docs in prod and pin paths.
app = FastAPI(
    title="SARA MVP API",
    version="0.1.0",
    docs_url="/docs",
    redoc_url=None,
    openapi_url="/openapi.json",
)

BASE_DIR = Path(__file__).parent
HUD_DIR = BASE_DIR / "web-hud"

app.mount("/hud", StaticFiles(directory=str(HUD_DIR), html=True), name="hud")


# CORS: allow all by default, or override via CORS_ALLOW_ORIGINS="https://foo,https://bar"
_origin_env = os.getenv("CORS_ALLOW_ORIGINS", "*")
if _origin_env.strip() == "*":
    _allow_origins = ["*"]
else:
    _allow_origins = [o.strip() for o in _origin_env.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", tags=["meta"], include_in_schema=False)
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
async def health():
    return {"ok": True}

@app.get("/api/currentCard")
async def current_card():
    return await compose_current_card()

@app.post("/api/reflect")
async def reflect(
    payload: dict = Body(...),
    authorization: str | None = Header(None),
):
    # If an API_KEY is set, require it as a Bearer token.
    expected = os.getenv("API_KEY")
    if expected:
        token = None
        if authorization and authorization.lower().startswith("bearer "):
            token = authorization.split(None, 1)[1].strip()
        if token != expected:
            raise HTTPException(status_code=401, detail="Unauthorized")

    text = (payload.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "No reflection text."}
    return await save_reflection(text)


# === Backup admin endpoints ===
def _require_api(authorization: str | None):
    expected = os.getenv("API_KEY")
    if not expected:
        raise HTTPException(status_code=403, detail="API_KEY not set on server")
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(None, 1)[1].strip()
    if token != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")

def _backups_dir() -> Path:
    db_path = Path(os.getenv("SARA_DB", "/var/data/sara.db"))
    d = db_path.parent / "backups"
    d.mkdir(parents=True, exist_ok=True)
    return d

@app.post("/admin/backup", include_in_schema=False)
def admin_backup(authorization: str | None = Header(None)):
    _require_api(authorization)
    # create timestamped copy and prune old
    db_path = Path(os.getenv("SARA_DB", "/var/data/sara.db"))
    backups_dir = _backups_dir()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M")
    dst = backups_dir / f"sara-{now}.db"
    shutil.copy2(db_path, dst)

    cutoff = time.time() - 14 * 24 * 3600
    removed = []
    for p in backups_dir.glob("sara-*.db"):
        if p.stat().st_mtime < cutoff:
            try:
                p.unlink()
                removed.append(p.name)
            except Exception:
                pass

    return {"ok": True, "created": str(dst.name), "pruned": removed}

@app.get("/admin/backups", include_in_schema=False)
def admin_list_backups(authorization: str | None = Header(None)):
    _require_api(authorization)
    items = []
    for p in sorted(_backups_dir().glob("sara-*.db"), key=lambda x: x.stat().st_mtime, reverse=True):
        st = p.stat()
        items.append({
            "name": p.name,
            "bytes": st.st_size,
            "mtime": st.st_mtime,
        })
    return {"ok": True, "backups": items}

@app.get("/admin/backup/{name}", include_in_schema=False)
def admin_download_backup(name: str, authorization: str | None = Header(None)):
    _require_api(authorization)
    # allow only files inside backups dir with expected pattern
    if not (name.startswith("sara-") and name.endswith(".db")):
        raise HTTPException(status_code=400, detail="invalid name")
    path = _backups_dir() / name
    if not path.exists():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(str(path), filename=name, media_type="application/octet-stream")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.getenv("HOST","0.0.0.0"), port=int(os.getenv("PORT","8000")))

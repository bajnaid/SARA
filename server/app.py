import os
import json
import shutil
import sqlite3
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional, List

from fastapi import FastAPI, Body, Header, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from dotenv import load_dotenv
from fastapi.responses import StreamingResponse
from io import BytesIO
from openai import OpenAI

import inspect
import logging
import base64

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
ENFORCE_API_KEY = os.getenv("ENFORCE_API_KEY", "true").strip().lower() == "true"

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
    expose_headers=["X-Conversation-Id", "X-Reply-Text-B64"],
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
        # Conversations table (no updated_at)
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL,
                role TEXT NOT NULL,          -- 'user' | 'assistant' | 'system'
                text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
            )
            """
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_messages_conv_time ON messages(conversation_id, created_at)")
        # Transactions table for finance tracking
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT DEFAULT 'default',
                created_at TEXT NOT NULL,
                amount_cents INTEGER NOT NULL,
                currency TEXT NOT NULL,
                merchant TEXT,
                raw_input TEXT NOT NULL,
                category TEXT,
                emotion TEXT,
                notes TEXT
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


# Ensure DB schema exists at import/startup so chat persistence is always available
try:
    _ensure_db()
except Exception:
    logging.exception("DB init failed at startup")


# -------------------------------------------------------------------
# Conversations & Messages helpers
# -------------------------------------------------------------------
def _create_conversation(title: str = "") -> int:
    ts = datetime.now(timezone.utc).isoformat()
    con = sqlite3.connect(str(_db_path()))
    try:
        cur = con.cursor()
        cur.execute(
            "INSERT INTO conversations(title, created_at) VALUES (?, ?)",
            (title.strip() or None, ts),
        )
        con.commit()
        return int(cur.lastrowid)
    finally:
        con.close()

def _insert_message(conversation_id: int, role: str, text: str) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    con = sqlite3.connect(str(_db_path()))
    try:
        con.execute(
            "INSERT INTO messages(conversation_id, role, text, created_at) VALUES (?, ?, ?, ?)",
            (conversation_id, role, text, ts),
        )
        con.commit()
    finally:
        con.close()

def _list_conversations(limit: int = 20) -> list[dict]:
    con = sqlite3.connect(str(_db_path()))
    try:
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        cur.execute(
            """
            SELECT c.id,
                   COALESCE(c.title, '(untitled)') AS title,
                   c.created_at,
                   (
                     SELECT m.text
                     FROM messages m
                     WHERE m.conversation_id = c.id
                     ORDER BY m.id DESC
                     LIMIT 1
                   ) AS last_text
            FROM conversations c
            ORDER BY datetime(c.created_at) DESC, c.id DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()

def _list_messages(conversation_id: int, limit: int = 50) -> list[dict]:
    con = sqlite3.connect(str(_db_path()))
    try:
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        cur.execute(
            """
            SELECT id, role, text, created_at
            FROM messages
            WHERE conversation_id=?
            ORDER BY id ASC
            LIMIT ?
            """,
            (conversation_id, limit),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        con.close()


# --- Conversation management helpers ---
def _rename_conversation(conversation_id: int, title: str) -> None:
    """Rename a conversation."""
    con = sqlite3.connect(str(_db_path()))
    try:
        con.execute(
            "UPDATE conversations SET title = ? WHERE id = ?",
            (title.strip() or None, conversation_id),
        )
        con.commit()
    finally:
        con.close()


def _delete_conversation(conversation_id: int) -> None:
    """Delete a conversation and its messages."""
    con = sqlite3.connect(str(_db_path()))
    try:
        con.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
        con.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
        con.commit()
    finally:
        con.close()


# -------------------------------------------------------------------
# Finance helpers (transactions + parsing)
# -------------------------------------------------------------------

_FINANCE_SYSTEM_PROMPT = """
You are a transaction parser for a personal finance assistant.

Input: a short text like "record $3 espresso at Mike's yesterday"
or "I spent 20 SAR on Uber".

Output: a single JSON object with:
- amount: float
- currency: 3-letter code (USD, SAR, EUR). Default to USD if not specified.
- merchant: short string (e.g., "Mike's", "Uber", "Bodega")
- category: one of [coffee, food, groceries, transport, subscriptions, shopping, bills, rent, fun, other]
- created_at_iso: ISO8601 timestamp in the user's local time. If they say "yesterday" or "last week", infer date.
- notes: short free-text note if needed.

If something is ambiguous, make a best guess.
Always return VALID JSON only, no extra text.
"""

# Very simple monthly budget by category (USD).
# You can tweak these numbers whenever you want.
MONTHLY_BUDGET_BY_CATEGORY: dict[str, float] = {
    "coffee": 100.0,
    "food": 300.0,
    "groceries": 200.0,
    "transport": 150.0,
    "subscriptions": 110.0,
    "rent": 875.0,
    "college": 150.0,
    "fun": 100.0,
    "other": 150.0,
}


def _parse_transaction_input(user_text: str) -> dict:
    """Use OpenAI to turn natural text into a structured transaction dict."""
    try:
        resp = _oai.chat.completions.create(
            model="gpt-4o-mini",
            response_format={"type": "json_object"},
            temperature=0,
            messages=[
                {"role": "system", "content": _FINANCE_SYSTEM_PROMPT},
                {"role": "user", "content": user_text},
            ],
        )
        content = (resp.choices[0].message.content or "").strip()
        data = json.loads(content or "{}")
    except Exception:
        logging.exception("finance parse failed; falling back to simple parse")
        # ultra-dumb fallback: try to extract first number
        data = {}
        tokens = user_text.replace("$", " ").split()
        amount = 0.0
        for t in tokens:
            try:
                amount = float(t)
                break
            except Exception:
                continue
        data.setdefault("amount", amount or 0.0)
        data.setdefault("currency", "USD")
        data.setdefault("merchant", "")
        data.setdefault("category", "other")
        data.setdefault("created_at_iso", datetime.now(timezone.utc).isoformat())
        data.setdefault("notes", "")

    # Hard fallbacks / normalization from the model
    if "amount" not in data:
        data["amount"] = 0.0
    if "currency" not in data or not data["currency"]:
        data["currency"] = "USD"
    if "created_at_iso" not in data or not data["created_at_iso"]:
        data["created_at_iso"] = datetime.now(timezone.utc).isoformat()
    if "category" not in data or not data["category"]:
        data["category"] = "other"
    if "notes" not in data:
        data["notes"] = ""

    # ------------------------------------------------------------------
    # Heuristics to clean up categories & merchant using the raw text
    # ------------------------------------------------------------------
    text_lower = user_text.lower()

    # Coffee
    coffee_keywords = [
        "coffee", "latte", "espresso", "cortado", "americano",
        "flat white", "cold brew", "drip", "brew",
    ]
    if any(k in text_lower for k in coffee_keywords):
        data["category"] = "coffee"

    # Transport
    transport_keywords = [
        "uber", "lyft", "taxi", "cab", "mta", "subway", "train",
        "bus", "metro", "ride",
    ]
    if any(k in text_lower for k in transport_keywords):
        data["category"] = "transport"

    # Groceries
    grocery_keywords = [
        "grocery", "groceries", "supermarket", "whole foods",
        "trader joe", "bodega", "market",
    ]
    if any(k in text_lower for k in grocery_keywords):
        data["category"] = "groceries"

    # Subscriptions
    subscription_keywords = [
        "spotify", "netflix", "apple music", "icloud",
        "adobe", "notion", "mubi", "hbo", "max", "youtube premium",
    ]
    if any(k in text_lower for k in subscription_keywords):
        data["category"] = "subscriptions"

    # Rent / housing
    rent_keywords = ["rent", "landlord", "apartment", "room"]
    if any(k in text_lower for k in rent_keywords):
        data["category"] = "rent"

    # College / school
    college_keywords = ["tuition", "pratt", "course", "class", "college", "registration"]
    if any(k in text_lower for k in college_keywords):
        data["category"] = "college"

    # Food separate from groceries/coffee (restaurants, takeout, etc.)
    food_keywords = [
        "burger", "pizza", "taco", "tacos", "sushi", "ramen",
        "dinner", "lunch", "breakfast", "takeout", "delivery",
    ]
    if any(k in text_lower for k in food_keywords) and data.get("category") not in ("coffee", "groceries"):
        data["category"] = "food"

    # Merchant fallback: never leave it empty
    if not str(data.get("merchant", "")).strip():
        data["merchant"] = "unknown"

    return data

# -------------------------------------------------------------------
# Auth helper
# -------------------------------------------------------------------
def _require_api_key(auth_header: Optional[str]) -> None:
    """
    If ENFORCE_API_KEY is true AND API_KEY is set, require 'Bearer <key>'.
    When ENFORCE_API_KEY is false, this is a no-op to make the MVP publicly usable.
    """
    # Short‑circuit if not enforcing or if no API key is configured
    if not ENFORCE_API_KEY or not API_KEY:
        return

    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")

    token = auth_header.split(" ", 1)[1].strip()
    if token != API_KEY:
        # Use 403 to indicate provided credentials are not acceptable
        raise HTTPException(status_code=403, detail="Invalid API key")


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
        # OpenAI TTS: "gpt-4o-mini-tts" default output is mp3 bytes
        resp = _oai.audio.speech.create(
            model="gpt-4o-mini-tts",
            voice=payload.get("voice", "alloy"),
            input=text,
        )
        audio_bytes = resp.read()  # bytes
        buf = BytesIO(audio_bytes)
        return StreamingResponse(buf, media_type="audio/mpeg")
    except Exception as e:
        logging.exception("TTS failed")
        raise HTTPException(500, f"TTS error: {e}")

@app.post("/api/stt")
async def api_stt(
    authorization: Optional[str] = Header(None),
    audio: UploadFile = File(...),
):
    _require_api_key(authorization)
    if not OPENAI_API_KEY:
        raise HTTPException(500, "OPENAI_API_KEY missing")

    try:
        data = await audio.read()
        filename = audio.filename or "mic.webm"
        mime = audio.content_type or "audio/webm"

        tr = _oai.audio.transcriptions.create(
            model="whisper-1",
            file=(filename, data, mime),
        )
        return {"ok": True, "text": tr.text}
    except Exception as e:
        logging.exception("STT failed")
        raise HTTPException(500, f"STT error: {e}")



# Helper to run chat completion and persist both turns
def _chat_and_persist(user_text: str, conv_id: Optional[int]) -> tuple[int, str]:
    """
    Runs chat completion with the current system prompt, persists user and assistant turns.
    Returns (conversation_id, reply_text).
    """
    if not OPENAI_API_KEY:
        raise HTTPException(500, "OPENAI_API_KEY missing")

    text = (user_text or "").strip()
    if not text:
        raise HTTPException(400, "text required")

    # conversation id handling
    try:
        cid = int(conv_id) if conv_id is not None else None
    except Exception:
        cid = None
    if not cid:
        _ensure_db()
        cid = _create_conversation(title=text[:120])

    # persist user message
    try:
        _insert_message(cid, "user", text)
    except Exception:
        logging.exception("Failed to persist user message")

    # system style
    system = (
        "You are S.A.R.A., a concise, warm assistant and coach. "
        "Reply in 1–3 short sentences unless more detail is requested. "
        "Prefer direct, helpful answers with a clear next step."
    )
    # run model
    try:
        resp = _oai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": text},
            ],
            temperature=0.4,
        )
        reply = (resp.choices[0].message.content or "").strip()
        if not reply:
            reply = "I’m here. Try asking me again with a bit more detail?"
    except Exception as e:
        logging.exception("CHAT failed")
        raise HTTPException(500, f"Chat error: {e}")

    # persist assistant message
    try:
        _insert_message(cid, "assistant", reply)
    except Exception:
        logging.exception("Failed to persist assistant message")

    return cid, reply


# New concise chat endpoint with persistence and conversations
@app.post("/api/chat")
async def api_chat(payload: dict = Body(...), authorization: Optional[str] = Header(None)):
    _require_api_key(authorization)
    user_text = (payload.get("text") or "").strip()
    conv_id_in = payload.get("conversation_id")
    cid, reply = _chat_and_persist(user_text, conv_id_in)
    return {"ok": True, "reply": reply, "conversation_id": cid}


# Speak endpoint: chat and stream TTS audio of reply
@app.post("/api/speak")
async def api_speak(payload: dict = Body(...), authorization: Optional[str] = Header(None)):
    """
    One-shot: user text -> assistant reply (persisted) -> MP3 audio stream of reply.
    Sets X-Conversation-Id and X-Reply-Text headers for convenience.
    """
    _require_api_key(authorization)
    if not OPENAI_API_KEY:
        raise HTTPException(500, "OPENAI_API_KEY missing")

    user_text = (payload.get("text") or "").strip()
    conv_id_in = payload.get("conversation_id")
    voice = (payload.get("voice") or "alloy").strip() or "alloy"

    cid, reply = _chat_and_persist(user_text, conv_id_in)

    try:
        tts_resp = _oai.audio.speech.create(
            model="gpt-4o-mini-tts",
            voice=voice,
            input=reply,
        )
        audio_bytes = tts_resp.read()
        buf = BytesIO(audio_bytes)
        headers = {
            "X-Conversation-Id": str(cid),
            # Base64-encode short replies for debug/clients; truncate if very long
            "X-Reply-Text-B64": base64.b64encode(reply.encode("utf-8")).decode("ascii")[:4096],
        }
        return StreamingResponse(buf, media_type="audio/mpeg", headers=headers)
    except Exception as e:
        logging.exception("TTS(speak) failed")
        raise HTTPException(500, f"TTS error: {e}")


# ------------------- Conversation API -------------------
# ------------------- Daily Summary Endpoint -------------------
@app.get("/api/dailySummary")
def api_daily_summary(limit_messages: int = 40, limit_reflections: int = 20, authorization: Optional[str] = Header(None)):
    _require_api_key(authorization)
    if not OPENAI_API_KEY:
        raise HTTPException(500, "OPENAI_API_KEY missing")

    # gather recent messages
    _ensure_db()
    con = sqlite3.connect(str(_db_path()))
    try:
        con.row_factory = sqlite3.Row
        msgs = con.execute(
            "SELECT role, text, created_at FROM messages ORDER BY id DESC LIMIT ?",
            (limit_messages,)
        ).fetchall()
        refls = con.execute(
            "SELECT text, created_at FROM reflections ORDER BY id DESC LIMIT ?",
            (limit_reflections,)
        ).fetchall()
    finally:
        con.close()

    context = {
        "messages": [dict(r) for r in msgs][::-1],
        "reflections": [dict(r) for r in refls][::-1],
    }

    try:
        prompt = (
            "You are S.A.R.A. Summarize the past day for Saif. "
            "Sections: 1) Mood & Energy, 2) Key Themes, 3) Decisions Made, "
            "4) Actionable Next 3 Steps (bullet points, concrete). Be concise."
        )
        resp = _oai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps(context)},
            ],
            temperature=0.3,
        )
        summary = (resp.choices[0].message.content or "").strip()
    except Exception as e:
        logging.exception("dailySummary failed")
        raise HTTPException(500, f"Summary error: {e}")

    return {"ok": True, "summary": summary}


# ------------------- Finance API (v0.1) -------------------

@app.post("/api/finance/log")
def api_finance_log(payload: dict = Body(...), authorization: Optional[str] = Header(None)):
    """Log an expense in natural language.

    Payload example:
      { "text": "record $3 espresso at Mike's", "emotion": "tired" }
    """
    _require_api_key(authorization)
    _ensure_db()

    text = (payload.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "text required")

    emotion = (payload.get("emotion") or "").strip() or None

    try:
        parsed = _parse_transaction_input(text)
    except Exception as e:
        logging.exception("finance parse error")
        raise HTTPException(500, f"parse error: {e}")

    # extract model-parsed components
    try:
        amount = float(parsed.get("amount", 0.0))
    except Exception:
        amount = 0.0

    # Guard: don't insert junk $0 transactions
    if amount <= 0:
        raise HTTPException(
            400,
            "I couldn't see a positive amount in that. "
            "Try something like: 'record $7 latte at Blue Bottle'.",
        )

    amount_cents = int(round(amount * 100))
    currency = str(parsed.get("currency", "USD")).upper()
    merchant = (parsed.get("merchant") or "").strip() or None
    category = (parsed.get("category") or "other").strip() or "other"
    notes = (parsed.get("notes") or "").strip() or None

    # FORCE current timestamp instead of model date
    created_at_iso = datetime.now(timezone.utc).isoformat()

    con = sqlite3.connect(str(_db_path()))
    try:
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        cur.execute(
            """
            INSERT INTO transactions (
                user_id, created_at, amount_cents, currency,
                merchant, raw_input, category, emotion, notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "default",
                created_at_iso,
                amount_cents,
                currency,
                merchant,
                text,
                category,
                emotion,
                notes,
            ),
        )
        con.commit()
        tx_id = cur.lastrowid
        row = con.execute("SELECT * FROM transactions WHERE id = ?", (tx_id,)).fetchone()
    finally:
        con.close()

    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "amount": row["amount_cents"] / 100.0,
        "currency": row["currency"],
        "merchant": row["merchant"],
        "category": row["category"],
        "raw_input": row["raw_input"],
        "emotion": row["emotion"],
        "notes": row["notes"],
    }
@app.get("/api/finance/month")
def api_finance_month(
    year: Optional[int] = None,
    month: Optional[int] = None,
    authorization: Optional[str] = Header(None),
):
    """List all transactions for a given month (default: current month)."""
    _require_api_key(authorization)
    _ensure_db()

    now = datetime.now()
    if year is None:
        year = now.year
    if month is None:
        month = now.month

    start = datetime(year, month, 1)
    # first day of next month
    if month == 12:
        next_start = datetime(year + 1, 1, 1)
    else:
        next_start = datetime(year, month + 1, 1)

    con = sqlite3.connect(str(_db_path()))
    try:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            """
            SELECT * FROM transactions
            WHERE user_id = ?
              AND created_at >= ?
              AND created_at < ?
            ORDER BY created_at ASC
            """,
            ("default", start.isoformat(), next_start.isoformat()),
        ).fetchall()
    finally:
        con.close()

    items: list[dict] = []
    for row in rows:
        items.append(
            {
                "id": row["id"],
                "created_at": row["created_at"],
                "amount": row["amount_cents"] / 100.0,
                "currency": row["currency"],
                "merchant": row["merchant"],
                "category": row["category"],
                "raw_input": row["raw_input"],
                "emotion": row["emotion"],
                "notes": row["notes"],
            }
        )
    return {
        "ok": True,
        "year": year,
        "month": month,
        "items": items,
    }


@app.get("/api/finance/summary/monthly")
def api_finance_summary_monthly(
    year: Optional[int] = None,
    month: Optional[int] = None,
    authorization: Optional[str] = Header(None),
):
    """Monthly finance summary with simple budget comparison."""
    _require_api_key(authorization)
    _ensure_db()

    now = datetime.now()
    if year is None:
        year = now.year
    if month is None:
        month = now.month

    start = datetime(year, month, 1)
    if month == 12:
        next_start = datetime(year + 1, 1, 1)
    else:
        next_start = datetime(year, month + 1, 1)

    # "to_date" = last moment of the month
    to_date = next_start - timedelta(microseconds=1)

    con = sqlite3.connect(str(_db_path()))
    try:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            """
            SELECT * FROM transactions
            WHERE user_id = ?
              AND created_at >= ?
              AND created_at < ?
            """,
            ("default", start.isoformat(), next_start.isoformat()),
        ).fetchall()
    finally:
        con.close()

    if not rows:
        return {
            "period": "month",
            "year": year,
            "month": month,
            "from_date": start.isoformat(),
            "to_date": to_date.isoformat(),
            "total_spent": 0.0,
            "currency": "USD",
            "by_category": {},
            "budget_by_category": MONTHLY_BUDGET_BY_CATEGORY,
            "diff_by_category": {},
            "budget_total": sum(MONTHLY_BUDGET_BY_CATEGORY.values()),
            "over_under_total": sum(MONTHLY_BUDGET_BY_CATEGORY.values()),
            "transaction_count": 0,
            "insight": "No spending logged for this month yet.",
        }

    total_cents = 0
    currency = rows[0]["currency"]
    by_category_cents: dict[str, int] = {}

    for row in rows:
        c = int(row["amount_cents"])
        total_cents += c
        cat = (row["category"] or "other").lower()
        by_category_cents[cat] = by_category_cents.get(cat, 0) + c

    total = total_cents / 100.0
    by_category = {k: v / 100.0 for k, v in by_category_cents.items()}

    # Budget comparison
    budget_total = sum(MONTHLY_BUDGET_BY_CATEGORY.values())
    diff_by_category: dict[str, float] = {}
    for cat, budget in MONTHLY_BUDGET_BY_CATEGORY.items():
        actual = by_category.get(cat, 0.0)
        diff_by_category[cat] = round(budget - actual, 2)

    over_under_total = round(budget_total - total, 2)

    if over_under_total < 0:
        insight = (
            f"You're ${abs(over_under_total):.2f} over your configured monthly budget "
            f"(${budget_total:.2f}). Worth looking at subs, food, and 'other'."
        )
    else:
        insight = (
            f"You're ${over_under_total:.2f} under your configured monthly budget "
            f"(${budget_total:.2f}). Nice. See if any cuts feel easy without feeling restrictive."
        )

    return {
        "period": "month",
        "year": year,
        "month": month,
        "from_date": start.isoformat(),
        "to_date": to_date.isoformat(),
        "total_spent": total,
        "currency": currency,
        "by_category": by_category,
        "budget_by_category": MONTHLY_BUDGET_BY_CATEGORY,
        "diff_by_category": diff_by_category,
        "budget_total": budget_total,
        "over_under_total": over_under_total,
        "transaction_count": len(rows),
        "insight": insight,
    }


@app.delete("/api/finance/{transaction_id}")
def api_finance_delete(transaction_id: int, authorization: Optional[str] = Header(None)):
    """Delete a single transaction by id."""
    _require_api_key(authorization)
    _ensure_db()

    con = sqlite3.connect(str(_db_path()))
    try:
        cur = con.cursor()
        cur.execute(
            "DELETE FROM transactions WHERE id = ? AND user_id = ?",
            (transaction_id, "default"),
        )
        con.commit()
        deleted = cur.rowcount
    finally:
        con.close()

    if not deleted:
        raise HTTPException(404, f"Transaction {transaction_id} not found.")

    return {"ok": True, "deleted_id": transaction_id}


@app.get("/api/finance/today")
def api_finance_today(authorization: Optional[str] = Header(None)):
    """List today's transactions for the default user."""
    _require_api_key(authorization)
    _ensure_db()

    today = datetime.now().date()
    start = datetime.combine(today, datetime.min.time()).isoformat()
    end = datetime.combine(today, datetime.max.time()).isoformat()

    con = sqlite3.connect(str(_db_path()))
    try:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            """
            SELECT * FROM transactions
            WHERE user_id = ? AND created_at BETWEEN ? AND ?
            ORDER BY created_at DESC
            """,
            ("default", start, end),
        ).fetchall()
    finally:
        con.close()

    items = []
    for row in rows:
        items.append(
            {
                "id": row["id"],
                "created_at": row["created_at"],
                "amount": row["amount_cents"] / 100.0,
                "currency": row["currency"],
                "merchant": row["merchant"],
                "category": row["category"],
                "raw_input": row["raw_input"],
                "emotion": row["emotion"],
                "notes": row["notes"],
            }
        )
    return items


@app.get("/api/finance/summary/daily")
def api_finance_summary_daily(authorization: Optional[str] = Header(None)):
    """Daily finance summary: total, by category, coffee, and a small insight."""
    _require_api_key(authorization)
    _ensure_db()

    today = datetime.now().date()
    start = datetime.combine(today, datetime.min.time())
    end = datetime.combine(today, datetime.max.time())

    con = sqlite3.connect(str(_db_path()))
    try:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            """
            SELECT * FROM transactions
            WHERE user_id = ? AND created_at BETWEEN ? AND ?
            """,
            ("default", start.isoformat(), end.isoformat()),
        ).fetchall()
    finally:
        con.close()

    if not rows:
        return {
            "period": "today",
            "from_date": start.isoformat(),
            "to_date": end.isoformat(),
            "total_spent": 0.0,
            "currency": "USD",
            "by_category": {},
            "coffee_spent": 0.0,
            "transaction_count": 0,
            "insight": "No spending logged yet. Try adding one thing, even if it's tiny.",
        }

    total_cents = 0
    currency = rows[0]["currency"]
    by_category: dict[str, int] = {}
    coffee_cents = 0

    for row in rows:
        c = int(row["amount_cents"])
        total_cents += c
        cat = (row["category"] or "other").lower()
        by_category[cat] = by_category.get(cat, 0) + c
        if cat == "coffee":
            coffee_cents += c

    total = total_cents / 100.0
    coffee = coffee_cents / 100.0
    cats_float = {k: v / 100.0 for k, v in by_category.items()}

    if coffee > 0 and total > 0:
        ratio = coffee / total
        if ratio > 0.3:
            insight = f"Coffee is {ratio:.0%} of your spending today. Are these rushed days or intentional rituals?"
        else:
            insight = "Your coffee spending is present but not dominating today."
    else:
        insight = "No coffee logged today. How are your energy rituals showing up instead?"

    return {
        "period": "today",
        "from_date": start.isoformat(),
        "to_date": end.isoformat(),
        "total_spent": total,
        "currency": currency,
        "by_category": cats_float,
        "coffee_spent": coffee,
        "transaction_count": len(rows),
        "insight": insight,
    }

@app.get("/api/conversations")
def api_list_conversations(limit: int = 20, authorization: Optional[str] = Header(None)):
    """
    List recent conversations (most recently updated first).
    Returns id, title, created_at, updated_at, and last_text.
    """
    _require_api_key(authorization)
    try:
        _ensure_db()
        return {"ok": True, "items": _list_conversations(limit=limit)}
    except Exception as e:
        logging.exception("list conversations failed")
        raise HTTPException(500, f"error: {e}")

@app.get("/api/conversations/{conversation_id}")
def api_get_conversation(conversation_id: int, limit: int = 200, authorization: Optional[str] = Header(None)):
    _require_api_key(authorization)
    try:
        _ensure_db()
        return {"ok": True, "items": _list_messages(conversation_id, limit=limit)}
    except Exception as e:
        logging.exception("get conversation failed")
        raise HTTPException(500, f"error: {e}")


# --- Conversation management endpoints ---

@app.post("/api/conversations")
def api_new_conversation(payload: dict = Body({}), authorization: Optional[str] = Header(None)):
    _require_api_key(authorization)
    _ensure_db()
    title = (payload.get("title") or "Untitled").strip()
    cid = _create_conversation(title=title[:120])
    return {"ok": True, "id": cid, "title": title}


@app.patch("/api/conversations/{conversation_id}")
def api_rename_conversation(conversation_id: int, payload: dict = Body(...), authorization: Optional[str] = Header(None)):
    _require_api_key(authorization)
    new_title = (payload.get("title") or "Untitled").strip()
    if not new_title:
        raise HTTPException(400, "title required")
    _ensure_db()
    _rename_conversation(conversation_id, new_title)
    return {"ok": True, "id": conversation_id, "title": new_title}


@app.delete("/api/conversations/{conversation_id}")
def api_delete_conversation(conversation_id: int, authorization: Optional[str] = Header(None)):
    _require_api_key(authorization)
    _ensure_db()
    _delete_conversation(conversation_id)
    return {"ok": True, "id": conversation_id}


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

# ------------------- Admin (backups) -------------------

# --- Self-test endpoint ---
@app.post("/api/selftest")
def api_selftest(authorization: Optional[str] = Header(None)):
    """
    Runs a minimal create → chat → list → rename → delete cycle
    without leaving test artifacts. Returns a concise report.
    """
    _require_api_key(authorization)
    _ensure_db()
    report: dict[str, list[str] | str | bool] = {"steps": []}

    try:
        # create
        cid = _create_conversation("Self-test")
        report["steps"].append(f"create:{cid}")  # type: ignore[arg-type]

        # user turn + assistant turn (persisted)
        _, reply = _chat_and_persist("Quick self-check: say 'pong' once.", cid)
        report["steps"].append(f"chat:{'ok' if reply else 'no-reply'}")  # type: ignore[arg-type]

        # list
        convs = _list_conversations(limit=5)
        report["steps"].append(f"list:{len(convs)}")  # type: ignore[arg-type]

        # rename
        _rename_conversation(cid, "Self-test (renamed)")
        report["steps"].append("rename:ok")  # type: ignore[arg-type]

        # delete
        _delete_conversation(cid)
        report["steps"].append("delete:ok")  # type: ignore[arg-type]

        return {"ok": True, "report": report}
    except Exception as e:
        logging.exception("selftest failed")
        report["error"] = str(e)
        return {"ok": False, "report": report}
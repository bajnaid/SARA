# server/services/context_builder.py

from datetime import datetime, timedelta
import sqlite3
import os

DB_PATH = os.getenv("SARA_DB", "sara.db")

def get_connection():
    return sqlite3.connect(DB_PATH)

def get_recent_reflections(user_id: int, limit: int = 5):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT created_at, text
        FROM reflections
        WHERE user_id = ?
        ORDER BY datetime(created_at) DESC
        LIMIT ?
    """, (user_id, limit))
    rows = cur.fetchall()
    conn.close()
    return rows

def get_recent_finance_logs(user_id: int, days: int = 3, limit: int = 10):
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT created_at, amount, category, raw_input
        FROM finance_logs
        WHERE user_id = ?
          AND datetime(created_at) >= datetime(?)
        ORDER BY datetime(created_at) DESC
        LIMIT ?
    """, (user_id, cutoff, limit))
    rows = cur.fetchall()
    conn.close()
    return rows

def build_user_context(user_id: int):
    reflections = get_recent_reflections(user_id, limit=5)
    finance = get_recent_finance_logs(user_id, days=3, limit=10)

    parts = []

    if reflections:
        parts.append("Recent reflections:")
        for r in reflections:
            parts.append(f"- {r[0]}: {r[1]}")

    if finance:
        parts.append("\nRecent financial activity:")
        for f in finance:
            parts.append(f"- {f[0]} · ${f[1]} · {f[2]} · {f[3]}")

    return "\n".join(parts) if parts else None
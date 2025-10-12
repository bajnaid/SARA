# server/services/cards.py

from __future__ import annotations

from datetime import datetime
import logging

# --- Imports from our services ---
from .calendar_google import get_today_events
from .ai_coach import plan_today_summary, nightly_review_summary

# Plaid may not be configured yet; import lazily/fail-safe.
try:
    from .finance_plaid import get_money_snapshot
    HAS_PLAID = True
except Exception:
    HAS_PLAID = False


# -------------------------------
# Card builders (with fallbacks)
# -------------------------------

async def build_plan_card() -> dict:
    """
    Build 'Plan Today' using REAL Google Calendar events.
    Falls back to a friendly stub if Google auth isn't completed yet.
    """
    try:
        events = await get_today_events()  # [{title, start_iso, end_iso}, ...]
        body = await plan_today_summary(events)  # ai_coach formats the summary
    except Exception as e:
        logging.exception("Plan card failed; falling back to stub: %s", e)
        body = "Today: review your schedule and pick 3 MITs. Start with the hardest task."

    return {
        "type": "plan",
        "title": "Plan Today",
        "body": body,
        "cta": "Start Focus",
    }


async def build_money_card() -> dict:
    """
    Build 'Money Snapshot'. If Plaid isn't wired yet, show a placeholder.
    """
    if not HAS_PLAID:
        body = "Connect accounts to see spend today, budget left, and week total."
        return {"type": "money", "title": "Money Snapshot", "body": body, "cta": "Connect Budget"}

    try:
        money = await get_money_snapshot()
        body = (
            f"Spend today: ${money['spend_today']:.2f} | "
            f"Left today: ${money['budget_left_today']:.2f} | "
            f"Week: ${money['spend_week']:.2f}"
        )
        cta = "Open Budget"
    except Exception as e:
        logging.exception("Money card failed; showing placeholder: %s", e)
        body = "Budget data unavailable right now."
        cta = "Retry"
    return {"type": "money", "title": "Money Snapshot", "body": body, "cta": cta}


async def build_reflect_card() -> dict:
    body = "60-sec check-in: What went well? What blocked you? What will you try tomorrow?"
    return {"type": "reflect", "title": "Nightly Review", "body": body, "cta": "Log Reflection"}


# -------------------------------
# Public API used by /api/currentCard
# -------------------------------

async def compose_current_card() -> dict:
    """
    Route selector: morning = Plan, midday = Money, evening = Reflect.
    """
    hour = datetime.now().hour

    if hour <= 12:
        return await build_plan_card()
    elif hour < 20:
        return await build_money_card()
    else:
        return await build_reflect_card()


# -------------------------------
# Reflection writer
# -------------------------------

async def save_reflection(text: str) -> dict:
    """
    Summarize a reflection and return it.
    Replace the spend snapshot string with a real value once Money is wired.
    """
    try:
        spend_snapshot = "Week-to-date: $96.30"  # TODO: replace with real number from Plaid
        summary = await nightly_review_summary(text, spend_snapshot)
        return {"ok": True, "summary": summary}
    except Exception as e:
        logging.exception("Failed to save reflection: %s", e)
        return {"ok": False, "summary": "Couldn't summarize right now. Try again in a moment."}
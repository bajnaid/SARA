from services import compose_current_card, save_reflection
from services.ai_coach import svc_list_reflections, svc_export_reflections
import logging

async def current_card():
    try:
        # compose_current_card may be sync; avoid awaiting a non-coroutine
        import asyncio
        result = compose_current_card()
        if asyncio.iscoroutine(result):
            result = await result
        return result
    except Exception as e:
        logging.exception("currentCard failed")
        # Safe, minimal fallback card so the HUD never 500s
        return {
            "type": "plan",
            "title": "Plan Today",
            "body": f"(fallback) Unable to compute current card: {type(e).__name__}",
            "cta": "Start Focus",
        }

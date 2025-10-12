import os

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

async def plan_today_summary(events: list) -> str:
    if not events:
        return "No fixed events. 3 MITs: Sketch variant, Update deck, 45m study."
    first = events[0]["title"]
    return f"Today: {len(events)} events. Start with '{first}'. 3 MITs: deck draft, 45m CAD, 3 investor outreaches."

async def nightly_review_summary(reflection: str, spend_snapshot: str) -> str:
    return f"Reflection saved. Money: {spend_snapshot}. Tomorrow: keep MITs <= 3; start with the hardest task."

# server/services/sara_chat.py

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional

from openai import OpenAI

from .prompts import SYSTEM_PROMPT_SAIF, SYSTEM_PROMPT_PUBLIC
from .context_builder import build_user_context
from server.auth import get_current_user, User  # adjust import if your auth is different

router = APIRouter()
client = OpenAI()

class ChatRequest(BaseModel):
    message: str
    mode: Optional[str] = None  # "saif" or "public" (optional override)

@router.post("/chat/stream")
async def chat_stream(payload: ChatRequest, user: User = Depends(get_current_user)):
    # Persona selection
    persona = (payload.mode or "public").lower()
    if user.email.endswith("@bajnaid.nyc") or persona == "saif":
        system_prompt = SYSTEM_PROMPT_SAIF
    else:
        system_prompt = SYSTEM_PROMPT_PUBLIC

    # Context from DB (reflections + finances for this user)
    context_text = build_user_context(user_id=user.id)

    messages = [{"role": "system", "content": system_prompt}]
    if context_text:
        messages.append({"role": "system", "content": f"Here is this user's recent context:\n{context_text}"})
    messages.append({"role": "user", "content": payload.message})

    def generate():
        stream = client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=messages,
            stream=True,
        )
        for chunk in stream:
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta

    return StreamingResponse(generate(), media_type="text/plain")
import os
from fastapi import FastAPI, Body
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from services.cards import compose_current_card, save_reflection

load_dotenv()
app = FastAPI(title="SARA MVP API", version="0.1.0")


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)


@app.get("/", tags=["meta"], include_in_schema=False)
def root():
    return {
        "status": "ok",
        "service": "sara-mvp-api",
        "version": "0.1.0",
        "docs": "/docs",
        "health": "/api/health",
    }

@app.get("/api/health")
async def health():
    return {"ok": True}

@app.get("/api/currentCard")
async def current_card():
    return await compose_current_card()

@app.post("/api/reflect")
async def reflect(payload: dict = Body(...)):
    text = (payload.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "No reflection text."}
    return await save_reflection(text)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.getenv("HOST","0.0.0.0"), port=int(os.getenv("PORT","8000")))

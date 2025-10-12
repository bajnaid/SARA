S.A.R.A. MVP Starter — Plan · Spend · Reflect

Quick start (macOS, zsh):
1) Terminal in sara-mvp/server:
   python -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   cp .env.example .env
   python app.py

2) Test the API:
   http://localhost:8000/api/currentCard
   http://localhost:8000/docs

3) Open the Web HUD:
   Open sara-mvp/web-hud/index.html in a browser.
   If your API runs on another device, use:
   index.html?api=http://YOUR-LAN-IP:8000

Next:
- Replace stubs in services/ with Google Calendar + Plaid.
- Plug your GPT prompts into services/ai_coach.py.

from datetime import datetime, timedelta, timezone
import os
from datetime import datetime, timedelta, timezone

GOOGLE_READY = True
try:
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
except Exception:
    GOOGLE_READY = False

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]

# Use environment variables so it works locally and on Render
CRED_FILE = os.getenv("GOOGLE_CREDENTIALS_PATH", "server/credentials.json")
TOKEN_FILE = os.getenv("GOOGLE_TOKEN_PATH", "/tmp/token.json")

def _get_google_creds():
    if not GOOGLE_READY:
        print("⚠️ Google SDK not available, skipping Calendar integration.")
        return None

    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CRED_FILE):
                print("⚠️ No Google credentials found, skipping Calendar integration.")
                return None
            flow = InstalledAppFlow.from_client_secrets_file(CRED_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return creds

async def get_today_events() -> list:
    """Return today's events as [{title, start_iso, end_iso}]"""
    if not GOOGLE_READY:
        return []
    creds = _get_google_creds()
    if creds is None:
        return []

    service = build("calendar", "v3", credentials=creds)

    now = datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)

    res = service.events().list(
        calendarId="primary",
        timeMin=start.isoformat(),
        timeMax=end.isoformat(),
        singleEvents=True,
        orderBy="startTime",
    ).execute()

    events = []
    for item in res.get("items", []):
        title = item.get("summary", "Untitled")
        start_iso = item["start"].get("dateTime") or (item["start"]["date"] + "T00:00:00Z")
        end_iso = item["end"].get("dateTime") or (item["end"]["date"] + "T23:59:59Z")
        events.append({"title": title, "start_iso": start_iso, "end_iso": end_iso})

    return events
from datetime import datetime, timedelta, timezone
import pathlib
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
BASE_DIR = pathlib.Path(__file__).resolve().parents[1]  # .../server
CRED_FILE = BASE_DIR / "credentials.json"
TOKEN_FILE = BASE_DIR / "token.json"

def _get_google_creds():
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(CRED_FILE), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())
    return creds

async def get_today_events() -> list:
    """Return today's events as [{title, start_iso, end_iso}]"""
    creds = _get_google_creds()
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
        end_iso   = item["end"].get("dateTime")   or (item["end"]["date"] + "T23:59:59Z")
        events.append({"title": title, "start_iso": start_iso, "end_iso": end_iso})
    return events
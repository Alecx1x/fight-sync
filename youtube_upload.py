"""One-click upload of a finished render to the user's own YouTube channel.

Uses the YouTube Data API v3. Because the OAuth consent must happen in a real
browser (and would block the web server), the *interactive* part is a one-time
terminal step — run `youtube_auth.py` once — which saves a refreshable token.
After that this module uploads using the saved token, no prompts.

Setup (one time, done by the user — I can't create their Google project):
  1. console.cloud.google.com -> new project -> enable "YouTube Data API v3".
  2. Create an OAuth client ID of type "Desktop app"; download the JSON and
     save it here as `client_secret.json`.
  3. Run:  .venv\Scripts\python youtube_auth.py   (approves access once).
Heads-up: until a Google API project passes verification, uploads may be forced
to *private* regardless of the privacy you pick — that's a YouTube policy, not us.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).parent
CLIENT_SECRET = ROOT / "client_secret.json"
TOKEN = ROOT / "yt_token.json"
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
CATEGORY_GAMING = "20"


def state() -> dict:
    """Whether upload is set up — drives the UI (configured? authorized?)."""
    return {
        "configured": CLIENT_SECRET.exists(),
        "authorized": TOKEN.exists(),
    }


def _load_credentials():
    """Return valid Credentials (refreshing if needed) or None."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    if not TOKEN.exists():
        return None
    creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN.write_text(creds.to_json(), encoding="utf-8")
    return creds if creds and creds.valid else None


def authorize_interactive() -> None:
    """Run the one-time consent flow in a browser and save the token.
    Called by youtube_auth.py (terminal), NOT by the web server."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not CLIENT_SECRET.exists():
        raise RuntimeError(
            f"Missing {CLIENT_SECRET.name}. Create an OAuth 'Desktop app' client "
            "in Google Cloud (with YouTube Data API v3 enabled) and save its JSON "
            f"here as {CLIENT_SECRET.name}.")
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent",
                                  authorization_prompt_message="")
    TOKEN.write_text(creds.to_json(), encoding="utf-8")


def upload(video_path: str, title: str, description: str, tags: list,
           privacy: str = "unlisted",
           progress: Optional[Callable[[int, str], None]] = None) -> dict:
    """Upload the video; returns {video_id, url}. Raises with a clear message
    if not set up."""
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    def prog(p, m):
        if progress:
            progress(p, m)

    st = state()
    if not st["configured"]:
        raise RuntimeError(
            "YouTube upload isn't set up yet. Add client_secret.json (see the "
            "setup steps), then run youtube_auth.py once.")
    creds = _load_credentials()
    if not creds:
        raise RuntimeError(
            "Not authorized yet — run  .venv\\Scripts\\python youtube_auth.py  "
            "once to approve access, then try again.")

    privacy = privacy if privacy in ("public", "unlisted", "private") else "unlisted"
    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
    body = {
        "snippet": {
            "title": (title or "Thrill of the Fight 2")[:100],
            "description": description or "",
            "tags": (tags or [])[:30],
            "categoryId": CATEGORY_GAMING,
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(video_path, chunksize=8 * 1024 * 1024,
                            resumable=True, mimetype="video/mp4")
    request = youtube.videos().insert(part="snippet,status", body=body,
                                      media_body=media)
    prog(5, "Starting upload to YouTube…")
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            pct = 5 + int(status.progress() * 90)
            prog(pct, f"Uploading… {int(status.progress() * 100)}%")
    vid = response["id"]
    prog(100, "Uploaded.")
    return {"video_id": vid, "url": f"https://youtu.be/{vid}", "privacy": privacy}

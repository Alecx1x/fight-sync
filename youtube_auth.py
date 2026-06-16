"""One-time YouTube authorization for FightSync.

Run this ONCE in the terminal:

    .venv\\Scripts\\python youtube_auth.py

It opens a browser, you approve access to your channel, and a refreshable token
is saved to yt_token.json. After that, the "Upload to YouTube" button in the app
works with no further prompts.

Prerequisite: client_secret.json must be present (an OAuth "Desktop app" client
from a Google Cloud project with YouTube Data API v3 enabled).
"""
from youtube_upload import authorize_interactive, CLIENT_SECRET, TOKEN


def main():
    if not CLIENT_SECRET.exists():
        print(f"\n[X] Missing {CLIENT_SECRET.name}")
        print("    1. console.cloud.google.com -> new project")
        print("    2. Enable 'YouTube Data API v3'")
        print("    3. Create an OAuth client ID, type 'Desktop app'")
        print(f"    4. Download the JSON and save it here as {CLIENT_SECRET.name}")
        print("    Then run this script again.\n")
        return
    print("\nOpening your browser to approve YouTube access...")
    authorize_interactive()
    print(f"\n[OK] Authorized. Token saved to {TOKEN.name}.")
    print("     You can now use the Upload to YouTube button in FightSync.\n")


if __name__ == "__main__":
    main()

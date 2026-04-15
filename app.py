import os
import threading

from dotenv import load_dotenv

load_dotenv()

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from features.study import register_study_handlers
from features.gmail import register_gmail_handlers
from features.assassins import register_assassins_handlers
from features.spottings import register_spottings_handlers
from features.matchy import register_matchy_handlers

app = App(token=os.environ.get("SLACK_BOT_TOKEN"))
register_study_handlers(app)
register_gmail_handlers(app)
register_assassins_handlers(app)
register_spottings_handlers(app)
register_matchy_handlers(app)


def _start_oauth_server():
    """Run HTTP server for Gmail OAuth callback (e.g. /gmail/oauth on Railway)."""
    from oauth_server import run_oauth_server
    run_oauth_server()

if __name__ == "__main__":
    # Start OAuth HTTP server in background when Gmail callback URL is configured (e.g. Railway)
    from features.gmail import _gmail_oauth_callback_base
    base = _gmail_oauth_callback_base()
    if base:
        t = threading.Thread(target=_start_oauth_server, daemon=True)
        t.start()
        print(f"[Gmail] OAuth: visit {base}/gmail/oauth to authorize (if token missing)")
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()

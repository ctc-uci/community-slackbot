import os
import threading

from dotenv import load_dotenv

load_dotenv()

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from features.study import register_study_handlers
from features.gmail import register_gmail_handlers
from features.assassins import register_assassins_handlers
from features.ridesheet import register_ridesheet_handlers
from features.spottings import register_spottings_handlers
from features.matchy import register_matchy_handlers
from features.events import register_events_handlers

app = App(token=os.environ.get("SLACK_BOT_TOKEN"))
register_study_handlers(app)
register_gmail_handlers(app)
register_assassins_handlers(app)
register_ridesheet_handlers(app)
register_spottings_handlers(app)
register_matchy_handlers(app)
register_events_handlers(app)


def _start_web_server():
    """Run Flask server for the ridesheet website and Gmail OAuth callback."""
    from oauth_server import run_web_server
    run_web_server()

if __name__ == "__main__":
    threading.Thread(target=_start_web_server, daemon=True).start()

    from features.gmail import _gmail_oauth_callback_base
    base = _gmail_oauth_callback_base()
    if base:
        print(f"[Gmail] OAuth: visit {base}/gmail/oauth to authorize (if token missing)")

    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()

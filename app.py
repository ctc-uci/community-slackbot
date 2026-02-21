import os

from dotenv import load_dotenv

load_dotenv()

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from features.study import register_study_handlers
from features.spottings import register_spottings_handlers

app = App(token=os.environ.get("SLACK_BOT_TOKEN"))
register_study_handlers(app)
register_spottings_handlers(app)

if __name__ == "__main__":
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()

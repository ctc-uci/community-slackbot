"""
Flask web server running alongside the Slack bot.

Routes are split by feature:
  web/gmail.py      — Gmail OAuth
  web/ridesheet.py  — Ridesheet SPA + API + refresh webhook
"""
import os

from flask import Flask

from web.gmail import bp as gmail_bp
from web.ridesheet import bp as ridesheet_bp


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-key")
    app.register_blueprint(gmail_bp)
    app.register_blueprint(ridesheet_bp)
    return app


flask_app = create_app()


def run_web_server(port: int | None = None) -> None:
    port = port or int(os.environ.get("PORT", "3000"))
    print(f"[Web] Flask server listening on port {port}")
    flask_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

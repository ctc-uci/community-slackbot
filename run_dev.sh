#!/usr/bin/env bash
# Run the Slack bot with auto-reload on .py file changes.
# Requires: pip install -r requirements.txt (includes watchdog)
cd "$(dirname "$0")"
.venv/bin/watchmedo auto-restart --directory . --patterns "*.py" --recursive -- .venv/bin/python3 app.py

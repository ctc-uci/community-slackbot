"""
Backward-compatible entry point for the Flask web server.

Implementation lives in the ``web`` package; import from there for new code.
"""
from web import flask_app, run_web_server

__all__ = ["flask_app", "run_web_server"]

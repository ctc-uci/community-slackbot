"""
Firebase Admin SDK client. Credentials from file or .env one-liner.
- FIREBASE_CREDENTIALS_JSON: JSON string (one line) → use this, no file needed.
- FIREBASE_CREDENTIALS_PATH: path to JSON file (default: firebase-credentials.json).
"""
import json
import os
from pathlib import Path

import firebase_admin
from firebase_admin import credentials

_app = None


def get_firebase_app():
    """Initialize and return the Firebase app. Safe to call multiple times."""
    global _app
    if _app is not None:
        return _app
    raw = os.environ.get("FIREBASE_CREDENTIALS_JSON", "").strip()
    if raw:
        data = json.loads(raw)
        if isinstance(data.get("private_key"), str) and "\\n" in data["private_key"]:
            data["private_key"] = data["private_key"].replace("\\n", "\n")
        cred = credentials.Certificate(data)
    else:
        cred_path = os.environ.get(
            "FIREBASE_CREDENTIALS_PATH",
            str(Path(__file__).resolve().parent / "firebase-credentials.json"),
        )
        if not Path(cred_path).exists():
            raise FileNotFoundError(
                f"Firebase credentials not found at {cred_path}. "
                "Set FIREBASE_CREDENTIALS_JSON (JSON one-liner) or FIREBASE_CREDENTIALS_PATH."
            )
        cred = credentials.Certificate(cred_path)
    _app = firebase_admin.initialize_app(cred)
    return _app

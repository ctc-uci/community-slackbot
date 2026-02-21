"""
Firebase Admin SDK client. Initializes from credentials file.
Set FIREBASE_CREDENTIALS_PATH in .env to override (default: firebase-credentials.json).
"""
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
    cred_path = os.environ.get(
        "FIREBASE_CREDENTIALS_PATH",
        str(Path(__file__).parent / "firebase-credentials.json"),
    )
    if not Path(cred_path).exists():
        raise FileNotFoundError(
            f"Firebase credentials not found at {cred_path}. "
            "Set FIREBASE_CREDENTIALS_PATH or add firebase-credentials.json."
        )
    cred = credentials.Certificate(cred_path)
    _app = firebase_admin.initialize_app(cred)
    return _app

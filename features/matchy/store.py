"""Firestore persistence for Matchy roster and match history."""
from datetime import datetime, timezone

from firebase_admin import firestore

from firebase_client import get_firebase_app

ROSTER_COLLECTION = "matchyData"
ROSTER_DOC_ID = "members"


def _db():
    get_firebase_app()
    return firestore.client()


def _empty_roster() -> dict:
    return {
        "members": [],
        "previousMatches": {},
        "nextMatchOverrides": [],
        "skipNextMatchy": False,
        "matchyPaused": False,
    }


def _parse_overrides(raw) -> list[list[str]]:
    if not isinstance(raw, list):
        return []
    groups = []
    for item in raw:
        if isinstance(item, dict):
            members = [m for m in (item.get("members") or []) if m]
        elif isinstance(item, list):
            members = [m for m in item if m]
        else:
            continue
        if len(members) >= 2:
            groups.append(members)
    return groups


def load_roster() -> dict:
    doc = _db().collection(ROSTER_COLLECTION).document(ROSTER_DOC_ID).get()
    if doc.exists:
        data = doc.to_dict() or {}
        return {
            "members": data.get("members") or [],
            "previousMatches": data.get("previousMatches") or {},
            "nextMatchOverrides": _parse_overrides(data.get("nextMatchOverrides")),
            "skipNextMatchy": bool(data.get("skipNextMatchy")),
            "matchyPaused": bool(data.get("matchyPaused")),
        }

    empty = _empty_roster()
    try:
        _db().collection(ROSTER_COLLECTION).document(ROSTER_DOC_ID).set({
            **empty,
            "lastUpdated": datetime.now(timezone.utc).isoformat(),
        })
    except Exception:
        pass
    return empty


def save_roster(data: dict) -> None:
    overrides = []
    for group in data.get("nextMatchOverrides") or []:
        members = [m for m in group if m] if isinstance(group, list) else []
        if len(members) >= 2:
            overrides.append({"members": members})

    _db().collection(ROSTER_COLLECTION).document(ROSTER_DOC_ID).set({
        "members": data.get("members") or [],
        "previousMatches": data.get("previousMatches") or {},
        "nextMatchOverrides": overrides,
        "skipNextMatchy": bool(data.get("skipNextMatchy")),
        "matchyPaused": bool(data.get("matchyPaused")),
        "lastUpdated": datetime.now(timezone.utc).isoformat(),
    })

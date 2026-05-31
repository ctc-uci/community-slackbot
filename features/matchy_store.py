"""
Firestore persistence for Matchy member/match data.
Collection: matchyData / document: members (same schema as ctc-slackbot).
"""
from datetime import datetime, timezone

from firebase_admin import firestore

from firebase_client import get_firebase_app

MEMBERS_COLLECTION = "matchyData"
MEMBERS_DOC_ID = "members"


def _get_firestore():
    get_firebase_app()
    return firestore.client()


def _empty_data() -> dict:
    return {
        "members": [],
        "previousMatches": {},
        "nextMatchOverrides": [],
        "skipNextMatchy": False,
        "matchyPaused": False,
    }


def _parse_overrides(raw) -> list:
    if not isinstance(raw, list):
        return []
    parsed = []
    for group in raw:
        if isinstance(group, dict) and isinstance(group.get("members"), list):
            members = [m for m in group["members"] if m]
        elif isinstance(group, list):
            members = [m for m in group if m]
        else:
            continue
        if members:
            parsed.append(members)
    return parsed


def load_members_data() -> dict:
    db = _get_firestore()
    doc = db.collection(MEMBERS_COLLECTION).document(MEMBERS_DOC_ID).get()
    if doc.exists:
        data = doc.to_dict() or {}
        return {
            "members": data.get("members") or [],
            "previousMatches": data.get("previousMatches") or {},
            "nextMatchOverrides": _parse_overrides(data.get("nextMatchOverrides")),
            "skipNextMatchy": bool(data.get("skipNextMatchy")),
            "matchyPaused": bool(data.get("matchyPaused")),
        }

    empty = _empty_data()
    try:
        db.collection(MEMBERS_COLLECTION).document(MEMBERS_DOC_ID).set({
            **empty,
            "lastUpdated": datetime.now(timezone.utc).isoformat(),
        })
    except Exception:
        pass
    return empty


def save_members_data(data: dict) -> None:
    db = _get_firestore()
    sanitized = []
    for group in data.get("nextMatchOverrides") or []:
        if isinstance(group, list):
            members = [m for m in group if m]
        elif isinstance(group, dict):
            members = [m for m in (group.get("members") or []) if m]
        else:
            continue
        if members:
            sanitized.append({"members": members})

    db.collection(MEMBERS_COLLECTION).document(MEMBERS_DOC_ID).set({
        "members": data.get("members") or [],
        "previousMatches": data.get("previousMatches") or {},
        "nextMatchOverrides": sanitized,
        "skipNextMatchy": bool(data.get("skipNextMatchy")),
        "matchyPaused": bool(data.get("matchyPaused")),
        "lastUpdated": datetime.now(timezone.utc).isoformat(),
    })

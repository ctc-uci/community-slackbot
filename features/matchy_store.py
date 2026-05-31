"""
Firestore persistence for Matchy member/match data.
Collection: matchyData / document: members
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


def member_matchy_count(slack_id: str, member: dict, previous_matches: dict) -> int:
    """Leaderboard score from stored matchyCount, else rep, else unique partners met."""
    if member.get("matchyCount") is not None:
        return max(0, int(member["matchyCount"]))
    if member.get("rep"):
        return max(0, int(member["rep"]))
    return len(previous_matches.get(slack_id) or [])


def build_leaderboard() -> list[tuple[str, int, str]]:
    """Return [(slack_id, count, name), ...] sorted by count descending."""
    data = load_members_data()
    members = data.get("members") or []
    previous_matches = data.get("previousMatches") or {}
    rows = []
    for member in members:
        slack_id = member.get("slackId")
        if not slack_id:
            continue
        count = member_matchy_count(slack_id, member, previous_matches)
        if count <= 0:
            continue
        name = member.get("name") or slack_id
        rows.append((slack_id, count, name))
    rows.sort(key=lambda r: (-r[1], r[2].lower()))
    return rows


def get_member_matchy_count(slack_id: str) -> int:
    data = load_members_data()
    previous_matches = data.get("previousMatches") or {}
    for member in data.get("members") or []:
        if member.get("slackId") == slack_id:
            return member_matchy_count(slack_id, member, previous_matches)
    return 0


def set_member_matchy_count(slack_id: str, count: int) -> bool:
    data = load_members_data()
    for member in data.get("members") or []:
        if member.get("slackId") == slack_id:
            member["matchyCount"] = max(0, int(count))
            save_members_data(data)
            return True
    return False


def recount_matchy_counts() -> list[tuple[str, int, str]]:
    """Backfill matchyCount from unique partners in previousMatches."""
    data = load_members_data()
    members = data.get("members") or []
    previous_matches = data.get("previousMatches") or {}
    for member in members:
        slack_id = member.get("slackId")
        if slack_id:
            member["matchyCount"] = len(previous_matches.get(slack_id) or [])
    save_members_data(data)
    return build_leaderboard()

"""
Verified Matchy participation: posts and @mentions in the Matchy channel.

Stored in ctc-bot Firestore (collection `matchy`, meta `matchy_meta/state`).
This is the source of truth for the leaderboard — being placed in a weekly group
does not count; only public channel activity is verifiable.
"""
import os
import re
from collections import defaultdict

from firebase_admin import firestore
from slack_sdk import WebClient

from firebase_client import get_firebase_app

MATCHY_CHANNEL_ID = os.environ.get("MATCHY_CHANNEL_ID", "C01FL4VCE1Z")
MENTION_PATTERN = re.compile(r"<@(U[A-Z0-9]+)(?:\|[^>]*)?>")

PARTICIPATION_COLLECTION = "matchy"
PARTICIPATION_META_COLLECTION = "matchy_meta"
PARTICIPATION_META_DOC = "state"


def _get_firestore():
    get_firebase_app()
    return firestore.client()


def _get_user_count(user_id: str) -> int:
    doc = _get_firestore().collection(PARTICIPATION_COLLECTION).document(user_id).get()
    if doc.exists:
        return doc.to_dict().get("participation_count", 0)
    return 0


def set_user_count(user_id: str, value: int) -> None:
    _get_firestore().collection(PARTICIPATION_COLLECTION).document(user_id).set(
        {"participation_count": max(0, int(value))}
    )


def _get_last_processed_ts() -> float:
    doc = _get_firestore().collection(PARTICIPATION_META_COLLECTION).document(
        PARTICIPATION_META_DOC
    ).get()
    if doc.exists:
        return float(doc.to_dict().get("last_processed_ts", 0.0))
    return 0.0


def _set_last_processed_ts(ts: float) -> None:
    _get_firestore().collection(PARTICIPATION_META_COLLECTION).document(
        PARTICIPATION_META_DOC
    ).set({"last_processed_ts": ts}, merge=True)


def _extract_mentions(text: str) -> set:
    if not text:
        return set()
    return set(MENTION_PATTERN.findall(text))


def _fetch_channel_messages(client: WebClient, channel_id: str, oldest: float = 0.0) -> list:
    messages = []
    cursor = None
    while True:
        kwargs = {"channel": channel_id, "limit": 200}
        if cursor:
            kwargs["cursor"] = cursor
        if oldest > 0:
            kwargs["oldest"] = str(oldest)
        response = client.conversations_history(**kwargs)
        for msg in response.get("messages", []):
            if msg.get("bot_id") or not msg.get("text"):
                continue
            if msg.get("subtype", "") in ("channel_join", "channel_leave", "bot_message"):
                continue
            messages.append(msg)
        if not response.get("has_more"):
            break
        cursor = response.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    return messages


def _compute_counts_from_messages(messages: list) -> dict:
    counts = defaultdict(int)
    for msg in messages:
        sender = msg.get("user")
        mentioned = _extract_mentions(msg.get("text", ""))
        participants = set()
        if sender:
            participants.add(sender)
        for uid in mentioned:
            if uid != sender:
                participants.add(uid)
        for uid in participants:
            counts[uid] += 1
    return dict(counts)


def _build_leaderboard_rows() -> list[tuple[str, int]]:
    counts = {}
    for doc in _get_firestore().collection(PARTICIPATION_COLLECTION).stream():
        data = doc.to_dict() or {}
        counts[doc.id] = data.get("participation_count", 0)
    return sorted(counts.items(), key=lambda x: -x[1])


def run_incremental_count(client: WebClient) -> list[tuple[str, int]]:
    if not MATCHY_CHANNEL_ID:
        return []
    last_ts = _get_last_processed_ts()
    oldest = last_ts + 1e-6 if last_ts > 0 else 0.0
    messages = _fetch_channel_messages(client, MATCHY_CHANNEL_ID, oldest=oldest)
    if not messages:
        return _build_leaderboard_rows()

    new_counts = _compute_counts_from_messages(messages)
    max_ts = max(float(m["ts"]) for m in messages)
    db = _get_firestore()

    if last_ts == 0:
        for uid, count in new_counts.items():
            db.collection(PARTICIPATION_COLLECTION).document(uid).set(
                {"participation_count": count}
            )
    else:
        for uid, count in new_counts.items():
            db.collection(PARTICIPATION_COLLECTION).document(uid).set(
                {"participation_count": firestore.Increment(count)}, merge=True
            )

    _set_last_processed_ts(max_ts)
    return _build_leaderboard_rows()


def run_full_recount(client: WebClient) -> list[tuple[str, int]]:
    if not MATCHY_CHANNEL_ID:
        return []
    messages = _fetch_channel_messages(client, MATCHY_CHANNEL_ID)
    counts = _compute_counts_from_messages(messages)
    db = _get_firestore()
    for uid, count in counts.items():
        db.collection(PARTICIPATION_COLLECTION).document(uid).set(
            {"participation_count": count}
        )
    if messages:
        _set_last_processed_ts(max(float(m["ts"]) for m in messages))
    return sorted(counts.items(), key=lambda x: -x[1])


def get_participation_count(user_id: str) -> int:
    return _get_user_count(user_id)


def startup_catchup() -> None:
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token or not MATCHY_CHANNEL_ID:
        return
    client = WebClient(token=token)
    try:
        client.conversations_join(channel=MATCHY_CHANNEL_ID)
    except Exception:
        pass
    try:
        run_incremental_count(client)
    except Exception:
        pass

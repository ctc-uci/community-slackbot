"""
Verified Matchy participation in the Matchy channel (top-level messages only).

Counts a message when someone @mentions another user (not themselves):
- +1 for the sender who @'d someone else
- +1 for each other user @mentioned

Thread replies are ignored. Counts are stored in Firestore on message events;
conversations.history only catches up gaps while the bot was offline.

Requires: message.channels subscription, bot in MATCHY_CHANNEL_ID.
"""
import logging
import os
import re
import threading
from collections import defaultdict

from firebase_admin import firestore
from slack_sdk import WebClient

from firebase_client import get_firebase_app

logger = logging.getLogger(__name__)

MATCHY_CHANNEL_ID = os.environ.get("MATCHY_CHANNEL_ID", "C01FL4VCE1Z")
MENTION_PATTERN = re.compile(r"<@(U[A-Z0-9]+)(?:\|[^>]*)?>")

PARTICIPATION_COLLECTION = "matchy"
PARTICIPATION_META_COLLECTION = "matchy_meta"
PARTICIPATION_META_DOC = "state"

_apply_lock = threading.Lock()

# Slack non-Enterprise plans typically expose ~90 days via conversations.history
SLACK_HISTORY_DAYS_HINT = 90


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


def _is_thread_reply(msg: dict) -> bool:
    thread_ts = msg.get("thread_ts")
    if not thread_ts:
        return False
    return str(thread_ts) != str(msg.get("ts", ""))


def _should_count_message(msg: dict) -> bool:
    if msg.get("bot_id") or not msg.get("text"):
        return False
    if _is_thread_reply(msg):
        return False
    return msg.get("subtype", "") not in ("channel_join", "channel_leave", "bot_message")


def _participants_from_message(msg: dict) -> set:
    """Users who earn +1 from this message (sender @'d someone else, or was @'d)."""
    sender = msg.get("user")
    if not sender:
        return set()

    mentioned = _extract_mentions(msg.get("text", ""))
    others_mentioned = {uid for uid in mentioned if uid != sender}
    if not others_mentioned:
        return set()

    participants = {sender}
    participants.update(others_mentioned)
    return participants


def _apply_message(msg: dict, *, force: bool = False) -> bool:
    """
    Increment participation for one message. Returns True if counted.
    Skips messages at or before last_processed_ts unless force=True (full recount).
    """
    if not _should_count_message(msg):
        return False

    ts = float(msg.get("ts", 0))
    with _apply_lock:
        if not force:
            last_ts = _get_last_processed_ts()
            if ts <= last_ts + 1e-9:
                return False

        participants = _participants_from_message(msg)
        if not participants:
            return False

        db = _get_firestore()
        for uid in participants:
            db.collection(PARTICIPATION_COLLECTION).document(uid).set(
                {"participation_count": firestore.Increment(1)}, merge=True
            )

        if not force:
            last_ts = _get_last_processed_ts()
            if ts > last_ts:
                _set_last_processed_ts(ts)
    return True


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
            if _should_count_message(msg):
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
        for uid in _participants_from_message(msg):
            counts[uid] += 1
    return dict(counts)


def _build_leaderboard_rows() -> list[tuple[str, int]]:
    counts = {}
    for doc in _get_firestore().collection(PARTICIPATION_COLLECTION).stream():
        data = doc.to_dict() or {}
        counts[doc.id] = data.get("participation_count", 0)
    return sorted(counts.items(), key=lambda x: -x[1])


def handle_message_event(event: dict) -> None:
    """Real-time: count each new message in the Matchy channel."""
    if not MATCHY_CHANNEL_ID:
        return
    channel = event.get("channel")
    if channel != MATCHY_CHANNEL_ID:
        return
    try:
        _apply_message(event)
    except Exception:
        logger.exception("Matchy participation: failed to record message event")


def register_participation_events(app) -> None:
    @app.event("message")
    def _matchy_channel_message(event, logger):
        if event.get("channel") != MATCHY_CHANNEL_ID:
            return
        if event.get("subtype") in ("message_changed", "message_deleted"):
            return
        threading.Thread(target=handle_message_event, args=(event,), daemon=True).start()


def run_incremental_count(client: WebClient) -> list[tuple[str, int]]:
    """Catch up messages since last_processed_ts (Slack may cap history ~90 days)."""
    if not MATCHY_CHANNEL_ID:
        return []

    last_ts = _get_last_processed_ts()
    if last_ts == 0:
        rows, _ = run_full_recount(client)
        return rows

    messages = _fetch_channel_messages(
        client, MATCHY_CHANNEL_ID, oldest=last_ts + 1e-6
    )
    for msg in messages:
        _apply_message(msg)

    return _build_leaderboard_rows()


def run_full_recount(client: WebClient) -> tuple[list[tuple[str, int]], str]:
    """
    Rebuild counts from Slack channel history. On free/pro workspaces Slack only
    returns ~90 days — older totals only exist if the bot was recording live.
    Does not delete Firestore docs for users absent from that window.
    """
    if not MATCHY_CHANNEL_ID:
        return [], ""

    messages = _fetch_channel_messages(client, MATCHY_CHANNEL_ID)
    counts = _compute_counts_from_messages(messages)
    db = _get_firestore()

    for uid, count in counts.items():
        db.collection(PARTICIPATION_COLLECTION).document(uid).set(
            {"participation_count": count}
        )

    if messages:
        _set_last_processed_ts(max(float(m["ts"]) for m in messages))

    note = (
        f"_Recount used Slack history (~{SLACK_HISTORY_DAYS_HINT} days max on non-Enterprise). "
        "Older activity is only included if the bot was running and recording messages live._"
    )
    return sorted(counts.items(), key=lambda x: -x[1]), note


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
        logger.exception("Matchy participation startup catch-up failed")

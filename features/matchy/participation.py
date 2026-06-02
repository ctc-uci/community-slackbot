"""Channel participation leaderboard (@mentions, top-level only)."""
import logging
import re
import threading
from collections import defaultdict

from firebase_admin import firestore
from slack_sdk import WebClient

from features.matchy.config import CHANNEL_ID, SLACK_HISTORY_DAYS_HINT
from firebase_client import get_firebase_app

logger = logging.getLogger(__name__)

MENTION_RE = re.compile(r"<@(U[A-Z0-9]+)(?:\|[^>]*)?>", re.I)

COUNTS_COLLECTION = "matchy"
META_COLLECTION = "matchy_meta"
META_DOC = "state"

_lock = threading.Lock()


def _db():
    get_firebase_app()
    return firestore.client()


def get_count(user_id: str) -> int:
    doc = _db().collection(COUNTS_COLLECTION).document(user_id).get()
    return (doc.to_dict() or {}).get("participation_count", 0) if doc.exists else 0


def set_count(user_id: str, value: int) -> None:
    _db().collection(COUNTS_COLLECTION).document(user_id).set(
        {"participation_count": max(0, int(value))}
    )


def _last_processed_ts() -> float:
    doc = _db().collection(META_COLLECTION).document(META_DOC).get()
    return float((doc.to_dict() or {}).get("last_processed_ts", 0.0)) if doc.exists else 0.0


def _set_last_processed_ts(ts: float) -> None:
    _db().collection(META_COLLECTION).document(META_DOC).set(
        {"last_processed_ts": ts}, merge=True
    )


def _is_thread_reply(msg: dict) -> bool:
    thread_ts = msg.get("thread_ts")
    return bool(thread_ts and str(thread_ts) != str(msg.get("ts", "")))


def _counts_for_message(msg: dict) -> set[str]:
    if msg.get("bot_id") or not msg.get("text") or _is_thread_reply(msg):
        return set()
    if msg.get("subtype", "") in ("channel_join", "channel_leave", "bot_message"):
        return set()

    sender = msg.get("user")
    if not sender:
        return set()

    others = {u for u in MENTION_RE.findall(msg.get("text", "")) if u != sender}
    if not others:
        return set()
    return {sender, *others}


def _apply_message(msg: dict, *, force: bool = False) -> bool:
    if not _counts_for_message(msg):
        return False

    ts = float(msg.get("ts", 0))
    with _lock:
        if not force and ts <= _last_processed_ts() + 1e-9:
            return False

        for uid in _counts_for_message(msg):
            _db().collection(COUNTS_COLLECTION).document(uid).set(
                {"participation_count": firestore.Increment(1)}, merge=True
            )

        if not force and ts > _last_processed_ts():
            _set_last_processed_ts(ts)
    return True


def _fetch_messages(client: WebClient, oldest: float = 0.0) -> list:
    messages = []
    cursor = None
    while True:
        kwargs = {"channel": CHANNEL_ID, "limit": 200}
        if cursor:
            kwargs["cursor"] = cursor
        if oldest > 0:
            kwargs["oldest"] = str(oldest)
        resp = client.conversations_history(**kwargs)
        messages.extend(m for m in resp.get("messages", []) if _counts_for_message(m))
        if not resp.get("has_more"):
            break
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    return messages


def leaderboard_rows() -> list[tuple[str, int]]:
    rows = {}
    for doc in _db().collection(COUNTS_COLLECTION).stream():
        rows[doc.id] = (doc.to_dict() or {}).get("participation_count", 0)
    return sorted(rows.items(), key=lambda x: -x[1])


def run_incremental(client: WebClient) -> list[tuple[str, int]]:
    if not CHANNEL_ID:
        return []
    last = _last_processed_ts()
    if last == 0:
        rows, _ = run_full_recount(client)
        return rows
    for msg in _fetch_messages(client, oldest=last + 1e-6):
        _apply_message(msg)
    return leaderboard_rows()


def run_full_recount(client: WebClient) -> tuple[list[tuple[str, int]], str]:
    if not CHANNEL_ID:
        return [], ""

    messages = _fetch_messages(client)
    totals: dict[str, int] = defaultdict(int)
    for msg in messages:
        for uid in _counts_for_message(msg):
            totals[uid] += 1

    for uid, count in totals.items():
        _db().collection(COUNTS_COLLECTION).document(uid).set(
            {"participation_count": count}
        )
    if messages:
        _set_last_processed_ts(max(float(m["ts"]) for m in messages))

    note = (
        f"_Recount used Slack history (~{SLACK_HISTORY_DAYS_HINT} days max on non-Enterprise). "
        "Older activity is only included if the bot was recording messages live._"
    )
    return sorted(totals.items(), key=lambda x: -x[1]), note


def startup_catchup() -> None:
    import os

    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    if not token or not CHANNEL_ID:
        return
    client = WebClient(token=token)
    try:
        client.conversations_join(channel=CHANNEL_ID)
    except Exception:
        pass
    try:
        run_incremental(client)
    except Exception:
        logger.exception("Matchy participation startup catch-up failed")


def register_events(app) -> None:
    @app.event("message")
    def on_message(event, logger):
        if event.get("channel") != CHANNEL_ID:
            return
        if event.get("subtype") in ("message_changed", "message_deleted"):
            return

        def work():
            try:
                _apply_message(event)
            except Exception:
                logger.exception("Matchy participation event failed")

        threading.Thread(target=work, daemon=True).start()

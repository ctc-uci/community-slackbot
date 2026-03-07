"""
CTC Spottings bot: track who spots whom on campus.

Counting strategy (incremental):
- Stores last_processed_ts in Firebase (spottings_meta/state)
- On each scheduled run (every 24h): fetches only new messages since that timestamp,
  atomically increments existing Firebase counts, updates the stored timestamp
- On first run (no stored timestamp): bootstraps from full channel history
- Admins can force a full recount via /recount-spottings at any time

Admin commands:
- /edit-spotting  — manually set a user's spotting count
- /edit-spotted   — manually set a user's spotted count
- /recount-spottings — full history recount (resets incremental pointer)

Cooldown: same (spotter, spotted) pair within 30s counts once.
Same person tagged twice in one message counts once (set deduplication).
"""
import json
import os
import re
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta

import pytz
from slack_sdk import WebClient

from firebase_client import get_firebase_app
from firebase_admin import firestore

# CTC-spottings channel ID (set SPOTTINGS_CHANNEL_ID in .env to override)
SPOTTINGS_CHANNEL_ID = os.environ.get("SPOTTINGS_CHANNEL_ID", "")
TIMEZONE = pytz.timezone(os.environ.get("TZ", "America/Los_Angeles"))

# User IDs allowed to use admin commands
ADMIN_USER_IDS = frozenset({
    "U0631Q51G04",
    "U07T20PN1GB",
    "U063K9AG40Y",
    "U07T4JEEUSG",
})

# Regex to extract user IDs from Slack message text: <@U123> or <@U123|display>
MENTION_PATTERN = re.compile(r"<@(U[A-Z0-9]+)(?:\|[^>]*)?>")

# Firestore collections
FIRESTORE_COLLECTION = "spottings"           # per-user count documents
SPOTTINGS_META_COLLECTION = "spottings_meta" # bot state
SPOTTINGS_META_DOC = "state"                 # single document in spottings_meta


# ---------------------------------------------------------------------------
# Firestore helpers
# ---------------------------------------------------------------------------

def _get_firestore():
    """Get Firestore client."""
    get_firebase_app()
    return firestore.client()


def _get_user_counts(user_id: str) -> dict:
    """Get spotting_count and spotted_count for a user from Firebase."""
    db = _get_firestore()
    doc = db.collection(FIRESTORE_COLLECTION).document(user_id).get()
    if doc.exists:
        data = doc.to_dict()
        return {
            "spotting_count": data.get("spotting_count", 0),
            "spotted_count": data.get("spotted_count", 0),
        }
    return {"spotting_count": 0, "spotted_count": 0}


def _set_user_count(user_id: str, count_type: str, value: int) -> None:
    """Set spotting_count or spotted_count for a user in Firebase."""
    db = _get_firestore()
    doc_ref = db.collection(FIRESTORE_COLLECTION).document(user_id)
    doc = doc_ref.get()
    if doc.exists:
        doc_ref.update({count_type: value})
    else:
        counts = {"spotting_count": 0, "spotted_count": 0}
        counts[count_type] = value
        doc_ref.set(counts)


def _get_last_processed_ts() -> float:
    """Return the Unix timestamp of the last processed message (0.0 if never run)."""
    db = _get_firestore()
    doc = db.collection(SPOTTINGS_META_COLLECTION).document(SPOTTINGS_META_DOC).get()
    if doc.exists:
        return float(doc.to_dict().get("last_processed_ts", 0.0))
    return 0.0


def _set_last_processed_ts(ts: float) -> None:
    """Store the Unix timestamp of the most recently processed message."""
    db = _get_firestore()
    db.collection(SPOTTINGS_META_COLLECTION).document(SPOTTINGS_META_DOC).set(
        {"last_processed_ts": ts}, merge=True
    )


def _build_leaderboard_from_firebase() -> tuple[list, list]:
    """
    Stream all user docs from Firebase and return sorted leaderboards.
    Used after incremental updates where we only know the delta, not totals.
    Returns (spotting_leaderboard, spotted_leaderboard) as sorted lists of (uid, count).
    """
    db = _get_firestore()
    spotting_counts = {}
    spotted_counts = {}
    for doc in db.collection(FIRESTORE_COLLECTION).stream():
        data = doc.to_dict() or {}
        uid = doc.id
        spotting_counts[uid] = data.get("spotting_count", 0)
        spotted_counts[uid] = data.get("spotted_count", 0)

    spotting_lb = sorted(spotting_counts.items(), key=lambda x: -x[1])
    spotted_lb = sorted(spotted_counts.items(), key=lambda x: -x[1])
    return spotting_lb, spotted_lb


# ---------------------------------------------------------------------------
# Message processing
# ---------------------------------------------------------------------------

def _extract_mentions(text: str) -> set:
    """Extract unique user IDs from message text."""
    if not text:
        return set()
    return set(MENTION_PATTERN.findall(text))


def _fetch_channel_messages(client: WebClient, channel_id: str, oldest: float = 0.0) -> list:
    """
    Fetch messages from channel (paginated). Excludes bot/system messages.
    oldest: if > 0, only fetch messages with ts > oldest (incremental mode).
    """
    messages = []
    cursor = None
    while True:
        kwargs = {"channel": channel_id, "limit": 200}
        if cursor:
            kwargs["cursor"] = cursor
        if oldest > 0:
            kwargs["oldest"] = str(oldest)
        response = client.conversations_history(**kwargs)
        batch = response.get("messages", [])
        for msg in batch:
            if msg.get("bot_id"):
                continue
            if not msg.get("text"):
                continue
            subtype = msg.get("subtype", "")
            if subtype in ("channel_join", "channel_leave", "bot_message"):
                continue
            messages.append(msg)
        if not response.get("has_more"):
            break
        cursor = response.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    return messages


def _apply_cooldown(pairs_with_ts: list, cooldown_sec: int = 30) -> list:
    """
    Given [(spotter, spotted, ts), ...], return unique (spotter, spotted) pairs
    applying cooldown: same pair within cooldown_sec counts once.
    """
    sorted_pairs = sorted(pairs_with_ts, key=lambda x: x[2])
    result = []
    last_ts = {}
    for spotter, spotted, ts in sorted_pairs:
        key = (spotter, spotted)
        prev = last_ts.get(key, -float("inf"))
        if ts - prev >= cooldown_sec:
            result.append((spotter, spotted))
            last_ts[key] = ts
    return result


def _compute_counts_from_messages(messages: list) -> tuple[dict, dict]:
    """
    Compute spotting_count and spotted_count from messages.
    Returns (spotting_counts, spotted_counts) as dicts user_id -> count.
    """
    pairs_with_ts = []
    for msg in messages:
        spotter = msg.get("user")
        if not spotter:
            continue
        text = msg.get("text", "")
        mentioned = _extract_mentions(text)
        if not mentioned:
            continue
        ts = float(msg.get("ts", 0))
        for spotted in mentioned:
            if spotted != spotter:
                pairs_with_ts.append((spotter, spotted, ts))

    unique_pairs = _apply_cooldown(pairs_with_ts, cooldown_sec=30)

    spotting_counts = defaultdict(int)
    spotted_counts = defaultdict(int)
    for spotter, spotted in unique_pairs:
        spotting_counts[spotter] += 1
        spotted_counts[spotted] += 1

    return dict(spotting_counts), dict(spotted_counts)


# ---------------------------------------------------------------------------
# Counting runs
# ---------------------------------------------------------------------------

def _run_incremental_count(client: WebClient) -> tuple[list, list]:
    """
    Fetch only messages newer than last_processed_ts, atomically increment
    Firebase counts, update the stored timestamp, return full leaderboard.

    On first run (last_processed_ts == 0): bootstraps from full history using
    plain set() to establish baseline counts.
    """
    channel_id = SPOTTINGS_CHANNEL_ID
    if not channel_id:
        return [], []

    last_ts = _get_last_processed_ts()
    # Add tiny epsilon so Slack's inclusive oldest filter excludes the boundary message
    oldest = last_ts + 1e-6 if last_ts > 0 else 0.0
    messages = _fetch_channel_messages(client, channel_id, oldest=oldest)

    if not messages:
        return _build_leaderboard_from_firebase()

    new_spotting, new_spotted = _compute_counts_from_messages(messages)
    max_ts = max(float(m["ts"]) for m in messages)

    db = _get_firestore()
    all_uids = set(new_spotting.keys()) | set(new_spotted.keys())

    if last_ts == 0:
        # Bootstrap: plain set (no existing counts to preserve)
        for uid in all_uids:
            db.collection(FIRESTORE_COLLECTION).document(uid).set({
                "spotting_count": new_spotting.get(uid, 0),
                "spotted_count": new_spotted.get(uid, 0),
            })
    else:
        # Incremental: atomic increment so concurrent writes don't race
        for uid in all_uids:
            db.collection(FIRESTORE_COLLECTION).document(uid).set({
                "spotting_count": firestore.Increment(new_spotting.get(uid, 0)),
                "spotted_count": firestore.Increment(new_spotted.get(uid, 0)),
            }, merge=True)

    _set_last_processed_ts(max_ts)
    return _build_leaderboard_from_firebase()


def _run_full_recount(client: WebClient) -> tuple[list, list]:
    """
    Full channel history recount — fetches all messages, overwrites Firebase
    counts, resets the incremental timestamp pointer.
    Used by the /recount-spottings admin command.
    """
    channel_id = SPOTTINGS_CHANNEL_ID
    if not channel_id:
        return [], []

    messages = _fetch_channel_messages(client, channel_id)
    spotting_counts, spotted_counts = _compute_counts_from_messages(messages)

    db = _get_firestore()
    all_uids = set(spotting_counts.keys()) | set(spotted_counts.keys())
    for uid in all_uids:
        db.collection(FIRESTORE_COLLECTION).document(uid).set({
            "spotting_count": spotting_counts.get(uid, 0),
            "spotted_count": spotted_counts.get(uid, 0),
        })

    if messages:
        max_ts = max(float(m["ts"]) for m in messages)
        _set_last_processed_ts(max_ts)

    spotting_lb = sorted(spotting_counts.items(), key=lambda x: -x[1])
    spotted_lb = sorted(spotted_counts.items(), key=lambda x: -x[1])
    return spotting_lb, spotted_lb


# ---------------------------------------------------------------------------
# Leaderboard posting
# ---------------------------------------------------------------------------

def _build_leaderboard_blocks(spotting_leaderboard: list, spotted_leaderboard: list) -> list:
    """Build Slack blocks for the leaderboard message."""
    def format_row(rank: int, user_id: str, count: int) -> str:
        return f"{rank}. <@{user_id}> \u2014 {count}"

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "🏆 CTC Spottings Leaderboard", "emoji": True}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "*Most Spottings Posted*"}},
    ]
    if spotting_leaderboard:
        lines = [format_row(i + 1, uid, c) for i, (uid, c) in enumerate(spotting_leaderboard[:10])]
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}})
    else:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "_No spottings yet!_"}})

    blocks.append({"type": "divider"})
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "*Most Spotted*"}})
    if spotted_leaderboard:
        lines = [format_row(i + 1, uid, c) for i, (uid, c) in enumerate(spotted_leaderboard[:10])]
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}})
    else:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "_No one spotted yet!_"}})

    return blocks


def _post_leaderboard(client: WebClient, spotting_leaderboard: list, spotted_leaderboard: list) -> None:
    """Post leaderboard message to the spottings channel."""
    channel_id = SPOTTINGS_CHANNEL_ID
    if not channel_id:
        return
    blocks = _build_leaderboard_blocks(spotting_leaderboard, spotted_leaderboard)
    client.chat_postMessage(channel=channel_id, text="CTC Spottings Leaderboard", blocks=blocks)


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

def _seconds_until_next_run(target_hour: int, target_minute: int) -> float:
    """Seconds until next occurrence of target_hour:target_minute in Pacific."""
    now = datetime.now(TIMEZONE)
    target = now.replace(hour=target_hour, minute=target_minute, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def _scheduler_loop() -> None:
    """
    Background loop:
    - On startup: silent catch-up (process any messages missed while offline)
    - Every 24 hours: incremental count + post leaderboard
    """
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token or not SPOTTINGS_CHANNEL_ID:
        return

    client = WebClient(token=token)

    # Ensure the bot is a member of the spottings channel so conversations.history works.
    # Requires the channels:join OAuth scope. No-ops if already a member.
    try:
        client.conversations_join(channel=SPOTTINGS_CHANNEL_ID)
    except Exception:
        pass  # private channel or already a member — proceed anyway

    # Startup catch-up — no leaderboard post so channel isn't flooded on restarts
    try:
        _run_incremental_count(client)
    except Exception:
        pass

    while True:
        time.sleep(86400)  # 24 hours
        try:
            spotting_lb, spotted_lb = _run_incremental_count(client)
            _post_leaderboard(client, spotting_lb, spotted_lb)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Modal builders
# ---------------------------------------------------------------------------

def _build_edit_modal_blocks(default_type: str) -> list:
    """Build modal blocks for editing spotting/spotted counts (4 blocks)."""
    type_options = [
        {"text": {"type": "plain_text", "text": "Spotting"}, "value": "spotting"},
        {"text": {"type": "plain_text", "text": "Spotted"}, "value": "spotted"},
    ]
    initial_type = next((o for o in type_options if o["value"] == default_type), type_options[0])

    return [
        {
            "type": "input",
            "dispatch_action": True,
            "block_id": "user_block",
            "element": {
                "type": "users_select",
                "action_id": "spottings_user_select",
                "placeholder": {"type": "plain_text", "text": "Select a user"},
            },
            "label": {"type": "plain_text", "text": "User"},
        },
        {
            "type": "input",
            "dispatch_action": True,
            "block_id": "type_block",
            "element": {
                "type": "static_select",
                "action_id": "spottings_type_select",
                "placeholder": {"type": "plain_text", "text": "Spotting or Spotted"},
                "options": type_options,
                "initial_option": initial_type,
            },
            "label": {"type": "plain_text", "text": "Edit count type"},
        },
        {
            # Read-only display — section block, not an input, so not submitted
            "type": "section",
            "block_id": "current_count_display",
            "text": {
                "type": "mrkdwn",
                "text": "*Current count:* _Select a user above_",
            },
        },
        {
            "type": "input",
            "block_id": "count_block",
            "element": {
                "type": "plain_text_input",
                "action_id": "spottings_count_input",
                "placeholder": {"type": "plain_text", "text": "Enter new count"},
                "initial_value": "",
            },
            "label": {"type": "plain_text", "text": "New count"},
        },
    ]


def _build_edit_modal_view(blocks: list, callback_id: str, title: str, default_type: str) -> dict:
    """Build full modal view. Stores default_type in private_metadata for handler fallback."""
    return {
        "type": "modal",
        "callback_id": callback_id,
        "title": {"type": "plain_text", "text": title},
        "submit": {"type": "plain_text", "text": "Save"},
        "private_metadata": json.dumps({"default_type": default_type}),
        "blocks": blocks,
    }


# ---------------------------------------------------------------------------
# Handler registration
# ---------------------------------------------------------------------------

def register_spottings_handlers(app):
    """Register spottings-related handlers with the Bolt app."""

    def _check_admin(user_id: str) -> bool:
        return user_id in ADMIN_USER_IDS

    def _open_edit_modal(ack, body, client, default_type: str, title: str, logger):
        ack()
        user_id = body["user_id"]
        if not _check_admin(user_id):
            try:
                client.chat_postEphemeral(
                    channel=body.get("channel_id", ""),
                    user=user_id,
                    text="You don't have permission to use this command.",
                )
            except Exception:
                pass
            return

        trigger_id = body.get("trigger_id")
        if not trigger_id:
            logger.error("Missing trigger_id in edit modal payload")
            return

        blocks = _build_edit_modal_blocks(default_type)
        view = _build_edit_modal_view(blocks, "spottings_edit_modal", title, default_type)
        client.views_open(trigger_id=trigger_id, view=view)

    @app.command("/edit-spotting")
    def cmd_edit_spotting(ack, body, client, logger):
        _open_edit_modal(ack, body, client, "spotting", "Edit Spotting Count", logger)

    @app.command("/edit-spotted")
    def cmd_edit_spotted(ack, body, client, logger):
        _open_edit_modal(ack, body, client, "spotted", "Edit Spotted Count", logger)

    @app.command("/recount-spottings")
    def cmd_recount_spottings(ack, body, client, logger):
        """Admin command: full channel history recount, resets incremental pointer."""
        ack()
        user_id = body["user_id"]
        if not _check_admin(user_id):
            try:
                client.chat_postEphemeral(
                    channel=body.get("channel_id", ""),
                    user=user_id,
                    text="You don't have permission to use this command.",
                )
            except Exception:
                pass
            return

        # Run in background thread — full recount can take a while and Slack
        # requires ack() within 3 seconds (already called above).
        def _do_recount():
            try:
                spotting_lb, spotted_lb = _run_full_recount(client)
                top_spotting = ", ".join(
                    f"<@{uid}>: {c}" for uid, c in spotting_lb[:5]
                ) or "none"
                top_spotted = ", ".join(
                    f"<@{uid}>: {c}" for uid, c in spotted_lb[:5]
                ) or "none"
                msg = (
                    f"Recount complete!\n"
                    f"*Top spotters:* {top_spotting}\n"
                    f"*Top spotted:* {top_spotted}"
                )
            except Exception as e:
                logger.error(f"Recount failed: {e}")
                msg = f"Recount failed: {e}"

            channel_id = body.get("channel_id", "")
            if channel_id:
                try:
                    client.chat_postEphemeral(channel=channel_id, user=user_id, text=msg)
                except Exception:
                    pass

        threading.Thread(target=_do_recount, daemon=True).start()

    @app.action("spottings_user_select")
    @app.action("spottings_type_select")
    def handle_spottings_select(ack, body, client):
        """
        When admin selects a user or changes type, fetch current count from Firebase
        and update the modal with the read-only display and pre-filled input.
        """
        ack()
        view = body.get("view")
        if not view:
            return

        values = view.get("state", {}).get("values", {})
        actions = body.get("actions", [])
        action = actions[0] if actions else {}

        # Resolve selected user: from the action itself or from view state
        selected_user = action.get("selected_user")
        if not selected_user:
            user_block = values.get("user_block", {}).get("spottings_user_select", {})
            selected_user = user_block.get("selected_user")

        # Resolve count type: from action, view state, or private_metadata fallback
        type_opt = action.get("selected_option") if action.get("type") == "static_select" else None
        if not type_opt:
            type_block = values.get("type_block", {}).get("spottings_type_select", {})
            type_opt = type_block.get("selected_option")

        count_type = type_opt.get("value") if type_opt else None

        # Fallback: read default_type from private_metadata (set when modal was opened)
        # This fires when admin selects a user without explicitly touching the type dropdown,
        # since Slack doesn't report initial_option in state until the element is interacted with.
        if not count_type:
            meta = json.loads(view.get("private_metadata") or "{}")
            count_type = meta.get("default_type", "spotting")

        if not selected_user:
            return

        counts = _get_user_counts(selected_user)
        if count_type == "spotting":
            count_value = str(counts["spotting_count"])
            display_label = "Current spotting count"
        else:
            count_value = str(counts["spotted_count"])
            display_label = "Current spotted count"

        type_options = [
            {"text": {"type": "plain_text", "text": "Spotting"}, "value": "spotting"},
            {"text": {"type": "plain_text", "text": "Spotted"}, "value": "spotted"},
        ]
        initial_type = next((o for o in type_options if o["value"] == count_type), type_options[0])

        blocks = [
            {
                "type": "input",
                "dispatch_action": True,
                "block_id": "user_block",
                "element": {
                    "type": "users_select",
                    "action_id": "spottings_user_select",
                    "placeholder": {"type": "plain_text", "text": "Select a user"},
                    "initial_user": selected_user,
                },
                "label": {"type": "plain_text", "text": "User"},
            },
            {
                "type": "input",
                "dispatch_action": True,
                "block_id": "type_block",
                "element": {
                    "type": "static_select",
                    "action_id": "spottings_type_select",
                    "placeholder": {"type": "plain_text", "text": "Spotting or Spotted"},
                    "options": type_options,
                    "initial_option": initial_type,
                },
                "label": {"type": "plain_text", "text": "Edit count type"},
            },
            {
                "type": "section",
                "block_id": "current_count_display",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*{display_label}:* {count_value}",
                },
            },
            {
                "type": "input",
                "block_id": "count_block",
                "element": {
                    "type": "plain_text_input",
                    "action_id": "spottings_count_input",
                    "placeholder": {"type": "plain_text", "text": "Enter new count"},
                    "initial_value": count_value,
                },
                "label": {"type": "plain_text", "text": "New count"},
            },
        ]

        client.views_update(
            view_id=view["id"],
            hash=view.get("hash"),
            view={
                "type": "modal",
                "callback_id": view.get("callback_id", "spottings_edit_modal"),
                "title": view.get("title", {}),
                "submit": {"type": "plain_text", "text": "Save"},
                "private_metadata": view.get("private_metadata", ""),
                "blocks": blocks,
            },
        )

    @app.view("spottings_edit_modal")
    def handle_spottings_edit_submit(ack, body, client, view):
        ack()
        user_id = body["user"]["id"]
        if not _check_admin(user_id):
            return

        values = view["state"]["values"]
        user_block = values.get("user_block", {}).get("spottings_user_select", {})
        type_block = values.get("type_block", {}).get("spottings_type_select", {})
        count_block = values.get("count_block", {}).get("spottings_count_input", {})

        target_user = user_block.get("selected_user")
        type_opt = type_block.get("selected_option")
        count_type = type_opt.get("value") if type_opt else None
        count_raw = (count_block.get("value") or "").strip()

        if not target_user or not count_type:
            return

        try:
            new_count = int(count_raw)
            if new_count < 0:
                new_count = 0
        except ValueError:
            new_count = 0

        count_key = "spotting_count" if count_type == "spotting" else "spotted_count"
        _set_user_count(target_user, count_key, new_count)

        channel_id = body.get("channel") or body.get("container", {}).get("channel_id")
        if channel_id:
            client.chat_postEphemeral(
                channel=channel_id,
                user=user_id,
                text=f"Updated <@{target_user}>'s {count_type} count to {new_count}.",
            )

    # Start background scheduler
    t = threading.Thread(target=_scheduler_loop, daemon=True)
    t.start()

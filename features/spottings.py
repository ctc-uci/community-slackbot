"""
CTC Spottings bot: track who spots whom on campus.

- Runs nightly at 11:59 PM Pacific to count mentions in CTC-spottings channel
- Posts leaderboard at 12 AM Pacific
- 30-second cooldown: same (spotter, spotted) pair within 30s counts once
- Same person tagged twice in one message counts once
- Admin commands /edit-spotting and /edit-spotted for manual count edits
"""
import os
import re
import threading
import time
from datetime import datetime, timedelta
from collections import defaultdict

import pytz
from slack_sdk import WebClient

from firebase_client import get_firebase_app
from firebase_admin import firestore

# CTC-spottings channel ID (set SPOTTINGS_CHANNEL_ID in .env to override)
SPOTTINGS_CHANNEL_ID = os.environ.get("SPOTTINGS_CHANNEL_ID", "")
TIMEZONE = pytz.timezone(os.environ.get("TZ", "America/Los_Angeles"))

# User IDs allowed to use /edit-spotting and /edit-spotted
ADMIN_USER_IDS = frozenset({
    "U0631Q51G04",
    "U07T20PN1GB",
    "U063K9AG40Y",
    "U07T4JEEUSG",
})

# Regex to extract user IDs from Slack message text: <@U123> or <@U123|display>
MENTION_PATTERN = re.compile(r"<@(U[A-Z0-9]+)(?:\|[^>]*)?>")

# Firestore collection for spotting/spotted counts
FIRESTORE_COLLECTION = "spottings"


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


def _extract_mentions(text: str) -> set:
    """Extract unique user IDs from message text."""
    if not text:
        return set()
    return set(MENTION_PATTERN.findall(text))


def _fetch_channel_messages(client: WebClient, channel_id: str) -> list:
    """Fetch all messages from channel (paginated). Excludes bot messages."""
    messages = []
    cursor = None
    while True:
        kwargs = {"channel": channel_id, "limit": 200}
        if cursor:
            kwargs["cursor"] = cursor
        response = client.conversations_history(**kwargs)
        batch = response.get("messages", [])
        for msg in batch:
            # Skip bot messages and messages without text
            if msg.get("bot_id"):
                continue
            if not msg.get("text"):
                continue
            # Skip if subtype indicates it's not a normal user message
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
    # Sort by timestamp
    sorted_pairs = sorted(pairs_with_ts, key=lambda x: x[2])
    result = []
    last_ts = {}  # (spotter, spotted) -> last timestamp we counted
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
    # (spotter, spotted, ts) with cooldown applied
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
            if spotted != spotter:  # Don't count self-mentions
                pairs_with_ts.append((spotter, spotted, ts))

    unique_pairs = _apply_cooldown(pairs_with_ts, cooldown_sec=30)

    spotting_counts = defaultdict(int)
    spotted_counts = defaultdict(int)
    for spotter, spotted in unique_pairs:
        spotting_counts[spotter] += 1
        spotted_counts[spotted] += 1

    return dict(spotting_counts), dict(spotted_counts)


def _build_leaderboard_blocks(spotting_leaderboard: list, spotted_leaderboard: list) -> list:
    """Build Slack blocks for the leaderboard message."""
    def format_row(rank: int, user_id: str, count: int) -> str:
        return f"{rank}. <@{user_id}> — {count}"

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


def _run_nightly_count(client: WebClient) -> tuple[list, list]:
    """Fetch channel messages, compute counts, update Firebase. Returns (spotting_lb, spotted_lb)."""
    channel_id = SPOTTINGS_CHANNEL_ID
    if not channel_id:
        return [], []

    messages = _fetch_channel_messages(client, channel_id)
    spotting_counts, spotted_counts = _compute_counts_from_messages(messages)

    # Update Firebase with computed counts (overwrite)
    db = _get_firestore()
    all_user_ids = set(spotting_counts.keys()) | set(spotted_counts.keys())
    for uid in all_user_ids:
        doc_ref = db.collection(FIRESTORE_COLLECTION).document(uid)
        doc_ref.set({
            "spotting_count": spotting_counts.get(uid, 0),
            "spotted_count": spotted_counts.get(uid, 0),
        })

    spotting_leaderboard = sorted(spotting_counts.items(), key=lambda x: -x[1])
    spotted_leaderboard = sorted(spotted_counts.items(), key=lambda x: -x[1])
    return spotting_leaderboard, spotted_leaderboard


def _post_leaderboard(client: WebClient, spotting_leaderboard: list, spotted_leaderboard: list) -> None:
    """Post leaderboard message to the spottings channel."""
    channel_id = SPOTTINGS_CHANNEL_ID
    if not channel_id:
        return
    blocks = _build_leaderboard_blocks(spotting_leaderboard, spotted_leaderboard)
    client.chat_postMessage(channel=channel_id, text="CTC Spottings Leaderboard", blocks=blocks)


def _seconds_until_next_run(target_hour: int, target_minute: int) -> float:
    """Seconds until next occurrence of target_hour:target_minute in Pacific."""
    now = datetime.now(TIMEZONE)
    target = now.replace(hour=target_hour, minute=target_minute, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def _nightly_scheduler_loop() -> None:
    """Background loop: run count at 11:59 PM, post leaderboard at 12 AM Pacific."""
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token or not SPOTTINGS_CHANNEL_ID:
        return

    client = WebClient(token=token)
    while True:
        # Sleep until 11:59 PM Pacific
        sleep_sec = _seconds_until_next_run(23, 59)
        time.sleep(sleep_sec)

        try:
            spotting_lb, spotted_lb = _run_nightly_count(client)
        except Exception:
            spotting_lb, spotted_lb = [], []

        # Sleep 60 seconds so leaderboard posts at 12:00 AM
        time.sleep(60)

        try:
            _post_leaderboard(client, spotting_lb, spotted_lb)
        except Exception:
            pass


def _build_edit_modal_blocks(default_type: str) -> list:
    """Build modal blocks for editing spotting/spotted counts."""
    type_options = [
        {"text": {"type": "plain_text", "text": "Spotting"}, "value": "spotting"},
        {"text": {"type": "plain_text", "text": "Spotted"}, "value": "spotted"},
    ]
    initial_type = next((o for o in type_options if o["value"] == default_type), type_options[0])

    return [
        {
            "type": "input",
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
            "type": "input",
            "block_id": "count_block",
            "element": {
                "type": "plain_text_input",
                "action_id": "spottings_count_input",
                "placeholder": {"type": "plain_text", "text": "Select user and type first"},
                "initial_value": "",
            },
            "label": {"type": "plain_text", "text": "New count"},
        },
    ]


def _build_edit_modal_view(blocks: list, callback_id: str, title: str) -> dict:
    """Build full modal view for edit form."""
    return {
        "type": "modal",
        "callback_id": callback_id,
        "title": {"type": "plain_text", "text": title},
        "submit": {"type": "plain_text", "text": "Save"},
        "blocks": blocks,
    }


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
            logger.error("Missing trigger_id in edit-spotting payload")
            return

        blocks = _build_edit_modal_blocks(default_type)
        view = _build_edit_modal_view(blocks, "spottings_edit_modal", title)
        client.views_open(trigger_id=trigger_id, view=view)

    @app.command("/edit-spotting")
    def cmd_edit_spotting(ack, body, client, logger):
        _open_edit_modal(ack, body, client, "spotting", "Edit Spotting Count", logger)

    @app.command("/edit-spotted")
    def cmd_edit_spotted(ack, body, client, logger):
        _open_edit_modal(ack, body, client, "spotted", "Edit Spotted Count", logger)

    @app.action("spottings_user_select")
    @app.action("spottings_type_select")
    def handle_spottings_select(ack, body, client):
        """When user selects a user or type, fetch count from Firebase and update modal."""
        ack()
        view = body.get("view")
        if not view:
            return

        values = view.get("state", {}).get("values", {})
        actions = body.get("actions", [])
        action = actions[0] if actions else {}

        # Get selected user: from action (just selected) or view state
        selected_user = action.get("selected_user")
        if not selected_user:
            user_block = values.get("user_block", {}).get("spottings_user_select", {})
            selected_user = user_block.get("selected_user")

        # Get selected type: from action or view state
        type_opt = action.get("selected_option") if action.get("type") == "static_select" else None
        if not type_opt:
            type_block = values.get("type_block", {}).get("spottings_type_select", {})
            type_opt = type_block.get("selected_option")
        count_type = type_opt.get("value") if type_opt else None

        if not selected_user or not count_type:
            return

        counts = _get_user_counts(selected_user)
        if count_type == "spotting":
            count_value = str(counts["spotting_count"])
        else:
            count_value = str(counts["spotted_count"])

        # Build updated view with new count and preserve selections
        type_options = [
            {"text": {"type": "plain_text", "text": "Spotting"}, "value": "spotting"},
            {"text": {"type": "plain_text", "text": "Spotted"}, "value": "spotted"},
        ]
        initial_type = next((o for o in type_options if o["value"] == count_type), type_options[0])

        blocks = [
            {
                "type": "input",
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
                "type": "input",
                "block_id": "count_block",
                "element": {
                    "type": "plain_text_input",
                    "action_id": "spottings_count_input",
                    "placeholder": {"type": "plain_text", "text": "Select user and type first"},
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

    # Start nightly scheduler
    t = threading.Thread(target=_nightly_scheduler_loop, daemon=True)
    t.start()

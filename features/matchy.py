"""
CTC Matchy: weekly pairing bot.

Slash command is MATCHY_COMMAND in matchy_core (currently /matchytest for testing).
Leaderboard reads matchyData/members in Firestore (matchyCount per weekly meetup).

Scheduled: Mondays 5:00 PM America/Los_Angeles.
"""
import os
import threading
import time
from datetime import datetime, timedelta

import pytz
from slack_sdk import WebClient

from features.matchy_core import (
    MATCHY_COMMAND,
    format_list_message,
    generate_matches,
    parse_override_user_ids,
    set_match_override,
    skip_next_week,
    toggle_pause,
    toggle_user_matchy,
)
from features.matchy_store import (
    build_leaderboard,
    get_member_matchy_count,
    recount_matchy_counts,
    set_member_matchy_count,
)

MATCHY_CHANNEL_ID = os.environ.get("MATCHY_CHANNEL_ID", "C01FL4VCE1Z")
MATCHY_TIMEZONE = pytz.timezone("America/Los_Angeles")

ADMIN_USER_IDS = frozenset({
    "U0631Q51G04",
    "U07T20PN1GB",
    "U063K9AG40Y",
    "U07T4JEEUSG",
})


def _build_leaderboard_blocks(rows: list[tuple[str, int, str]]) -> list:
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "Matchy Leaderboard", "emoji": True},
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": "*Most weekly meetups* _(from Firestore)_"}},
    ]
    if rows:
        lines = [f"{i + 1}. <@{uid}> — {count}" for i, (uid, count, _) in enumerate(rows[:10])]
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}})
    else:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "_No meetups counted yet!_"}})
    return blocks


def _help_text() -> str:
    c = MATCHY_COMMAND
    return (
        "*Matchy commands:*\n"
        f"• `{c}` — join or leave weekly Matchy meetups\n"
        f"• `{c} help` — show this message\n"
        f"• `{c} list` — member roster summary\n"
        f"• `{c} leaderboard` — meetup count leaderboard _(Firestore)_\n"
        f"• `{c} pause` — pause or resume scheduled generation _(admin)_\n"
        f"• `{c} skip` — skip the next scheduled run once _(admin)_\n"
        f"• `{c} count edit` — edit a user's meetup count _(admin)_\n"
        f"• `{c} count recount` — backfill counts from match history _(admin)_"
    )


# ---------------------------------------------------------------------------
# Schedulers
# ---------------------------------------------------------------------------

def _seconds_until_next_monday_5pm() -> float:
    now = datetime.now(MATCHY_TIMEZONE)
    days_ahead = (0 - now.weekday()) % 7
    target = (now + timedelta(days=days_ahead)).replace(
        hour=17, minute=0, second=0, microsecond=0
    )
    if days_ahead == 0 and now >= target:
        target += timedelta(days=7)
    return max((target - now).total_seconds(), 60.0)


def _weekly_matchy_scheduler_loop() -> None:
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        return
    client = WebClient(token=token)

    while True:
        time.sleep(_seconds_until_next_monday_5pm())
        try:
            def respond(msg: str) -> None:
                prefix = "🤖 *Automated Matchy Generation*\n\n"
                client.chat_postMessage(
                    channel=MATCHY_CHANNEL_ID,
                    text=prefix + msg,
                )

            generate_matches(client, respond)
        except Exception:
            pass
        time.sleep(60)


# ---------------------------------------------------------------------------
# Handler registration
# ---------------------------------------------------------------------------

def register_matchy_handlers(app):
    """Register matchy-related handlers with the Bolt app."""

    def _check_admin(user_id: str) -> bool:
        return user_id in ADMIN_USER_IDS

    def _ephemeral(client, channel_id, user_id, text, blocks=None):
        if not channel_id:
            return
        kwargs = {"channel": channel_id, "user": user_id, "text": text}
        if blocks:
            kwargs["blocks"] = blocks
        client.chat_postEphemeral(**kwargs)

    @app.command(MATCHY_COMMAND)
    def cmd_matchy(ack, body, client, logger):
        raw = (body.get("text") or "").strip()
        parts = raw.split(None, 1)
        sub = parts[0].lower() if parts else ""
        rest = parts[1] if len(parts) > 1 else ""
        user_id = body["user_id"]
        channel_id = body.get("channel_id", "")

        # Participation subcommands: {MATCHY_COMMAND} count ...
        if sub == "count":
            count_parts = rest.split(None, 1)
            count_sub = count_parts[0].lower() if count_parts else ""
            _handle_count_subcommand(
                ack, body, client, logger, count_sub, user_id, channel_id, _check_admin
            )
            return

        ack()

        if sub in ("", "join", "toggle"):
            msg = toggle_user_matchy(client, user_id)
            _ephemeral(client, channel_id, user_id, msg)
        elif sub == "list":
            _ephemeral(client, channel_id, user_id, format_list_message())
        elif sub == "sync":
            if not _check_admin(user_id):
                _ephemeral(client, channel_id, user_id, _help_text())
                return
            msg = set_match_override(parse_override_user_ids(rest, client))
            _ephemeral(client, channel_id, user_id, msg)
        elif sub == "leaderboard":
            try:
                rows = build_leaderboard()
                blocks = _build_leaderboard_blocks(rows)
                _ephemeral(
                    client, channel_id, user_id,
                    "Matchy Leaderboard",
                    blocks=blocks,
                )
            except Exception as e:
                logger.error("Matchy leaderboard failed: %s", e)
                _ephemeral(client, channel_id, user_id, f"Leaderboard failed: {e}")
        elif sub == "pause":
            if not _check_admin(user_id):
                _ephemeral(client, channel_id, user_id, "You don't have permission to use this command.")
                return
            _ephemeral(client, channel_id, user_id, toggle_pause())
        elif sub == "skip":
            if not _check_admin(user_id):
                _ephemeral(client, channel_id, user_id, "You don't have permission to use this command.")
                return
            _ephemeral(client, channel_id, user_id, skip_next_week())
        elif sub == "help":
            _ephemeral(client, channel_id, user_id, _help_text())
        elif sub == "generate":
            if not _check_admin(user_id):
                _ephemeral(client, channel_id, user_id, "You don't have permission to use this command.")
                return

            def _do_generate():
                try:
                    def respond(msg: str) -> None:
                        _ephemeral(client, channel_id, user_id, msg)

                    generate_matches(client, respond)
                except Exception as e:
                    logger.error("Matchy generate failed: %s", e)
                    _ephemeral(client, channel_id, user_id, f"Generation failed: {e}")

            threading.Thread(target=_do_generate, daemon=True).start()
        else:
            _ephemeral(client, channel_id, user_id, _help_text())

    def _handle_count_subcommand(ack, body, client, logger, count_sub, user_id, channel_id, check_admin):
        if count_sub == "edit":
            ack()
            if not check_admin(user_id):
                _ephemeral(client, channel_id, user_id, "You don't have permission to use this command.")
                return
            _open_count_edit_modal(body, client, logger)
            return

        ack()

        if count_sub == "recount":
            if not check_admin(user_id):
                _ephemeral(client, channel_id, user_id, "You don't have permission to use this command.")
                return

            def _do_recount():
                try:
                    rows = recount_matchy_counts()
                    top = ", ".join(f"<@{uid}>: {c}" for uid, c, _ in rows[:5]) or "none"
                    msg = f"Recount complete!\n*Top meetups:* {top}"
                except Exception as e:
                    logger.error("Matchy recount failed: %s", e)
                    msg = f"Recount failed: {e}"
                _ephemeral(client, channel_id, user_id, msg)

            threading.Thread(target=_do_recount, daemon=True).start()

        else:
            _ephemeral(
                client, channel_id, user_id,
                f"*Matchy count commands (admin):*\n"
                f"• `{MATCHY_COMMAND} count edit`\n"
                f"• `{MATCHY_COMMAND} count recount`",
            )

    def _open_count_edit_modal(body, client, logger):
        trigger_id = body.get("trigger_id")
        if not trigger_id:
            logger.error("Missing trigger_id in matchy edit payload")
            return
        client.views_open(trigger_id=trigger_id, view={
            "type": "modal",
            "callback_id": "matchy_edit_modal",
            "title": {"type": "plain_text", "text": "Edit Matchy Count"},
            "submit": {"type": "plain_text", "text": "Save"},
            "blocks": [
                {
                    "type": "input",
                    "dispatch_action": True,
                    "block_id": "user_block",
                    "element": {
                        "type": "users_select",
                        "action_id": "matchy_user_select",
                        "placeholder": {"type": "plain_text", "text": "Select a user"},
                    },
                    "label": {"type": "plain_text", "text": "User"},
                },
                {
                    "type": "section",
                    "block_id": "current_count_display",
                    "text": {"type": "mrkdwn", "text": "*Current count:* _Select a user above_"},
                },
                {
                    "type": "input",
                    "block_id": "count_block",
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "matchy_count_input",
                        "placeholder": {"type": "plain_text", "text": "Enter new count"},
                        "initial_value": "",
                    },
                    "label": {"type": "plain_text", "text": "New count"},
                },
            ],
        })

    @app.action("matchy_user_select")
    def handle_matchy_user_select(ack, body, client):
        ack()
        view = body.get("view")
        if not view:
            return
        actions = body.get("actions", [])
        action = actions[0] if actions else {}
        selected_user = action.get("selected_user")
        if not selected_user:
            return
        count_value = str(get_member_matchy_count(selected_user))
        client.views_update(
            view_id=view["id"],
            hash=view.get("hash"),
            view={
                "type": "modal",
                "callback_id": "matchy_edit_modal",
                "title": {"type": "plain_text", "text": "Edit Matchy Count"},
                "submit": {"type": "plain_text", "text": "Save"},
                "blocks": [
                    {
                        "type": "input",
                        "dispatch_action": True,
                        "block_id": "user_block",
                        "element": {
                            "type": "users_select",
                            "action_id": "matchy_user_select",
                            "placeholder": {"type": "plain_text", "text": "Select a user"},
                            "initial_user": selected_user,
                        },
                        "label": {"type": "plain_text", "text": "User"},
                    },
                    {
                        "type": "section",
                        "block_id": "current_count_display",
                        "text": {"type": "mrkdwn", "text": f"*Current count:* {count_value}"},
                    },
                    {
                        "type": "input",
                        "block_id": "count_block",
                        "element": {
                            "type": "plain_text_input",
                            "action_id": "matchy_count_input",
                            "placeholder": {"type": "plain_text", "text": "Enter new count"},
                            "initial_value": count_value,
                        },
                        "label": {"type": "plain_text", "text": "New count"},
                    },
                ],
            },
        )

    @app.view("matchy_edit_modal")
    def handle_matchy_edit_submit(ack, body, client, view):
        ack()
        user_id = body["user"]["id"]
        if not _check_admin(user_id):
            return
        values = view["state"]["values"]
        target_user = values.get("user_block", {}).get("matchy_user_select", {}).get("selected_user")
        count_raw = (values.get("count_block", {}).get("matchy_count_input", {}).get("value") or "").strip()
        if not target_user:
            return
        try:
            new_count = max(0, int(count_raw))
        except ValueError:
            new_count = 0
        if set_member_matchy_count(target_user, new_count):
            msg = f"Updated <@{target_user}>'s meetup count to {new_count}."
        else:
            msg = f"<@{target_user}> is not in the member roster. Add them with `{MATCHY_COMMAND}` first."
        channel_id = body.get("channel") or body.get("container", {}).get("channel_id")
        if channel_id:
            client.chat_postEphemeral(channel=channel_id, user=user_id, text=msg)

    threading.Thread(target=_weekly_matchy_scheduler_loop, daemon=True).start()

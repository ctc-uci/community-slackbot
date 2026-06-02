"""Slack Bolt handlers for Matchy."""
import threading
import time
from datetime import datetime, timedelta

import pytz
from slack_sdk import WebClient

from features.matchy.config import ADMIN_USER_IDS, CHANNEL_ID, COMMAND, TIMEZONE
from features.matchy.generation import run_generation
from features.matchy.participation import (
    get_count,
    register_events,
    run_full_recount,
    run_incremental,
    set_count,
    startup_catchup,
)
from features.matchy.roster import (
    format_roster_summary,
    parse_override_users,
    schedule_override,
    skip_next_run,
    toggle_opt_in,
    toggle_pause,
)


def _help_text() -> str:
    return (
        "*Matchy commands:*\n"
        f"• `{COMMAND}` — join or leave weekly Matchy meetups\n"
        f"• `{COMMAND} help` — show this message\n"
        f"• `{COMMAND} list` — member roster summary\n"
        f"• `{COMMAND} leaderboard` — verified channel participation\n"
        f"• `{COMMAND} pause` — pause or resume scheduled generation _(admin)_\n"
        f"• `{COMMAND} skip` — skip the next scheduled run once _(admin)_\n"
        f"• `{COMMAND} count edit` — edit participation count _(admin)_\n"
        f"• `{COMMAND} count recount` — recount from channel history _(admin)_"
    )


def _leaderboard_blocks(rows: list[tuple[str, int]]) -> list:
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "Matchy Participation Leaderboard", "emoji": True},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "*Verified activity* — top-level messages where you @ someone else "
                    "or someone @'s you (thread replies don't count)"
                ),
            },
        },
    ]
    if rows:
        body = "\n".join(f"{i + 1}. <@{uid}> — {n}" for i, (uid, n) in enumerate(rows[:10]))
    else:
        body = "_No participations yet!_"
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": body}})
    return blocks


def _seconds_until_monday_5pm() -> float:
    now = datetime.now(TIMEZONE)
    days = (0 - now.weekday()) % 7
    target = (now + timedelta(days=days)).replace(hour=17, minute=0, second=0, microsecond=0)
    if days == 0 and now >= target:
        target += timedelta(days=7)
    return max((target - now).total_seconds(), 60.0)


def _scheduler_loop() -> None:
    import os

    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    if not token:
        return
    client = WebClient(token=token)

    while True:
        time.sleep(_seconds_until_monday_5pm())
        try:
            def respond(msg: str) -> None:
                client.chat_postMessage(
                    channel=CHANNEL_ID,
                    text="🤖 *Automated Matchy Generation*\n\n" + msg,
                )

            run_generation(client, respond)
        except Exception:
            pass
        time.sleep(60)


def register_handlers(app) -> None:
    register_events(app)

    def is_admin(uid: str) -> bool:
        return uid in ADMIN_USER_IDS

    def ephemeral(client, channel_id, user_id, text, blocks=None):
        if not channel_id:
            return
        kw = {"channel": channel_id, "user": user_id, "text": text}
        if blocks:
            kw["blocks"] = blocks
        client.chat_postEphemeral(**kw)

    @app.command(COMMAND)
    def cmd(ack, body, client, logger):
        raw = (body.get("text") or "").strip()
        parts = raw.split(None, 1)
        sub = parts[0].lower() if parts else ""
        rest = parts[1] if len(parts) > 1 else ""
        user_id = body["user_id"]
        channel_id = body.get("channel_id", "")

        if sub == "count":
            count_sub = (rest.split(None, 1)[0].lower() if rest else "")
            if count_sub == "edit":
                ack()
                if not is_admin(user_id):
                    ephemeral(client, channel_id, user_id, "You don't have permission.")
                    return
                _open_edit_modal(body, client, logger)
                return
            ack()
            if count_sub == "recount":
                if not is_admin(user_id):
                    ephemeral(client, channel_id, user_id, "You don't have permission.")
                    return

                def recount():
                    try:
                        rows, note = run_full_recount(client)
                        top = ", ".join(f"<@{u}>: {c}" for u, c in rows[:5]) or "none"
                        msg = f"Recount complete!\n*Top participants:* {top}"
                        if note:
                            msg += f"\n\n{note}"
                    except Exception as e:
                        logger.error("Matchy recount failed: %s", e)
                        msg = f"Recount failed: {e}"
                    ephemeral(client, channel_id, user_id, msg)

                threading.Thread(target=recount, daemon=True).start()
            else:
                ephemeral(
                    client,
                    channel_id,
                    user_id,
                    f"*Matchy count (admin):*\n• `{COMMAND} count edit`\n• `{COMMAND} count recount`",
                )
            return

        ack()

        if sub in ("", "join", "toggle"):
            ephemeral(client, channel_id, user_id, toggle_opt_in(client, user_id))
        elif sub == "list":
            ephemeral(client, channel_id, user_id, format_roster_summary())
        elif sub == "sync":
            if not is_admin(user_id):
                ephemeral(client, channel_id, user_id, _help_text())
                return
            ephemeral(
                client, channel_id, user_id,
                schedule_override(parse_override_users(rest, client)),
            )
        elif sub == "leaderboard":
            def board():
                try:
                    rows = run_incremental(client)
                    ephemeral(
                        client,
                        channel_id,
                        user_id,
                        "Matchy Participation Leaderboard",
                        blocks=_leaderboard_blocks(rows),
                    )
                except Exception as e:
                    logger.error("Matchy leaderboard failed: %s", e)

            threading.Thread(target=board, daemon=True).start()
        elif sub == "pause":
            if not is_admin(user_id):
                ephemeral(client, channel_id, user_id, "You don't have permission.")
                return
            ephemeral(client, channel_id, user_id, toggle_pause())
        elif sub == "skip":
            if not is_admin(user_id):
                ephemeral(client, channel_id, user_id, "You don't have permission.")
                return
            ephemeral(client, channel_id, user_id, skip_next_run())
        elif sub == "help":
            ephemeral(client, channel_id, user_id, _help_text())
        elif sub == "generate":
            if not is_admin(user_id):
                ephemeral(client, channel_id, user_id, "You don't have permission.")
                return

            def gen():
                try:
                    run_generation(client, lambda m: ephemeral(client, channel_id, user_id, m))
                except Exception as e:
                    logger.error("Matchy generate failed: %s", e)
                    ephemeral(client, channel_id, user_id, f"Generation failed: {e}")

            threading.Thread(target=gen, daemon=True).start()
        else:
            ephemeral(client, channel_id, user_id, _help_text())

    def _open_edit_modal(body, client, logger):
        trigger = body.get("trigger_id")
        if not trigger:
            logger.error("Missing trigger_id for matchy edit modal")
            return
        client.views_open(
            trigger_id=trigger,
            view={
                "type": "modal",
                "callback_id": "matchy_edit_modal",
                "title": {"type": "plain_text", "text": "Edit Participation Count"},
                "submit": {"type": "plain_text", "text": "Save"},
                "blocks": _edit_modal_blocks(),
            },
        )

    def _edit_modal_blocks(selected=None, count=""):
        user_el = {
            "type": "users_select",
            "action_id": "matchy_user_select",
            "placeholder": {"type": "plain_text", "text": "Select a user"},
        }
        if selected:
            user_el["initial_user"] = selected
        count_text = (
            f"*Current count:* {count}"
            if selected
            else "*Current count:* _Select a user above_"
        )
        return [
            {
                "type": "input",
                "dispatch_action": True,
                "block_id": "user_block",
                "element": user_el,
                "label": {"type": "plain_text", "text": "User"},
            },
            {
                "type": "section",
                "block_id": "current_count_display",
                "text": {"type": "mrkdwn", "text": count_text},
            },
            {
                "type": "input",
                "block_id": "count_block",
                "element": {
                    "type": "plain_text_input",
                    "action_id": "matchy_count_input",
                    "placeholder": {"type": "plain_text", "text": "Enter new count"},
                    "initial_value": str(count) if selected else "",
                },
                "label": {"type": "plain_text", "text": "New count"},
            },
        ]

    @app.action("matchy_user_select")
    def on_user_select(ack, body, client):
        ack()
        view = body.get("view") or {}
        user = (body.get("actions") or [{}])[0].get("selected_user")
        if not user:
            return
        client.views_update(
            view_id=view["id"],
            hash=view.get("hash"),
            view={
                "type": "modal",
                "callback_id": "matchy_edit_modal",
                "title": {"type": "plain_text", "text": "Edit Participation Count"},
                "submit": {"type": "plain_text", "text": "Save"},
                "blocks": _edit_modal_blocks(user, get_count(user)),
            },
        )

    @app.view("matchy_edit_modal")
    def on_edit_submit(ack, body, client, view):
        ack()
        if not is_admin(body["user"]["id"]):
            return
        values = view["state"]["values"]
        target = values.get("user_block", {}).get("matchy_user_select", {}).get("selected_user")
        raw = (values.get("count_block", {}).get("matchy_count_input", {}).get("value") or "").strip()
        if not target:
            return
        try:
            n = max(0, int(raw))
        except ValueError:
            n = 0
        set_count(target, n)
        channel_id = body.get("channel") or body.get("container", {}).get("channel_id")
        if channel_id:
            client.chat_postEphemeral(
                channel=channel_id,
                user=body["user"]["id"],
                text=f"Updated <@{target}>'s verified participation count to {n}.",
            )

    threading.Thread(target=startup_catchup, daemon=True).start()
    threading.Thread(target=_scheduler_loop, daemon=True).start()

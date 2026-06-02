"""Run weekly Matchy generation and open Slack group DMs."""
import logging
from typing import Callable

from slack_sdk import WebClient

from features.matchy.config import WELCOME_MESSAGE
from features.matchy.matching import (
    assign_groups,
    combinations_mostly_exhausted,
    merge_stragglers,
    record_match_round,
    sanitize_groups,
    validate_groups,
)
from features.matchy.roster import enabled_member_ids
from features.matchy.store import load_roster, save_roster

logger = logging.getLogger(__name__)


def _name_for(slack_id: str, roster: list[dict]) -> str:
    for m in roster:
        if m.get("slackId") == slack_id:
            return m.get("name") or slack_id
    return slack_id


def run_generation(client: WebClient, respond: Callable[[str], None]) -> None:
    try:
        data = load_roster()

        if data.get("matchyPaused"):
            respond("⏸️ Matchy generation is currently paused.")
            return

        if data.get("skipNextMatchy"):
            data["skipNextMatchy"] = False
            save_roster(data)
            respond("⏸️ Matchy generation skipped this week.")
            return

        all_enabled = enabled_member_ids(data)
        if len(all_enabled) < 2:
            respond("❌ Not enough members enabled for Matchy! Need at least 2 people.")
            return

        history = data.get("previousMatches") or {}
        available = set(all_enabled)
        forced = []

        for group in data.get("nextMatchOverrides") or []:
            row = list(dict.fromkeys(u for u in group if u in available))
            if len(row) >= 2:
                forced.append(row)
                for uid in row:
                    available.discard(uid)

        auto = assign_groups([u for u in all_enabled if u in available], history)
        groups = sanitize_groups(forced + auto)
        groups, stragglers = merge_stragglers(groups)

        roster = data.get("members") or []
        check = validate_groups(groups, roster)
        if not check.ok:
            logger.error("[Matchy] validation failed: %s", check.message)
            respond(check.message)
            return

        viable = [g for g in groups if len(g) >= 2]
        allow_repeats = combinations_mostly_exhausted(all_enabled, history)

        created = []
        for group in viable:
            try:
                opened = client.conversations_open(users=",".join(group))
                if opened.get("ok"):
                    ch = opened["channel"]["id"]
                    names = [_name_for(uid, roster) for uid in group]
                    created.append(names)
                    client.chat_postMessage(channel=ch, text=WELCOME_MESSAGE)
            except Exception:
                logger.exception("Matchy group DM failed for %s", group)

        record_match_round(viable)

        data = load_roster()
        data["nextMatchOverrides"] = []
        save_roster(data)

        lines = ["🎯 *Matchy Meetups Created!*\n"]
        if allow_repeats:
            lines.append(
                "🔄 *Note: Allowing repeat matches — most unique pairings are used up.*\n"
            )
        for i, names in enumerate(created):
            lines.append(f"{i + 1}. *Group {i + 1}:* {', '.join(names)}")

        if stragglers:
            labels = ", ".join(_name_for(uid, roster) for uid in stragglers)
            lines.append(
                f"\n⚠️ *Could not place:* {labels}\n"
                "_No group chat created for them. Use a manual sync override next run._"
            )

        lines.append(
            f"\n*Summary:*\n"
            f"• {len(created)} group chats created\n"
            f"• {sum(len(g) for g in viable)} people matched\n"
            f"• Match history updated"
        )
        respond("\n".join(lines))
    except Exception:
        logger.exception("Matchy generation failed")
        respond("❌ Error generating matches. Check the logs for details.")

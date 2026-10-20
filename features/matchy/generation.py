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


def run_generation(
    client: WebClient,
    respond: Callable[[str], None],
    *,
    force: bool = False,
    notify: Callable[[str], None] | None = None,
) -> None:
    """Create the weekly Matchy groups.

    respond: receives the "Matchy Meetups Created" announcement.
    notify:  receives errors / skip notices (defaults to respond). Lets a manual run post
             the announcement publicly while keeping problems private to the admin.
    force:   manual run — ignore the pause and skip-next flags (without consuming them).
    """
    notify = notify or respond
    try:
        data = load_roster()

        if not force:
            if data.get("matchyPaused"):
                logger.info("Matchy generation skipped: paused")
                return

            if data.get("skipNextMatchy"):
                data["skipNextMatchy"] = False
                save_roster(data)
                logger.info("Matchy generation skipped: skip-next flag set")
                return

        all_enabled = enabled_member_ids(data)
        if len(all_enabled) < 2:
            notify("❌ Not enough members enabled for Matchy! Need at least 2 people.")
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
            notify(check.message)
            return

        viable = [g for g in groups if len(g) >= 2]
        allow_repeats = combinations_mostly_exhausted(all_enabled, history)

        created = []
        for group in viable:
            try:
                opened = client.conversations_open(users=",".join(group))
                if opened.get("ok"):
                    ch = opened["channel"]["id"]
                    created.append(list(group))
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
        for i, group in enumerate(created):
            tags = ", ".join(f"<@{uid}>" for uid in group)
            lines.append(f"{i + 1}. *Group {i + 1}:* {tags}")

        if stragglers:
            labels = ", ".join(f"<@{uid}>" for uid in stragglers)
            lines.append(
                f"\n⚠️ *Could not place:* {labels}\n"
                "_No group chat created for them. Use a manual sync override next run._"
            )

        respond("\n".join(lines))
    except Exception:
        logger.exception("Matchy generation failed")
        notify("❌ Error generating matches. Check the logs for details.")

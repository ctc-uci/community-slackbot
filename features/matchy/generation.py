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

    respond: receives the generic "new groups created, check your DMs" announcement.
             The actual pairings are only printed to the server logs.
    notify:  receives errors / skip notices (defaults to respond). Lets a manual run post
             the announcement publicly while keeping problems private to the admin.
    force:   manual run — ignore the pause and skip-next flags (without consuming them).
    """
    explicit_notify = notify is not None
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
            print(f"[Matchy] Generation aborted, validation failed:\n{check.message}", flush=True)
            # Details name people and groups, so only an admin running it manually sees them.
            notify(
                check.message
                if explicit_notify
                else "❌ Matchy generation aborted (validation error). Check the server logs."
            )
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

        # Full pairings go to the server logs only (stdout, so they show in Railway).
        # The channel just gets a generic announcement so groups aren't exposed.
        names = {m.get("slackId"): m.get("name") or m.get("slackId") for m in roster}

        def label(uid: str) -> str:
            return f"{names.get(uid, uid)} ({uid})"

        log_lines = [f"[Matchy] Generation complete: {len(created)} group(s) created"]
        if allow_repeats:
            log_lines.append("[Matchy] Note: allowing repeat matches (most unique pairings used up)")
        for i, group in enumerate(created):
            log_lines.append(f"[Matchy] Group {i + 1}: {', '.join(label(u) for u in group)}")
        if stragglers:
            log_lines.append(
                f"[Matchy] Could not place: {', '.join(label(u) for u in stragglers)} "
                "(no group chat created; use a sync override next run)"
            )
        print("\n".join(log_lines), flush=True)

        if stragglers and explicit_notify:
            # Manual run: tell the admin privately (never the channel) who was left out.
            notify(
                f"⚠️ Could not place: {', '.join(f'<@{u}>' for u in stragglers)}. "
                "No group chat created for them; use `/matchy sync` next run."
            )

        respond("🎯 *New Matchy groups created!* Check your DMs to meet your group.")
    except Exception:
        logger.exception("Matchy generation failed")
        notify("❌ Error generating matches. Check the logs for details.")

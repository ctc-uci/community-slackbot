"""Matchy roster: opt-in, admin controls, overrides."""
import logging

from slack_sdk import WebClient

from features.matchy.config import COMMAND
from features.matchy.store import load_roster, save_roster
from slack_users import resolve_users  # noqa: F401  (re-exported for handlers)

logger = logging.getLogger(__name__)


def enabled_member_ids(roster: dict) -> list[str]:
    return list(dict.fromkeys(
        m["slackId"]
        for m in (roster.get("members") or [])
        if m.get("slackId") and m.get("matchyEnabled")
    ))


def toggle_opt_in(client: WebClient, user_id: str) -> str:
    data = load_roster()
    members = data.setdefault("members", [])
    row = next((m for m in members if m.get("slackId") == user_id), None)

    if row:
        row["matchyEnabled"] = not row.get("matchyEnabled")
        save_roster(data)
        return (
            "✅ You've been removed from the Matchy system!"
            if not row["matchyEnabled"]
            else "✅ You've been added to the Matchy system!"
        )

    try:
        resp = client.users_info(user=user_id)
        user = (resp or {}).get("user") or {}
        if not resp.get("ok") or user.get("is_bot") or user.get("deleted"):
            return "❌ Cannot add this account to Matchy."

        profile = user.get("profile") or {}
        row = {
            "slackId": user_id,
            "name": user.get("real_name") or profile.get("display_name") or user.get("name") or "Unknown",
            "role": "MEMBER",
            "repos": [],
            "github": "",
            "rep": 0,
            "matchyEnabled": True,
        }
        members.append(row)
        save_roster(data)
        return (
            f"✅ You've been added to the Matchy system!\n\n"
            f"Your profile:\n• Name: {row['name']}\n• Matchy Enabled: Yes\n\n"
            f"You'll now be included in weekly Matchy meetups! 🎉"
        )
    except Exception:
        logger.exception("Matchy add member failed")
        return "❌ Error adding you to Matchy. Check the logs."


def format_roster_summary() -> str:
    data = load_roster()
    members = data.get("members") or []
    if not members:
        return "📊 **Member Data Loaded:**\n\n⚠️ **No members found in roster.**\n"

    enabled = sum(1 for m in members if m.get("matchyEnabled"))
    lines = [
        "📊 **Member Data Loaded:**\n",
        f"**Total Members:** {len(members)}",
        f"**Matchy Enabled:** {enabled}",
        f"**Matchy Disabled:** {len(members) - enabled}\n",
        "**Preview:**",
    ]
    for i, m in enumerate(members):
        flag = "✅" if m.get("matchyEnabled") else "❌"
        lines.append(
            f"{i + 1}. {m.get('name') or 'Unknown'} ({m.get('role') or 'Unknown'}) - {flag}"
        )
    history = data.get("previousMatches") or {}
    lines.append(f"\n**Previous Matches:** {len(history)} members have match history")
    return "\n".join(lines)


def parse_override_users(text: str, client: WebClient) -> list[str]:
    return resolve_users(text, client)[0]


def add_members(client: WebClient, user_ids: list[str], unresolved: list[str]) -> str:
    """Opt the given users in (idempotent, unlike toggle_opt_in)."""
    data = load_roster()
    members = data.setdefault("members", [])
    by_id = {m.get("slackId"): m for m in members}

    added, re_enabled, already, failed = [], [], [], []
    for uid in user_ids:
        row = by_id.get(uid)
        if row:
            if row.get("matchyEnabled"):
                already.append(uid)
            else:
                row["matchyEnabled"] = True
                re_enabled.append(uid)
            continue
        try:
            resp = client.users_info(user=uid)
            user = (resp or {}).get("user") or {}
            if not resp.get("ok") or user.get("is_bot") or user.get("deleted"):
                failed.append(uid)
                continue
            profile = user.get("profile") or {}
            row = {
                "slackId": uid,
                "name": user.get("real_name") or profile.get("display_name") or user.get("name") or "Unknown",
                "role": "MEMBER",
                "repos": [],
                "github": "",
                "rep": 0,
                "matchyEnabled": True,
            }
            members.append(row)
            by_id[uid] = row
            added.append(uid)
        except Exception:
            logger.exception("Matchy add member failed for %s", uid)
            failed.append(uid)

    if added or re_enabled:
        save_roster(data)

    def fmt(ids):
        return ", ".join(f"<@{u}>" for u in ids)

    lines = []
    if added:
        lines.append(f"✅ Added: {fmt(added)}")
    if re_enabled:
        lines.append(f"✅ Re-enabled: {fmt(re_enabled)}")
    if already:
        lines.append(f"ℹ️ Already enabled: {fmt(already)}")
    if failed:
        lines.append(f"❌ Could not add: {fmt(failed)}")
    if unresolved:
        lines.append(f"❓ Couldn't find a user for: {', '.join(unresolved)}")
    return "\n".join(lines) or f"❌ No users found. Usage: `{COMMAND} add @alice, @bob`"


def schedule_override(user_ids: list[str]) -> str:
    if len(user_ids) < 2 or len(user_ids) > 3:
        return "❌ Need 2–3 enabled members."

    data = load_roster()
    enabled = set(enabled_member_ids(data))
    bad = [u for u in user_ids if u not in enabled]
    if bad:
        return "❌ One or more users are not enabled for Matchy."

    overrides = data.get("nextMatchOverrides") or []
    overrides.append(user_ids)
    data["nextMatchOverrides"] = overrides
    save_roster(data)
    return "✅ Group scheduled for the next run."


def remove_members(user_ids: list[str], unresolved: list[str]) -> str:
    """Opt the given users out (idempotent, unlike toggle_opt_in)."""
    data = load_roster()
    by_id = {m.get("slackId"): m for m in data.get("members") or []}

    removed, already, not_found = [], [], []
    for uid in user_ids:
        row = by_id.get(uid)
        if not row:
            not_found.append(uid)
        elif row.get("matchyEnabled"):
            row["matchyEnabled"] = False
            removed.append(uid)
        else:
            already.append(uid)

    if removed:
        gone = set(removed)
        # drop pending sync groups that include anyone who just opted out
        data["nextMatchOverrides"] = [
            g for g in data.get("nextMatchOverrides") or [] if not gone.intersection(g)
        ]
        save_roster(data)

    def fmt(ids):
        return ", ".join(f"<@{u}>" for u in ids)

    lines = []
    if removed:
        lines.append(f"✅ Removed: {fmt(removed)}")
    if already:
        lines.append(f"ℹ️ Already opted out: {fmt(already)}")
    if not_found:
        lines.append(f"ℹ️ Not in the Matchy roster: {fmt(not_found)}")
    if unresolved:
        lines.append(f"❓ Couldn't find a user for: {', '.join(unresolved)}")
    return "\n".join(lines) or f"❌ No users found. Usage: `{COMMAND} remove @alice, @bob`"


def toggle_pause() -> str:
    data = load_roster()
    data["matchyPaused"] = not data.get("matchyPaused")
    save_roster(data)
    if data["matchyPaused"]:
        return f"⏸️ Matchy generation paused. Use `{COMMAND} pause` to resume."
    return "▶️ Matchy generation resumed."


def skip_next_run() -> str:
    data = load_roster()
    if data.get("matchyPaused"):
        return f"⏸️ Matchy is paused. Use `{COMMAND} pause` to resume first."
    data["skipNextMatchy"] = True
    save_roster(data)
    return "⏸️ Next scheduled Matchy run will be skipped."


def disable_all_members() -> str:
    data = load_roster()
    members = data.get("members") or []
    if not members:
        return "⚠️ No members in roster."

    was_on = sum(1 for m in members if m.get("matchyEnabled"))
    for m in members:
        m["matchyEnabled"] = False
    data["nextMatchOverrides"] = []
    save_roster(data)
    return (
        f"✅ Everyone opted out ({len(members)} members, {was_on} were enabled). "
        "Pending sync overrides cleared."
    )

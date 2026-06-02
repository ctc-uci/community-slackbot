"""Matchy roster: opt-in, admin controls, overrides."""
import logging
import re

from slack_sdk import WebClient

from features.matchy.config import COMMAND
from features.matchy.store import load_roster, save_roster

logger = logging.getLogger(__name__)

MENTION_RE = re.compile(r"<@(U[A-Z0-9]+)(?:\|[^>]*)?>", re.I)
HANDLE_RE = re.compile(r"^@?([A-Za-z0-9._-]+)$", re.I)


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
    ids = set()
    handles = []

    for token in text.split():
        m = MENTION_RE.match(token)
        if m:
            ids.add(m.group(1).upper())
            continue
        h = HANDLE_RE.match(token)
        if h:
            handles.append(h.group(1).lower())

    if handles:
        try:
            users = []
            cursor = None
            while True:
                kwargs = {"limit": 200}
                if cursor:
                    kwargs["cursor"] = cursor
                resp = client.users_list(**kwargs)
                users.extend(resp.get("members") or [])
                cursor = resp.get("response_metadata", {}).get("next_cursor")
                if not cursor:
                    break

            for handle in handles:
                for user in users:
                    if user.get("deleted") or user.get("is_bot"):
                        continue
                    profile = user.get("profile") or {}
                    names = [
                        user.get("name"),
                        profile.get("display_name"),
                        profile.get("display_name_normalized"),
                        profile.get("real_name"),
                        profile.get("real_name_normalized"),
                    ]
                    if handle in [n.lower() for n in names if n]:
                        ids.add(user["id"])
                        break
        except Exception:
            logger.exception("Matchy override user lookup failed")

    return list(ids)


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

"""
Matchy matching engine: weekly pairing, group DMs, and match history.
Ported from ctc-slackbot/utils/matchy-json.js.
"""
import logging
import random
import re
from typing import Callable

from slack_sdk import WebClient

from features.matchy_store import load_members_data, save_members_data

logger = logging.getLogger(__name__)

# Temporary test command; switch to /matchy for production.
MATCHY_COMMAND = "/matchytest"

MENTION_PATTERN = re.compile(r"<@(U[A-Z0-9]+)(?:\|[^>]*)?>", re.I)
HANDLE_PATTERN = re.compile(r"^@?([A-Za-z0-9._-]+)$", re.I)

WELCOME_MESSAGE = """🎯 *Welcome to your Matchy Meetup!*

This is your weekly match group! Feel free to introduce yourselves and plan your meetup. Have fun! 🎉

*💡 Activity Ideas:*
• ☕ Grab a sweet treat (Omomomo, Mori's, Heytea, etc.)
• 🍕 Grab food together (In-N-Out, Wadaya, Cava, Yintang, Burnt Crumbs, etc.)
• 🏓 Play sports at the ARC (Pickleball, Badminton, Volleyball, etc.)
• 🎮 Play League/Valorant together
• 🛍️ Go shopping (Irvine Spectrum, Thrifting/Bins, etc.)
• 🗺️ Explore a new place (Hiking Trail, Beach, etc.)
• 💬 Just chat and get to know each other!

*Remember:* The goal is to connect and build relationships. Keep it casual and fun! 🤓👍"""


def _name_for(slack_id: str, members: list) -> str:
    for m in members:
        if m.get("slackId") == slack_id:
            return m.get("name") or slack_id
    return slack_id


def _unique_preserve_order(ids: list) -> list:
    seen = set()
    out = []
    for uid in ids:
        if uid and uid not in seen:
            seen.add(uid)
            out.append(uid)
    return out


def sanitize_match_groups(matches: list) -> list:
    """One slack ID per group, at most one group per person."""
    cleaned = []
    used_global = set()
    for group in matches:
        if not isinstance(group, list):
            continue
        unique = _unique_preserve_order(group)
        filtered = [uid for uid in unique if uid not in used_global]
        for uid in filtered:
            used_global.add(uid)
        if len(filtered) >= 2:
            cleaned.append(filtered)
        elif len(filtered) == 1:
            cleaned.append(filtered)
    return cleaned


def valid_pairing(member1: str, member2: str, previous_matches: dict) -> bool:
    m1_prev = previous_matches.get(member1) or []
    m2_prev = previous_matches.get(member2) or []
    return member2 not in m1_prev and member1 not in m2_prev


def all_combinations_exhausted(members: list, previous_matches: dict) -> bool:
    total = len(members)
    if total < 2:
        return False
    max_pairs = total * (total - 1) / 2
    met_pairs = set()
    for member in members:
        for met in previous_matches.get(member) or []:
            if met in members:
                pair_key = f"{member}-{met}" if member < met else f"{met}-{member}"
                met_pairs.add(pair_key)
    return len(met_pairs) >= max_pairs * 0.8


def ensure_no_single_groups(matches: list, previous_matches: dict, allow_repeats: bool) -> list:
    result = []
    singles = []

    for match in matches:
        if len(match) == 1:
            singles.append(match[0])
        else:
            result.append(list(match))

    while singles:
        single = singles.pop(0)
        merged = False

        for group in result:
            if len(group) == 2:
                can_join = allow_repeats or all(
                    valid_pairing(m, single, previous_matches) for m in group
                )
                if can_join and single not in group:
                    group.append(single)
                    merged = True
                    break

        if not merged and singles:
            another = singles.pop(0)
            if single == another:
                singles.insert(0, another)
            elif allow_repeats or valid_pairing(single, another, previous_matches):
                result.append([single, another])
                merged = True
            else:
                singles.insert(0, another)
                for i, group in enumerate(result):
                    if len(group) == 3 and single not in group and another not in group:
                        for j in range(3):
                            m1, m2, m3 = group[j], group[(j + 1) % 3], group[(j + 2) % 3]
                            if (
                                (allow_repeats or valid_pairing(single, m1, previous_matches))
                                and (allow_repeats or valid_pairing(another, m2, previous_matches))
                            ):
                                result[i] = [m1, single]
                                result.append([m2, another])
                                singles.insert(0, m3)
                                merged = True
                                break
                        if merged:
                            break

        if not merged and allow_repeats:
            for group in result:
                if len(group) < 3 and single not in group:
                    group.append(single)
                    merged = True
                    break

        if not merged:
            largest_idx = max(range(len(result)), key=lambda i: len(result[i]), default=-1)
            if largest_idx >= 0 and len(result[largest_idx]) >= 2:
                group = result[largest_idx]
                if single not in group:
                    result[largest_idx] = [group[0], single]
                    if len(group) > 2:
                        result.append([group[1], *group[2:]])
                    else:
                        result.append([group[1]])
                    merged = True

        if not merged and result and single not in result[0]:
            result[0].append(single)
            merged = True
        elif not merged:
            result.append([single])

    final = []
    remaining = []
    for match in result:
        if len(match) == 1:
            remaining.append(match[0])
        else:
            final.append(match)

    while remaining:
        single = remaining.pop(0)
        merged = False
        for group in final:
            if len(group) == 2 and single not in group:
                group.append(single)
                merged = True
                break
        if not merged and final and single not in final[0]:
            final[0].append(single)
        elif not merged and remaining:
            final.append([single, remaining.pop(0)])
        elif not merged:
            final.append([single])

    return sanitize_match_groups(final)


def get_matches(members: list, previous_matches: dict) -> list:
    members = list(dict.fromkeys(members))
    matches = []
    used = set()
    allow_repeats = all_combinations_exhausted(members, previous_matches)
    shuffled = list(members)
    random.shuffle(shuffled)

    i = 0
    while i < len(shuffled):
        if shuffled[i] in used:
            i += 1
            continue

        current = [shuffled[i]]
        used.add(shuffled[i])
        remaining_after = len(shuffled) - len(used)

        if remaining_after == 0:
            group_size = random.choice([2, 3])
        elif remaining_after == 1:
            group_size = 3
        else:
            group_size = 3 if random.random() < 0.6 and remaining_after >= 2 else 2

        attempts = 0
        while len(current) < group_size and attempts < 50:
            candidate = random.choice(shuffled)
            if candidate not in used:
                can_join = allow_repeats or all(
                    valid_pairing(m, candidate, previous_matches) for m in current
                )
                if can_join and candidate not in current:
                    current.append(candidate)
                    used.add(candidate)
            attempts += 1

        matches.append(_unique_preserve_order(current))
        i += 1

    return ensure_no_single_groups(matches, previous_matches, allow_repeats)


def validate_match_assignments(matches: list, members_data: dict) -> dict:
    roster = members_data.get("members") or []
    slack_to_groups: dict[str, list[int]] = {}

    for g, group in enumerate(matches):
        if not isinstance(group, list):
            continue
        seen = set()
        for slack_id in group:
            if not slack_id:
                continue
            if slack_id in seen:
                return {
                    "ok": False,
                    "message": (
                        f"❌ *Matchy generation aborted:* duplicate user in a single group.\n\n"
                        f"• *{_name_for(slack_id, roster)}* (`{slack_id}`) appears twice in group {g + 1}\n\n"
                        "_No group chats were created. Previous matches were not updated._"
                    ),
                }
            seen.add(slack_id)
            slack_to_groups.setdefault(slack_id, []).append(g)

    cross = [(sid, idxs) for sid, idxs in slack_to_groups.items() if len(idxs) > 1]
    if cross:
        lines = [
            f"• *{_name_for(sid, roster)}* (`{sid}`): groups {', '.join(str(i + 1) for i in idxs)}"
            for sid, idxs in cross
        ]
        return {
            "ok": False,
            "message": (
                f"❌ *Matchy generation aborted:* {len(cross)} user(s) would be in more than one group:\n\n"
                + "\n".join(lines)
                + "\n\n_No group chats were created. Previous matches were not updated._"
            ),
        }
    return {"ok": True}


def _increment_matchy_counts_for_round(matches: list, members: list) -> None:
    """+1 matchyCount per member placed in a group this generation."""
    by_id = {m["slackId"]: m for m in members if m.get("slackId")}
    for group in matches:
        for slack_id in group:
            member = by_id.get(slack_id)
            if not member:
                continue
            current = member.get("matchyCount")
            if current is None:
                current = member.get("rep") or 0
            member["matchyCount"] = int(current) + 1


def update_previous_matches(current_matches: list, previous_matches: dict, members: list) -> None:
    for match in current_matches:
        for i in range(len(match)):
            for j in range(i + 1, len(match)):
                u1, u2 = match[i], match[j]
                previous_matches.setdefault(u1, [])
                previous_matches.setdefault(u2, [])
                if u2 not in previous_matches[u1]:
                    previous_matches[u1].append(u2)
                if u1 not in previous_matches[u2]:
                    previous_matches[u2].append(u1)

    _increment_matchy_counts_for_round(current_matches, members)

    data = load_members_data()
    data["previousMatches"] = previous_matches
    data["members"] = members
    save_members_data(data)


def create_group_chats(
    client: WebClient,
    matches: list,
    members_data: dict,
    respond: Callable[[str], None],
    allow_repeats: bool,
) -> bool:
    matches = sanitize_match_groups(matches)
    validation = validate_match_assignments(matches, members_data)
    if not validation["ok"]:
        logger.error("[Matchy] validateMatchAssignments failed: %s", validation["message"])
        respond(validation["message"])
        return False

    data = load_members_data()
    previous_matches = dict(data.get("previousMatches") or {})
    members = data.get("members") or []
    update_previous_matches(matches, previous_matches, members)

    created_groups = []
    for i, match in enumerate(matches):
        try:
            result = client.conversations_open(users=",".join(match))
            if result.get("ok"):
                channel_id = result["channel"]["id"]
                member_names = [_name_for(sid, members_data.get("members") or []) for sid in match]
                created_groups.append({"channel_id": channel_id, "members": match, "member_names": member_names})
                client.chat_postMessage(channel=channel_id, text=WELCOME_MESSAGE)
        except Exception as e:
            logger.error("Error creating group for match %s: %s", i + 1, e)

    output = "🎯 *Matchy Meetups Created!*\n\n"
    if allow_repeats:
        output += "🔄 *Note: Allowing repeat matches as most unique combinations have been exhausted*\n\n"
    for idx, group in enumerate(created_groups):
        output += f"{idx + 1}. *Group {idx + 1}:* {', '.join(group['member_names'])}\n"
    total_people = sum(len(m) for m in matches)
    output += (
        f"\n*Summary:*\n"
        f"• {len(created_groups)} group chats created\n"
        f"• {total_people} people matched\n"
        f"• Previous matches updated\n"
        f"• Welcome messages sent to each group\n"
    )
    respond(output)
    return True


def generate_matches(client: WebClient, respond: Callable[[str], None]) -> None:
    try:
        members_data = load_members_data()

        if members_data.get("matchyPaused"):
            respond("⏸️ Matchy generation is currently paused.")
            return

        if members_data.get("skipNextMatchy"):
            members_data["skipNextMatchy"] = False
            save_members_data(members_data)
            respond("⏸️ Matchy generation skipped this week.")
            return

        members = list(dict.fromkeys(
            m["slackId"]
            for m in (members_data.get("members") or [])
            if m.get("slackId") and m.get("matchyEnabled")
        ))
        previous_matches = members_data.get("previousMatches") or {}
        overrides = members_data.get("nextMatchOverrides") or []

        forced = []
        available = set(members)
        for group in overrides:
            if not isinstance(group, list):
                continue
            unique = list(dict.fromkeys(g for g in group if g in available))
            if len(unique) >= 2:
                forced.append(unique)
                for uid in unique:
                    available.discard(uid)

        available_members = [m for m in members if m in available]

        if len(members) < 2:
            respond("❌ Not enough members enabled for Matchy! Need at least 2 people.")
            return

        current = get_matches(available_members, previous_matches)
        all_matches = sanitize_match_groups(forced + current)
        allow_repeats = all_combinations_exhausted(members, previous_matches)

        if not create_group_chats(client, all_matches, members_data, respond, allow_repeats):
            return

        latest = load_members_data()
        latest["nextMatchOverrides"] = []
        save_members_data(latest)
    except Exception as e:
        logger.exception("Error in generate_matches")
        respond("❌ Error generating matches. Check the logs for details.")


def parse_override_user_ids(text: str, client: WebClient) -> list:
    user_ids = set()
    pending_handles = []

    for token in text.split():
        if not token:
            continue
        mention = MENTION_PATTERN.match(token)
        if mention:
            user_ids.add(mention.group(1).upper())
            continue
        handle = HANDLE_PATTERN.match(token)
        if handle:
            pending_handles.append(handle.group(1).lower())

    if pending_handles:
        try:
            cursor = None
            all_users = []
            while True:
                kwargs = {"limit": 200}
                if cursor:
                    kwargs["cursor"] = cursor
                resp = client.users_list(**kwargs)
                all_users.extend(resp.get("members") or [])
                cursor = resp.get("response_metadata", {}).get("next_cursor")
                if not cursor:
                    break

            for handle in pending_handles:
                for user in all_users:
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
                    candidates = [n.lower() for n in names if n]
                    if handle in candidates:
                        user_ids.add(user["id"])
                        break
        except Exception as e:
            logger.error("Error looking up Slack users: %s", e)

    return list(user_ids)


def toggle_user_matchy(client: WebClient, user_id: str) -> str:
    members_data = load_members_data()
    existing = next((m for m in members_data["members"] if m.get("slackId") == user_id), None)

    if existing:
        if existing.get("matchyEnabled"):
            existing["matchyEnabled"] = False
            save_members_data(members_data)
            return "✅ You've been removed from the Matchy system!"
        existing["matchyEnabled"] = True
        save_members_data(members_data)
        return "✅ You've been added to the Matchy system!"

    try:
        result = client.users_info(user=user_id)
        if not result.get("ok") or not result.get("user"):
            return "❌ Error fetching your user info. Please try again."
        user = result["user"]
        if user.get("is_bot") or user.get("deleted"):
            return "❌ Bots cannot be added to Matchy."

        profile = user.get("profile") or {}
        new_member = {
            "slackId": user["id"],
            "name": user.get("real_name") or profile.get("display_name") or user.get("name") or "Unknown",
            "role": "MEMBER",
            "repos": [],
            "github": "",
            "rep": 0,
            "matchyEnabled": True,
        }
        members_data["members"].append(new_member)
        save_members_data(members_data)
        return (
            f"✅ You've been added to the Matchy system!\n\n"
            f"Your profile:\n• Name: {new_member['name']}\n• Matchy Enabled: Yes\n\n"
            f"You'll now be included in weekly matchy meetups! 🎉"
        )
    except Exception:
        logger.exception("Error adding user to Matchy")
        return "❌ Error adding you to Matchy. Check the logs for details."


def format_list_message() -> str:
    data = load_members_data()
    members = data.get("members") or []
    previous_matches = data.get("previousMatches") or {}

    if not members:
        return (
            "📊 **Member Data Loaded:**\n\n"
            "⚠️ **No members found in data file!**\n"
            "The members document appears to be empty.\n"
        )

    enabled = sum(1 for m in members if m.get("matchyEnabled"))
    output = "📊 **Member Data Loaded:**\n\n"
    output += f"**Total Members:** {len(members)}\n"
    output += f"**Matchy Enabled:** {enabled}\n"
    output += f"**Matchy Disabled:** {len(members) - enabled}\n\n"
    output += "**Preview:**\n"
    for i, member in enumerate(members):
        flag = "✅" if member.get("matchyEnabled") else "❌"
        output += f"{i + 1}. {member.get('name') or 'Unknown'} ({member.get('role') or 'Unknown'}) - {flag}\n"
    output += f"\n**Previous Matches:** {len(previous_matches)} members have match history\n"
    return output


def set_match_override(user_ids: list) -> str:
    if len(user_ids) < 2 or len(user_ids) > 3:
        return "❌ Need 2–3 enabled members."

    data = load_members_data()
    enabled = {
        m["slackId"]
        for m in (data.get("members") or [])
        if m.get("matchyEnabled")
    }
    unavailable = [uid for uid in user_ids if uid not in enabled]
    if unavailable:
        return "❌ One or more users are not enabled for Matchy."

    overrides = data.get("nextMatchOverrides") or []
    overrides.append(user_ids)
    data["nextMatchOverrides"] = overrides
    save_members_data(data)
    return "✅ Group scheduled for the next run."


def toggle_pause() -> str:
    data = load_members_data()
    data["matchyPaused"] = not data.get("matchyPaused")
    save_members_data(data)
    if data["matchyPaused"]:
        return f"⏸️ Matchy generation has been paused. It will remain paused until you toggle it back on with `{MATCHY_COMMAND} pause`."
    return "▶️ Matchy generation has been resumed. Scheduled runs will now proceed normally."


def skip_next_week() -> str:
    data = load_members_data()
    if data.get("matchyPaused"):
        return f"⏸️ Matchy generation is already paused. Use `{MATCHY_COMMAND} pause` to toggle it back on first."
    data["skipNextMatchy"] = True
    save_members_data(data)
    return "⏸️ Matchy generation will be skipped for the next scheduled run."

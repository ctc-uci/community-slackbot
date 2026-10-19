"""Shared helper: turn "@mentions / handles" typed in a slash command into Slack IDs."""
import logging
import re

from slack_sdk import WebClient

logger = logging.getLogger(__name__)

MENTION_RE = re.compile(r"<@(U[A-Z0-9]+)(?:\|[^>]*)?>", re.I)
HANDLE_RE = re.compile(r"^@?([A-Za-z0-9._-]+)$", re.I)


def resolve_users(text: str, client: WebClient) -> tuple[list[str], list[str]]:
    """Resolve @mentions / handles in text to Slack IDs.

    Commas and spaces both separate names. Order is preserved and duplicates removed.
    Returns (ids, unresolved) where unresolved are tokens that matched no user.
    """
    tokens: list[tuple[str, str]] = []  # ("id", UID) or ("handle", name)
    unresolved: list[str] = []

    for token in (text or "").replace(",", " ").split():
        m = MENTION_RE.match(token)
        if m:
            tokens.append(("id", m.group(1).upper()))
            continue
        h = HANDLE_RE.match(token)
        if h:
            tokens.append(("handle", h.group(1).lower()))
        else:
            unresolved.append(token)

    handles = {val for kind, val in tokens if kind == "handle"}
    by_handle: dict[str, str] = {}
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
                        by_handle[handle] = user["id"]
                        break
        except Exception:
            logger.exception("User lookup failed")

    ids: list[str] = []
    for kind, val in tokens:
        uid = val if kind == "id" else by_handle.get(val)
        if uid is None:
            unresolved.append(f"@{val}")
        elif uid not in ids:
            ids.append(uid)
    return ids, unresolved

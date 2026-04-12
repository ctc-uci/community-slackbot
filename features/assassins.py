"""Water Assassins game: target assignment, kill reporting, GM validation, leaderboard."""
import os
import random
import threading
import time
import uuid
from datetime import datetime

import pytz
from firebase_admin import firestore
from firebase_client import get_firebase_app
from slack_sdk import WebClient

ASSASSIN_CHANNEL_ID = "C0ASVT9J3S4"
TIMEZONE = pytz.timezone("America/Los_Angeles")

COL_ROUNDS = "assassin_rounds"
COL_PLAYERS = "assassin_players"
COL_REPORTS = "assassin_kill_reports"

# ---------------------------------------------------------------------------
# In-memory state (restored from Firestore on startup)
# ---------------------------------------------------------------------------

_game_state = {
    "status": "none",   # "none" | "pending" | "active" | "ended"
    "round_id": None,
    "gm_id": None,
    "start_ts": None,
}
_state_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Firestore helpers
# ---------------------------------------------------------------------------

def _db():
    get_firebase_app()
    return firestore.client()


def _get_round():
    doc = _db().collection(COL_ROUNDS).document("current").get()
    return doc.to_dict() if doc.exists else None


def _get_player(user_id):
    doc = _db().collection(COL_PLAYERS).document(user_id).get()
    return doc.to_dict() if doc.exists else None


def _get_players_for_round(round_id):
    return [
        doc.to_dict()
        for doc in _db().collection(COL_PLAYERS).where("round_id", "==", round_id).stream()
    ]


def _get_alive_players(round_id):
    return [
        doc.to_dict()
        for doc in _db()
        .collection(COL_PLAYERS)
        .where("round_id", "==", round_id)
        .where("status", "==", "alive")
        .stream()
    ]


def _get_pending_report_for_reporter(reporter_id, round_id):
    docs = list(
        _db()
        .collection(COL_REPORTS)
        .where("reporter_id", "==", reporter_id)
        .where("round_id", "==", round_id)
        .where("status", "==", "pending")
        .stream()
    )
    return docs[0].to_dict() if docs else None


def _get_report(report_id):
    doc = _db().collection(COL_REPORTS).document(report_id).get()
    return doc.to_dict() if doc.exists else None


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

def _restore_state_from_firestore():
    """Load current round from Firestore into _game_state on startup."""
    with _state_lock:
        try:
            rnd = _get_round()
            if rnd and rnd.get("status") in ("pending", "active"):
                _game_state["status"] = rnd["status"]
                _game_state["round_id"] = rnd.get("round_id")
                _game_state["gm_id"] = rnd.get("gm_id")
                _game_state["start_ts"] = rnd.get("start_ts")
            elif rnd and rnd.get("status") == "ended":
                _game_state["status"] = "ended"
            else:
                _game_state["status"] = "none"
        except Exception as e:
            print(f"[Assassins] Failed to restore state: {e}")


def _set_state(status, round_id=None, gm_id=None, start_ts=None):
    with _state_lock:
        _game_state["status"] = status
        if round_id is not None:
            _game_state["round_id"] = round_id
        if gm_id is not None:
            _game_state["gm_id"] = gm_id
        if start_ts is not None:
            _game_state["start_ts"] = start_ts


def _get_state():
    with _state_lock:
        return dict(_game_state)


# ---------------------------------------------------------------------------
# DM helper
# ---------------------------------------------------------------------------

def _dm(client, user_id, text=None, blocks=None):
    dm = client.conversations_open(users=[user_id])
    channel = dm["channel"]["id"]
    kwargs = {"channel": channel}
    if text:
        kwargs["text"] = text
    if blocks:
        kwargs["blocks"] = blocks
        if not text:
            kwargs["text"] = "Water Assassins update"
    return client.chat_postMessage(**kwargs)


# ---------------------------------------------------------------------------
# Target assignment
# ---------------------------------------------------------------------------

def _assign_targets(client):
    state = _get_state()
    round_id = state["round_id"]
    players = _get_players_for_round(round_id)

    if len(players) < 2:
        client.chat_postMessage(
            channel=ASSASSIN_CHANNEL_ID,
            text="Not enough players to start the round (minimum 2). Waiting for more to join.",
        )
        return

    player_ids = [p["user_id"] for p in players]
    random.shuffle(player_ids)
    n = len(player_ids)

    db = _db()
    batch = db.batch()

    # Assign targets in a cycle
    for i, uid in enumerate(player_ids):
        target_id = player_ids[(i + 1) % n]
        ref = db.collection(COL_PLAYERS).document(uid)
        batch.set(ref, {
            "status": "alive",
            "target_id": target_id,
            "assigned_ts": time.time(),
        }, merge=True)

    # Update round
    round_ref = db.collection(COL_ROUNDS).document("current")
    batch.set(round_ref, {
        "status": "active",
        "start_ts": time.time(),
        "player_order": player_ids,
    }, merge=True)

    batch.commit()
    _set_state("active", start_ts=time.time())

    # DM each player their target
    for i, uid in enumerate(player_ids):
        target_id = player_ids[(i + 1) % n]
        try:
            _dm(client, uid, blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            "*Water Assassins has begun!* :droplet::dagger_knife:\n\n"
                            f"Your target is *<@{target_id}>*.\n\n"
                            "Eliminate them and report your kill with `/assassin report`. "
                            "You must secure at least one kill by end of round to survive.\n\n"
                            "Good luck. Stay sharp."
                        ),
                    },
                }
            ])
        except Exception as e:
            print(f"[Assassins] Failed to DM {uid}: {e}")

    client.chat_postMessage(
        channel=ASSASSIN_CHANNEL_ID,
        text=f"The round has begun! {n} players are hunting. Targets have been assigned via DM. May the best hunter win. :droplet:",
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f":droplet: *Water Assassins — Round Started!*\n\n"
                        f"{n} players are in the hunt. Targets have been assigned via DM.\n\n"
                        "Report eliminations with `/assassin report`. "
                        "May the best hunter win!"
                    ),
                },
            }
        ],
    )


# ---------------------------------------------------------------------------
# Round end
# ---------------------------------------------------------------------------

def _end_round(client, reason="gm_ended"):
    state = _get_state()
    round_id = state["round_id"]

    all_players = _get_players_for_round(round_id)
    alive = [p for p in all_players if p["status"] == "alive"]
    zero_kill_alive = [p for p in alive if p.get("kills", 0) == 0]
    survivors = [p for p in alive if p.get("kills", 0) > 0]

    db = _db()
    batch = db.batch()

    # Eliminate alive players with 0 kills
    for p in zero_kill_alive:
        ref = db.collection(COL_PLAYERS).document(p["user_id"])
        batch.set(ref, {"status": "eliminated"}, merge=True)

    # Mark round ended
    round_ref = db.collection(COL_ROUNDS).document("current")
    batch.set(round_ref, {"status": "ended", "end_ts": time.time()}, merge=True)
    batch.commit()

    _set_state("ended")

    # Notify zero-kill eliminated players
    for p in zero_kill_alive:
        try:
            _dm(client, p["user_id"],
                text="The round has ended. You were eliminated for not securing a kill. Better luck next time!")
        except Exception:
            pass

    # Build leaderboard (reload for accurate post-batch kill counts)
    all_players_fresh = _get_players_for_round(round_id)
    sorted_players = sorted(all_players_fresh, key=lambda p: p.get("kills", 0), reverse=True)

    medals = ["🥇", "🥈", "🥉"]
    leaderboard_lines = []
    rank = 0
    for p in sorted_players:
        if p.get("kills", 0) == 0:
            continue
        medal = medals[rank] if rank < 3 else f"{rank + 1}."
        leaderboard_lines.append(f"{medal} <@{p['user_id']}> — {p.get('kills', 0)} kill(s)")
        rank += 1

    if not leaderboard_lines:
        leaderboard_text = "No kills were recorded this round."
    else:
        leaderboard_text = "\n".join(leaderboard_lines)

    # Winner line
    if reason == "last_standing" and survivors:
        winner_uid = survivors[0]["user_id"] if len(survivors) == 1 else None
        # Find the last alive player
        last_alive = [p for p in all_players_fresh if p.get("status") == "alive"]
        if last_alive:
            winner_uid = last_alive[0]["user_id"]
        winner_line = f"🏆 *Winner: <@{winner_uid}>* — last hunter standing!" if winner_uid else "🏆 *Round over!*"
    else:
        alive_ids = [p["user_id"] for p in survivors]
        if alive_ids:
            winner_line = "Survivors: " + ", ".join(f"<@{uid}>" for uid in alive_ids)
        else:
            winner_line = "No survivors remain."

    zero_kill_ids = [p["user_id"] for p in zero_kill_alive]
    eliminated_line = ""
    if zero_kill_ids:
        eliminated_line = "\n\n*Eliminated (no kills):* " + ", ".join(f"<@{uid}>" for uid in zero_kill_ids)

    client.chat_postMessage(
        channel=ASSASSIN_CHANNEL_ID,
        text="The Water Assassins round has ended!",
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        ":droplet: *Water Assassins — Round Over!*\n\n"
                        f"{winner_line}"
                        f"{eliminated_line}"
                    ),
                },
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Kill Leaderboard:*\n{leaderboard_text}",
                },
            },
        ],
    )


# ---------------------------------------------------------------------------
# Background thread
# ---------------------------------------------------------------------------

def _tick(client):
    state = _get_state()

    if state["status"] == "pending" and state.get("start_ts"):
        if time.time() >= state["start_ts"]:
            print("[Assassins] 8am trigger hit — assigning targets")
            _assign_targets(client)


def _assassins_bg_loop(client):
    while True:
        try:
            _tick(client)
        except Exception as e:
            print(f"[Assassins] Background loop error: {e}")
        time.sleep(60)


# ---------------------------------------------------------------------------
# Handler helpers
# ---------------------------------------------------------------------------

def _ephemeral(respond, text):
    respond({"response_type": "ephemeral", "text": text})


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def _handle_join(body, client, respond):
    user_id = body["user_id"]
    state = _get_state()

    if state["status"] not in ("pending",):
        if state["status"] == "none":
            _ephemeral(respond, "No game is accepting players right now. Wait for a GM to run `/assassin start <YYYY-MM-DD>`.")
        elif state["status"] == "active":
            _ephemeral(respond, "The round is already in progress. You can join the next one.")
        else:
            _ephemeral(respond, "No active game right now.")
        return

    rnd = _get_round()
    round_id = rnd["round_id"]

    existing = _get_player(user_id)
    if existing and existing.get("round_id") == round_id:
        _ephemeral(respond, "You've already joined this round!")
        return

    _db().collection(COL_PLAYERS).document(user_id).set({
        "user_id": user_id,
        "round_id": round_id,
        "status": "pending",
        "target_id": None,
        "kills": 0,
        "killed_by": None,
        "joined_ts": time.time(),
        "assigned_ts": None,
    })

    # Count players
    players = _get_players_for_round(round_id)
    n = len(players)

    start_date = rnd.get("start_date", "TBD")
    _ephemeral(respond, f"You've joined the Water Assassins game! :droplet: Targets will be assigned at 8am on {start_date}.")

    client.chat_postMessage(
        channel=ASSASSIN_CHANNEL_ID,
        text=f"<@{user_id}> has joined the Water Assassins game! ({n} player(s) so far)",
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f":droplet: <@{user_id}> has joined the hunt! *{n} player(s)* registered so far.\nJoin with `/assassin join`.",
                },
            }
        ],
    )


def _handle_start(body, client, respond):
    user_id = body["user_id"]
    text = (body.get("text") or "").strip()
    parts = text.split()
    # parts[0] is "start", parts[1] should be date
    date_str = parts[1] if len(parts) > 1 else ""

    if not date_str:
        _ephemeral(respond, "Usage: `/assassin start YYYY-MM-DD`")
        return

    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        _ephemeral(respond, f"Invalid date format `{date_str}`. Use YYYY-MM-DD (e.g. `2025-05-01`).")
        return

    # Compute 8am Pacific timestamp
    start_naive = dt.replace(hour=8, minute=0, second=0, microsecond=0)
    start_aware = TIMEZONE.localize(start_naive)
    start_ts = start_aware.timestamp()

    if start_ts < time.time() - 3600:
        _ephemeral(respond, f"The date {date_str} is in the past. Please choose a future date.")
        return

    state = _get_state()

    # Allow GM to update start date if they already own a pending round
    if state["status"] == "pending" and state.get("gm_id") != user_id:
        _ephemeral(respond, "A round is already pending. Only the current GM can update it.")
        return
    if state["status"] == "active":
        _ephemeral(respond, "A round is already in progress. End it first with `/assassin end`.")
        return

    round_id = str(uuid.uuid4())

    _db().collection(COL_ROUNDS).document("current").set({
        "round_id": round_id,
        "status": "pending",
        "gm_id": user_id,
        "start_date": date_str,
        "start_ts": start_ts,
        "end_ts": None,
        "created_ts": time.time(),
        "player_order": [],
    })

    _set_state("pending", round_id=round_id, gm_id=user_id, start_ts=start_ts)

    _ephemeral(respond, f"Round created! You are the GM. Players can join with `/assassin join`. Targets will be assigned at 8am on {date_str}.")

    client.chat_postMessage(
        channel=ASSASSIN_CHANNEL_ID,
        text=f"A new Water Assassins round has been created by <@{user_id}>! Join with `/assassin join`. Targets assigned at 8am on {date_str}.",
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        ":droplet: *A new Water Assassins round is open!*\n\n"
                        f"Created by <@{user_id}>. Targets will be assigned at *8am on {date_str}*.\n\n"
                        "Sign up with `/assassin join` before the round starts!"
                    ),
                },
            }
        ],
    )


def _handle_report(body, client, respond):
    user_id = body["user_id"]
    trigger_id = body.get("trigger_id")
    state = _get_state()

    if state["status"] != "active":
        _ephemeral(respond, "There is no active round right now.")
        return

    player = _get_player(user_id)
    if not player or player.get("status") != "alive":
        _ephemeral(respond, "You are not an active player in this round.")
        return

    round_id = state["round_id"]
    pending = _get_pending_report_for_reporter(user_id, round_id)
    if pending:
        _ephemeral(respond, "You already have a kill report pending GM validation. Wait for it to be resolved.")
        return

    target_id = player.get("target_id")
    if not target_id:
        _ephemeral(respond, "You have no assigned target.")
        return

    # Check for a recent file upload from this player in #assassin-channel
    evidence_link = None
    try:
        history = client.conversations_history(channel=ASSASSIN_CHANNEL_ID, limit=50)
        for msg in history.get("messages", []):
            if msg.get("user") == user_id and msg.get("files"):
                evidence_link = msg["files"][0].get("permalink")
                break
    except Exception:
        pass

    if not evidence_link:
        _ephemeral(respond, f"No evidence found. Post your video in <#{ASSASSIN_CHANNEL_ID}> first, then run `/assassin report`.")
        return

    # Reject if this exact file permalink was already submitted in a previous report this round
    already_used = list(
        _db().collection(COL_REPORTS)
        .where("round_id", "==", round_id)
        .where("evidence_link", "==", evidence_link)
        .stream()
    )
    if already_used:
        _ephemeral(respond, "That video has already been submitted as evidence. Post a new video in the channel before reporting again.")
        return

    client.views_open(
        trigger_id=trigger_id,
        view={
            "type": "modal",
            "callback_id": "assassin_kill_modal",
            "title": {"type": "plain_text", "text": "Report a Kill"},
            "submit": {"type": "plain_text", "text": "Submit Report"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "private_metadata": f"{user_id}|{target_id}|{round_id}|{evidence_link}",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"You are reporting the elimination of your target: *<@{target_id}>*\n\n"
                            "This report will be sent to the GM for validation. "
                            "Only submit if you have genuinely eliminated your target."
                        ),
                    },
                },
                {"type": "divider"},
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": ":movie_camera: *Evidence:* Post your video in <#" + ASSASSIN_CHANNEL_ID + "> before submitting. The bot will automatically attach your most recent upload to this report.",
                    },
                },
                {
                    "type": "input",
                    "block_id": "confirmation_block",
                    "element": {
                        "type": "checkboxes",
                        "action_id": "confirmation_check",
                        "options": [
                            {
                                "text": {"type": "mrkdwn", "text": "I confirm I have eliminated my target"},
                                "value": "confirmed",
                            }
                        ],
                    },
                    "label": {"type": "plain_text", "text": "Confirmation"},
                },
            ],
        },
    )


def _handle_status(body, client, respond):
    user_id = body["user_id"]
    state = _get_state()

    if state["status"] == "none":
        _ephemeral(respond, "No Water Assassins game is currently active.")
        return

    player = _get_player(user_id)
    rnd = _get_round()

    if not player or not rnd or player.get("round_id") != rnd.get("round_id"):
        _ephemeral(respond, f"You are not registered in the current round. Use `/assassin join` to join.")
        return

    status = player.get("status", "unknown")
    kills = player.get("kills", 0)
    target_id = player.get("target_id")
    round_status = rnd.get("status", "unknown")

    if status == "pending":
        msg = f"*Status:* Registered, waiting for round to start (targets assigned at 8am on {rnd.get('start_date', 'TBD')})\n*Kills:* {kills}"
    elif status == "alive":
        target_line = f"*Current target:* <@{target_id}>" if target_id else "*Current target:* None"
        msg = f"*Status:* Alive :large_green_circle:\n{target_line}\n*Kills:* {kills}"
    elif status == "eliminated":
        killer = player.get("killed_by")
        killer_line = f" by <@{killer}>" if killer else ""
        msg = f"*Status:* Eliminated :red_circle:{killer_line}\n*Kills:* {kills}"
    else:
        msg = f"*Status:* {status}\n*Kills:* {kills}"

    _ephemeral(respond, msg)


def _handle_leaderboard(body, client, respond):
    state = _get_state()
    rnd = _get_round()

    if not rnd or state["status"] == "none":
        _ephemeral(respond, "No Water Assassins game data available.")
        return

    round_id = rnd.get("round_id")
    all_players = _get_players_for_round(round_id)
    sorted_players = sorted(all_players, key=lambda p: p.get("kills", 0), reverse=True)

    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for idx, p in enumerate(sorted_players):
        medal = medals[idx] if idx < 3 else f"{idx + 1}."
        status_icon = ":large_green_circle:" if p.get("status") == "alive" else ":red_circle:"
        lines.append(f"{medal} {status_icon} <@{p['user_id']}> — {p.get('kills', 0)} kill(s)")

    if not lines:
        text = "No players registered yet."
    else:
        text = "\n".join(lines)

    _ephemeral(respond, f":droplet: *Water Assassins — Leaderboard*\n\n{text}")


def _handle_eliminate(body, client, respond):
    gm_id = body["user_id"]
    state = _get_state()

    if state["status"] != "active":
        _ephemeral(respond, "There is no active round.")
        return

    if state.get("gm_id") != gm_id:
        _ephemeral(respond, "Only the GM can manually eliminate players.")
        return

    round_id = state["round_id"]
    alive = _get_alive_players(round_id)

    if not alive:
        _ephemeral(respond, "No alive players to eliminate.")
        return

    # Build options from alive players, fetching display names from Slack
    options = []
    for p in alive:
        uid = p["user_id"]
        if uid.startswith("UBOT"):
            name = uid  # fake bot player
        else:
            try:
                info = client.users_info(user=uid)
                name = (
                    info["user"].get("profile", {}).get("display_name")
                    or info["user"].get("real_name")
                    or uid
                )
            except Exception:
                name = uid
        options.append({
            "text": {"type": "plain_text", "text": name},
            "value": uid,
        })

    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "assassin_eliminate_modal",
            "title": {"type": "plain_text", "text": "Eliminate Player"},
            "submit": {"type": "plain_text", "text": "Eliminate"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "private_metadata": round_id,
            "blocks": [
                {
                    "type": "input",
                    "block_id": "player_block",
                    "label": {"type": "plain_text", "text": "Select player to eliminate"},
                    "element": {
                        "type": "static_select",
                        "action_id": "player_select",
                        "placeholder": {"type": "plain_text", "text": "Choose a player…"},
                        "options": options,
                    },
                }
            ],
        },
    )


def _do_eliminate(client, gm_id, target_id, round_id):
    """Shared elimination logic used by modal submit and debug commands."""
    target = _get_player(target_id)
    if not target or target.get("round_id") != round_id:
        return False, f"<@{target_id}> is not a registered player in the current round."
    if target.get("status") != "alive":
        return False, f"<@{target_id}> is already eliminated."

    db = _db()
    batch = db.batch()

    batch.set(db.collection(COL_PLAYERS).document(target_id), {
        "status": "eliminated",
        "killed_by": gm_id,
    }, merge=True)

    # Reassign: anyone targeting the eliminated player gets their target instead
    new_target_id = target.get("target_id")
    all_players = _get_players_for_round(round_id)
    for p in all_players:
        if p.get("target_id") == target_id and p.get("status") == "alive":
            batch.set(db.collection(COL_PLAYERS).document(p["user_id"]), {
                "target_id": new_target_id,
            }, merge=True)

    batch.commit()

    client.chat_postMessage(
        channel=ASSASSIN_CHANNEL_ID,
        text=f"<@{target_id}> has been eliminated by the GM.",
        blocks=[{
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":droplet: *<@{target_id}> has been manually eliminated by the GM.*",
            },
        }],
    )

    try:
        _dm(client, target_id, text="You have been manually eliminated from the round by the GM.")
    except Exception:
        pass

    alive = _get_alive_players(round_id)
    if len(alive) <= 1:
        _end_round(client, reason="last_standing")

    return True, f"<@{target_id}> has been eliminated."


def _handle_end(body, client, respond):
    user_id = body["user_id"]
    state = _get_state()

    if state["status"] != "active":
        _ephemeral(respond, "There is no active round to end.")
        return

    if state.get("gm_id") != user_id:
        _ephemeral(respond, "Only the GM can end the round.")
        return

    _ephemeral(respond, "Ending the round...")

    def run():
        _end_round(client, reason="gm_ended")

    threading.Thread(target=run, daemon=True).start()


def _handle_leave(body, client, respond):
    user_id = body["user_id"]
    state = _get_state()

    if state["status"] != "pending":
        if state["status"] == "active":
            _ephemeral(respond, "The round is already in progress — you cannot leave once targets have been assigned.")
        else:
            _ephemeral(respond, "There is no pending round to leave.")
        return

    rnd = _get_round()
    round_id = rnd["round_id"]

    player = _get_player(user_id)
    if not player or player.get("round_id") != round_id:
        _ephemeral(respond, "You are not registered in the current round.")
        return

    _db().collection(COL_PLAYERS).document(user_id).delete()

    players = _get_players_for_round(round_id)
    n = len(players)

    _ephemeral(respond, "You have left the Water Assassins round.")

    client.chat_postMessage(
        channel=ASSASSIN_CHANNEL_ID,
        text=f"<@{user_id}> has left the game. ({n} player(s) remaining)",
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f":droplet: <@{user_id}> has left the game. *{n} player(s)* remaining.",
                },
            }
        ],
    )


def _handle_players(body, client, respond):
    state = _get_state()
    rnd = _get_round()

    if not rnd or state["status"] == "none":
        _ephemeral(respond, "No active game.")
        return

    round_id = rnd.get("round_id")
    all_players = _get_players_for_round(round_id)
    alive = [p for p in all_players if p.get("status") == "alive"]
    pending = [p for p in all_players if p.get("status") == "pending"]

    if not all_players:
        _ephemeral(respond, "No players registered yet.")
        return

    lines = []
    if alive:
        lines.append("*Alive:*")
        lines.extend(f"• <@{p['user_id']}> — {p.get('kills', 0)} kill(s)" for p in alive)
    if pending:
        lines.append("*Pending (waiting for round start):*")
        lines.extend(f"• <@{p['user_id']}>" for p in pending)

    _ephemeral(respond, "\n".join(lines) if lines else "No active players.")


# ---------------------------------------------------------------------------
# Debug handlers
# ---------------------------------------------------------------------------

def _handle_debug(body, client, respond, raw_text):
    """
    Debug subcommands (ephemeral only — never posts to channel):
      /assassin debug state        — dump round + all players + pending reports
      /assassin debug assign       — force target assignment right now (skip 8am)
      /assassin debug kill         — auto-validate a kill for you against your current target
      /assassin debug reset        — wipe all Firestore game data and reset in-memory state
    """
    parts = raw_text.lower().split()
    # parts: ["debug", <subcmd>]
    subcmd = parts[1] if len(parts) > 1 else ""

    if subcmd == "state":
        _debug_state(respond)
    elif subcmd == "assign":
        _debug_assign(client, respond)
    elif subcmd == "kill":
        _debug_kill(body, client, respond)
    elif subcmd == "reset":
        _debug_reset(respond)
    elif subcmd == "addbot":
        parts2 = raw_text.split()
        n = int(parts2[2]) if len(parts2) > 2 and parts2[2].isdigit() else 2
        _debug_addbot(respond, n)
    elif subcmd == "echo":
        _ephemeral(respond, f"raw text: `{raw_text}`")
    else:
        _ephemeral(respond, (
            "*Debug commands:*\n"
            "• `/assassin debug state` — dump current round, players, and pending reports\n"
            "• `/assassin debug assign` — force target assignment now (skips 8am)\n"
            "• `/assassin debug kill` — instantly validate your kill against your current target\n"
            "• `/assassin debug reset` — wipe all game data and reset state\n"
            "• `/assassin debug addbot [n]` — add n fake bot players to the pending round (default 2)"
        ))


def _debug_state(respond):
    rnd = _get_round()
    state = _get_state()

    if not rnd:
        _ephemeral(respond, "No round document found in Firestore.")
        return

    lines = [
        "*— Round —*",
        f"round_id: `{rnd.get('round_id')}`",
        f"status: `{rnd.get('status')}`",
        f"gm_id: <@{rnd['gm_id']}>" if rnd.get('gm_id') else "gm_id: none",
        f"start_date: `{rnd.get('start_date')}`",
        f"start_ts: `{rnd.get('start_ts')}`",
        f"in-memory status: `{state['status']}`",
        "",
        "*— Players —*",
    ]

    players = _get_players_for_round(rnd.get("round_id", ""))
    if not players:
        lines.append("(none)")
    for p in players:
        target = f"→ <@{p['target_id']}>" if p.get("target_id") else "→ none"
        lines.append(
            f"<@{p['user_id']}> [{p.get('status')}] {target}  kills={p.get('kills', 0)}"
        )

    lines += ["", "*— Pending kill reports —*"]
    reports = list(
        _db().collection(COL_REPORTS)
        .where("round_id", "==", rnd.get("round_id", ""))
        .where("status", "==", "pending")
        .stream()
    )
    if not reports:
        lines.append("(none)")
    for r in reports:
        d = r.to_dict()
        lines.append(f"`{d['report_id'][:8]}…` <@{d['reporter_id']}> → <@{d['target_id']}>")

    _ephemeral(respond, "\n".join(lines))


def _debug_assign(client, respond):
    state = _get_state()
    if state["status"] != "pending":
        _ephemeral(respond, f"Round is not pending (status: `{state['status']}`). Cannot force assign.")
        return
    _ephemeral(respond, "Forcing target assignment now…")
    _assign_targets(client)
    _ephemeral(respond, "Done. Check your DMs for your target.")


def _debug_kill(body, client, respond):
    user_id = body["user_id"]
    state = _get_state()

    if state["status"] != "active":
        _ephemeral(respond, f"Round is not active (status: `{state['status']}`).")
        return

    player = _get_player(user_id)
    if not player or player.get("status") != "alive":
        _ephemeral(respond, "You are not an alive player in this round.")
        return

    target_id = player.get("target_id")
    if not target_id:
        _ephemeral(respond, "You have no assigned target.")
        return

    target_player = _get_player(target_id)
    if not target_player or target_player.get("status") != "alive":
        _ephemeral(respond, f"<@{target_id}> is already eliminated.")
        return

    new_target_id = target_player.get("target_id")
    round_id = state["round_id"]

    db = _db()
    batch = db.batch()
    batch.set(db.collection(COL_PLAYERS).document(target_id), {
        "status": "eliminated", "killed_by": user_id,
    }, merge=True)
    batch.set(db.collection(COL_PLAYERS).document(user_id), {
        "kills": firestore.Increment(1), "target_id": new_target_id,
    }, merge=True)
    batch.commit()

    alive = _get_alive_players(round_id)
    n_alive = len(alive)

    _ephemeral(respond, (
        f"[DEBUG] Kill applied: <@{target_id}> eliminated.\n"
        f"New target: {'<@' + new_target_id + '>' if new_target_id else 'none'}.\n"
        f"{n_alive} player(s) remaining."
    ))

    if n_alive <= 1:
        _end_round(client, reason="last_standing")
        return

    client.chat_postMessage(
        channel=ASSASSIN_CHANNEL_ID,
        text=f"[DEBUG] <@{target_id}> has been eliminated by <@{user_id}>! {n_alive} player(s) remain.",
    )


def _debug_addbot(respond, n):
    state = _get_state()
    if state["status"] != "pending":
        _ephemeral(respond, f"Round is not pending (status: `{state['status']}`). Cannot add bots.")
        return

    rnd = _get_round()
    round_id = rnd["round_id"]

    # Find the next available bot slot (avoid collisions across resets)
    existing = [
        p["user_id"] for p in _get_players_for_round(round_id)
        if p["user_id"].startswith("UBOT")
    ]
    start_index = len(existing) + 1

    db = _db()
    added = []
    for i in range(start_index, start_index + n):
        bot_id = f"UBOT{i:03d}"
        db.collection(COL_PLAYERS).document(bot_id).set({
            "user_id": bot_id,
            "round_id": round_id,
            "status": "pending",
            "target_id": None,
            "kills": 0,
            "killed_by": None,
            "joined_ts": time.time(),
            "assigned_ts": None,
        })
        added.append(bot_id)

    total = len(_get_players_for_round(round_id))
    bot_list = ", ".join(f"`{b}`" for b in added)
    _ephemeral(respond, f"[DEBUG] Added {n} bot(s): {bot_list}\nTotal players in round: {total}")


def _debug_reset(respond):
    db = _db()

    # Delete round doc
    db.collection(COL_ROUNDS).document("current").delete()

    # Delete all player docs
    for doc in db.collection(COL_PLAYERS).stream():
        doc.reference.delete()

    # Delete all kill reports
    for doc in db.collection(COL_REPORTS).stream():
        doc.reference.delete()

    with _state_lock:
        _game_state["status"] = "none"
        _game_state["round_id"] = None
        _game_state["gm_id"] = None
        _game_state["start_ts"] = None

    _ephemeral(respond, "[DEBUG] All game data wiped. State reset to `none`.")


# ---------------------------------------------------------------------------
# Register handlers
# ---------------------------------------------------------------------------

def register_assassins_handlers(app):
    _restore_state_from_firestore()

    token = os.environ.get("SLACK_BOT_TOKEN")
    slack_client = WebClient(token=token)
    t = threading.Thread(target=lambda: _assassins_bg_loop(slack_client), daemon=True)
    t.start()

    @app.command("/assassin")
    def cmd_assassin(ack, body, client, logger, respond):
        ack()
        raw_text = (body.get("text") or "").strip()
        sub = raw_text.lower().split()[0] if raw_text else ""

        def run():
            try:
                if sub == "join":
                    _handle_join(body, client, respond)
                elif sub == "start":
                    _handle_start(body, client, respond)
                elif sub == "report":
                    # report opens a modal — must be called synchronously (needs trigger_id)
                    _handle_report(body, client, respond)
                elif sub == "status":
                    _handle_status(body, client, respond)
                elif sub == "leaderboard":
                    _handle_leaderboard(body, client, respond)
                elif sub == "end":
                    _handle_end(body, client, respond)
                elif sub == "eliminate":
                    _handle_eliminate(body, client, respond)
                elif sub == "leave":
                    _handle_leave(body, client, respond)
                elif sub == "players":
                    _handle_players(body, client, respond)
                elif sub == "debug":
                    _handle_debug(body, client, respond, raw_text)
                else:
                    respond({
                        "response_type": "ephemeral",
                        "text": (
                            "*Water Assassins commands:*\n"
                            "• `/assassin join` — join the pending round\n"
                            "• `/assassin leave` — leave before the round starts\n"
                            "• `/assassin start YYYY-MM-DD` — (GM) create a new round\n"
                            "• `/assassin report` — report a kill (GM validates)\n"
                            "• `/assassin status` — view your current status & target\n"
                            "• `/assassin players` — list alive players\n"
                            "• `/assassin leaderboard` — post kill leaderboard\n"
                            "• `/assassin end` — (GM only) end the current round"
                        ),
                    })
            except Exception as e:
                logger.exception(f"[Assassins] cmd error: {e}")

        if sub in ("report", "eliminate"):
            # Modals must be opened synchronously (trigger_id expires quickly)
            run()
        else:
            threading.Thread(target=run, daemon=True).start()

    @app.view("assassin_kill_modal")
    def view_kill_report(ack, body, client, logger):
        ack()

        def run():
            try:
                user_id = body["user"]["id"]
                private_metadata = body["view"]["private_metadata"]
                parts = private_metadata.split("|")
                if len(parts) < 3:
                    return
                reporter_id, target_id, round_id = parts[0], parts[1], parts[2]
                evidence_link = parts[3] if len(parts) > 3 and parts[3] != "None" else None

                # Verify confirmation checked
                values = body["view"]["state"]["values"]
                checked = values.get("confirmation_block", {}).get("confirmation_check", {}).get("selected_options", [])
                if not checked:
                    return


                report_id = str(uuid.uuid4())
                _db().collection(COL_REPORTS).document(report_id).set({
                    "report_id": report_id,
                    "round_id": round_id,
                    "reporter_id": reporter_id,
                    "target_id": target_id,
                    "status": "pending",
                    "evidence_link": evidence_link,
                    "gm_dm_channel": None,
                    "gm_dm_ts": None,
                    "created_ts": time.time(),
                    "resolved_ts": None,
                })

                state = _get_state()
                gm_id = state.get("gm_id")
                if not gm_id:
                    return

                # DM GM with validate/reject buttons
                gm_dm = client.conversations_open(users=[gm_id])
                gm_channel = gm_dm["channel"]["id"]
                msg = client.chat_postMessage(
                    channel=gm_channel,
                    text=f"Kill report: <@{reporter_id}> claims they eliminated <@{target_id}>.",
                    blocks=[
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": (
                                    f":droplet: *Kill Report*\n\n"
                                    f"<@{reporter_id}> claims to have eliminated *<@{target_id}>*.\n\n"
                                    "Validate or reject below."
                                ),
                            },
                        },
                        {
                            "type": "actions",
                            "block_id": f"kill_report_{report_id}",
                            "elements": [
                                {
                                    "type": "button",
                                    "text": {"type": "plain_text", "text": "✅ Validate"},
                                    "style": "primary",
                                    "action_id": "assassin_validate_kill",
                                    "value": report_id,
                                },
                                {
                                    "type": "button",
                                    "text": {"type": "plain_text", "text": "❌ Reject"},
                                    "style": "danger",
                                    "action_id": "assassin_reject_kill",
                                    "value": report_id,
                                },
                            ],
                        },
                    ],
                )

                # Save DM info to report
                _db().collection(COL_REPORTS).document(report_id).set({
                    "gm_dm_channel": gm_channel,
                    "gm_dm_ts": msg["ts"],
                }, merge=True)

                # Forward auto-detected evidence link to GM
                if evidence_link:
                    client.chat_postMessage(
                        channel=gm_channel,
                        text=f"Evidence from <@{reporter_id}>: {evidence_link}",
                        blocks=[
                            {
                                "type": "section",
                                "text": {
                                    "type": "mrkdwn",
                                    "text": f":movie_camera: *Evidence (auto-detected):*\n{evidence_link}",
                                },
                            }
                        ],
                    )
                else:
                    client.chat_postMessage(
                        channel=gm_channel,
                        text=f"No evidence file found from <@{reporter_id}> in #assassin-channel.",
                        blocks=[
                            {
                                "type": "section",
                                "text": {
                                    "type": "mrkdwn",
                                    "text": f":warning: No evidence file found from <@{reporter_id}> in <#{ASSASSIN_CHANNEL_ID}>.",
                                },
                            }
                        ],
                    )

            except Exception as e:
                logger.exception(f"[Assassins] view_kill_report error: {e}")

        threading.Thread(target=run, daemon=True).start()

    @app.action("assassin_validate_kill")
    def action_validate(ack, body, client, logger):
        ack()

        def run():
            try:
                validator_id = body["user"]["id"]
                state = _get_state()

                if state.get("gm_id") != validator_id:
                    client.chat_postEphemeral(
                        channel=body["container"]["channel_id"],
                        user=validator_id,
                        text="Only the GM can validate kills.",
                    )
                    return

                report_id = body["actions"][0]["value"]
                report = _get_report(report_id)
                if not report or report.get("status") != "pending":
                    client.chat_postEphemeral(
                        channel=body["container"]["channel_id"],
                        user=validator_id,
                        text="This kill report has already been resolved.",
                    )
                    return

                reporter_id = report["reporter_id"]
                target_id = report["target_id"]
                round_id = report["round_id"]

                target_player = _get_player(target_id)
                if not target_player or target_player.get("status") != "alive":
                    client.chat_postEphemeral(
                        channel=body["container"]["channel_id"],
                        user=validator_id,
                        text=f"<@{target_id}> is already eliminated. Cannot validate.",
                    )
                    return

                reporter_player = _get_player(reporter_id)
                new_target_id = target_player.get("target_id")

                db = _db()
                batch = db.batch()

                # Mark kill report validated
                batch.set(db.collection(COL_REPORTS).document(report_id), {
                    "status": "validated",
                    "resolved_ts": time.time(),
                }, merge=True)

                # Eliminate target
                batch.set(db.collection(COL_PLAYERS).document(target_id), {
                    "status": "eliminated",
                    "killed_by": reporter_id,
                }, merge=True)

                # Update reporter: +1 kill, new target
                batch.set(db.collection(COL_PLAYERS).document(reporter_id), {
                    "kills": firestore.Increment(1),
                    "target_id": new_target_id,
                }, merge=True)

                batch.commit()

                # Update GM DM message
                gm_channel = report.get("gm_dm_channel")
                gm_ts = report.get("gm_dm_ts")
                if gm_channel and gm_ts:
                    client.chat_update(
                        channel=gm_channel,
                        ts=gm_ts,
                        text=f"✅ Validated: <@{reporter_id}> eliminated <@{target_id}>.",
                        blocks=[
                            {
                                "type": "section",
                                "text": {
                                    "type": "mrkdwn",
                                    "text": f"✅ *Validated:* <@{reporter_id}> eliminated <@{target_id}>.",
                                },
                            }
                        ],
                    )

                # DM reporter with new target
                if new_target_id and new_target_id != reporter_id:
                    try:
                        _dm(client, reporter_id, blocks=[
                            {
                                "type": "section",
                                "text": {
                                    "type": "mrkdwn",
                                    "text": (
                                        f"✅ *Kill validated!* <@{target_id}> has been eliminated.\n\n"
                                        f"Your new target is *<@{new_target_id}>*. Keep hunting! :droplet:"
                                    ),
                                },
                            }
                        ])
                    except Exception:
                        pass
                else:
                    try:
                        _dm(client, reporter_id, text="✅ Kill validated! You are the last hunter standing!")
                    except Exception:
                        pass

                # DM eliminated player
                try:
                    _dm(client, target_id, blocks=[
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": (
                                    f":red_circle: *You have been eliminated* by <@{reporter_id}>.\n\n"
                                    "Better luck next round!"
                                ),
                            },
                        }
                    ])
                except Exception:
                    pass

                # Count remaining alive players
                alive = _get_alive_players(round_id)
                n_alive = len(alive)

                client.chat_postMessage(
                    channel=ASSASSIN_CHANNEL_ID,
                    text=f"<@{target_id}> has been eliminated by <@{reporter_id}>! {n_alive} player(s) remain.",
                    blocks=[
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": (
                                    f":droplet: *<@{target_id}> has been eliminated!*\n"
                                    f"<@{reporter_id}> moves on. *{n_alive} player(s) remain.*"
                                ),
                            },
                        }
                    ],
                )

                # Auto-end if only 1 player remains
                if n_alive <= 1:
                    _end_round(client, reason="last_standing")

            except Exception as e:
                logger.exception(f"[Assassins] action_validate error: {e}")

        threading.Thread(target=run, daemon=True).start()

    @app.action("assassin_reject_kill")
    def action_reject(ack, body, client, logger):
        ack()

        def run():
            try:
                validator_id = body["user"]["id"]
                state = _get_state()

                if state.get("gm_id") != validator_id:
                    client.chat_postEphemeral(
                        channel=body["container"]["channel_id"],
                        user=validator_id,
                        text="Only the GM can reject kills.",
                    )
                    return

                report_id = body["actions"][0]["value"]
                report = _get_report(report_id)
                if not report or report.get("status") != "pending":
                    client.chat_postEphemeral(
                        channel=body["container"]["channel_id"],
                        user=validator_id,
                        text="This kill report has already been resolved.",
                    )
                    return

                reporter_id = report["reporter_id"]
                target_id = report["target_id"]

                _db().collection(COL_REPORTS).document(report_id).set({
                    "status": "rejected",
                    "resolved_ts": time.time(),
                }, merge=True)

                # Update GM DM message
                gm_channel = report.get("gm_dm_channel")
                gm_ts = report.get("gm_dm_ts")
                if gm_channel and gm_ts:
                    client.chat_update(
                        channel=gm_channel,
                        ts=gm_ts,
                        text=f"❌ Rejected: <@{reporter_id}>'s claim against <@{target_id}>.",
                        blocks=[
                            {
                                "type": "section",
                                "text": {
                                    "type": "mrkdwn",
                                    "text": f"❌ *Rejected:* <@{reporter_id}>'s claim against <@{target_id}>.",
                                },
                            }
                        ],
                    )

                # DM reporter
                try:
                    _dm(client, reporter_id, blocks=[
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": (
                                    f":x: Your kill report for <@{target_id}> was *rejected* by the GM.\n\n"
                                    f"Your target remains <@{target_id}>. Keep hunting!"
                                ),
                            },
                        }
                    ])
                except Exception:
                    pass

            except Exception as e:
                logger.exception(f"[Assassins] action_reject error: {e}")

        threading.Thread(target=run, daemon=True).start()

    @app.view("assassin_eliminate_modal")
    def view_eliminate(ack, body, client, logger):
        ack()

        def run():
            try:
                gm_id = body["user"]["id"]
                round_id = body["view"]["private_metadata"]
                values = body["view"]["state"]["values"]
                target_id = values["player_block"]["player_select"]["selected_option"]["value"]

                ok, msg = _do_eliminate(client, gm_id, target_id, round_id)
                if not ok:
                    # Can't respond to the user after modal close easily, so just log
                    logger.warning(f"[Assassins] eliminate modal: {msg}")
            except Exception as e:
                logger.exception(f"[Assassins] view_eliminate error: {e}")

"""Water Assassins game: multi-round single game. Target assignment, kill reporting, GM validation, leaderboard."""
import os
import random
import threading
import time
import uuid
from datetime import datetime, timedelta

import pytz
from firebase_admin import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from firebase_client import get_firebase_app
from slack_sdk import WebClient

ASSASSIN_CHANNEL_ID = "C0ASVT9J3S4"
TIMEZONE = pytz.timezone("America/Los_Angeles")

COL_GAME = "assassin_game"
COL_ROUNDS = "assassin_rounds"
COL_PLAYERS = "assassin_players"
COL_REPORTS = "assassin_kill_reports"

# ---------------------------------------------------------------------------
# Safe zone constants
# ---------------------------------------------------------------------------

SAFE_ZONE_LOCATIONS = [
    "Science Library 2nd Floor",
    "Science Library 4th Floor",
    "Science Library 5th Floor",
    "Anthill Pub & Grille",
    "CSL",
    "Gateway Study Center Basement",
    "The Ring Road tunnel near Student Center",
    "Donald Bren Hall 2nd Floor",
    "Donald Bren Hall 6th Floor",
    "Humanities Gateway 2nd Floor (Ryan's Secret Spot)",
    "Taco Bell",
    "ALP 3rd Floor",
    "The Hill (UCI Merch Store)",
    "Langson Library 5th floor",
    "Alrich Park Grass (Have to be touching grass to be in the safe zone)"
]

SAFE_ZONE_DURATION_SECONDS = 3600  # 1 hour
SAFE_ZONE_WINDOW_START_HOUR = 10   # 10am
SAFE_ZONE_WINDOW_END_HOUR = 18     # 6pm (last safe zone must start by 5pm to end by 6pm)

# ---------------------------------------------------------------------------
# In-memory state (restored from Firestore on startup)
# ---------------------------------------------------------------------------

_game_state = {
    "status": "none",   # "none" | "pending" | "active" | "round_ended" | "game_over"
    "game_id": None,
    "gm_id": None,
    "round_number": 0,
    "start_ts": None,
    "scheduled_end_ts": None,
    # Safe zone state
    "safe_zone_next_ts": None,      # epoch when the safe zone goes live
    "safe_zone_warning_ts": None,   # epoch for the 30-min heads-up (safe_zone_next_ts - 1800)
    "safe_zone_warning_sent": False, # whether the 30-min warning has been posted
    "safe_zone_location": None,     # location chosen at schedule time (used for warning + activation)
    "safe_zone_expires_ts": None,   # epoch when the active safe zone expires
}
_state_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Firestore helpers
# ---------------------------------------------------------------------------

def _db():
    get_firebase_app()
    return firestore.client()


def _get_game():
    doc = _db().collection(COL_GAME).document("current").get()
    return doc.to_dict() if doc.exists else None


def _get_round(round_number=None):
    state = _get_state()
    n = round_number if round_number is not None else state["round_number"]
    if not n:
        return None
    doc = _db().collection(COL_ROUNDS).document(str(n)).get()
    return doc.to_dict() if doc.exists else None


def _get_player(user_id):
    doc = _db().collection(COL_PLAYERS).document(user_id).get()
    return doc.to_dict() if doc.exists else None


def _get_players_for_game(game_id):
    """All players in the game regardless of status."""
    return [
        doc.to_dict()
        for doc in _db().collection(COL_PLAYERS)
        .where(filter=FieldFilter("game_id", "==", game_id))
        .stream()
    ]


def _get_active_players(game_id):
    """Players who can participate this round: alive or pending (not eliminated)."""
    all_players = _get_players_for_game(game_id)
    return [p for p in all_players if p.get("status") in ("alive", "pending")]


def _get_alive_players(game_id):
    return [
        doc.to_dict()
        for doc in _db()
        .collection(COL_PLAYERS)
        .where(filter=FieldFilter("game_id", "==", game_id))
        .where(filter=FieldFilter("status", "==", "alive"))
        .stream()
    ]


def _get_pending_report_for_reporter(reporter_id, round_number):
    docs = list(
        _db()
        .collection(COL_REPORTS)
        .where(filter=FieldFilter("reporter_id", "==", reporter_id))
        .where(filter=FieldFilter("round_number", "==", round_number))
        .where(filter=FieldFilter("status", "==", "pending"))
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
    """Load game state from Firestore into _game_state on startup."""
    with _state_lock:
        try:
            game = _get_game()
            if not game:
                _game_state["status"] = "none"
                return

            status = game.get("status", "none")
            _game_state["status"] = status
            _game_state["game_id"] = game.get("game_id")
            _game_state["gm_id"] = game.get("gm_id")
            _game_state["round_number"] = game.get("round_number", 0)

            if status in ("pending", "active"):
                rnd_doc = _db().collection(COL_ROUNDS).document(
                    str(game.get("round_number", 0))
                ).get()
                if rnd_doc.exists:
                    rnd = rnd_doc.to_dict()
                    _game_state["start_ts"] = rnd.get("start_ts")
                    _game_state["scheduled_end_ts"] = rnd.get("scheduled_end_ts")
        except Exception as e:
            print(f"[Assassins] Failed to restore state: {e}")


def _set_state(status, game_id=None, gm_id=None, round_number=None,
               start_ts=None, scheduled_end_ts=None):
    with _state_lock:
        _game_state["status"] = status
        if game_id is not None:
            _game_state["game_id"] = game_id
        if gm_id is not None:
            _game_state["gm_id"] = gm_id
        if round_number is not None:
            _game_state["round_number"] = round_number
        if start_ts is not None:
            _game_state["start_ts"] = start_ts
        if scheduled_end_ts is not None:
            _game_state["scheduled_end_ts"] = scheduled_end_ts


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
    game_id = state["game_id"]
    round_number = state["round_number"]

    players = _get_active_players(game_id)

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

    for i, uid in enumerate(player_ids):
        target_id = player_ids[(i + 1) % n]
        ref = db.collection(COL_PLAYERS).document(uid)
        batch.set(ref, {
            "status": "alive",
            "target_id": target_id,
            "kills_this_round": 0,
            "assigned_ts": time.time(),
        }, merge=True)

    round_ref = db.collection(COL_ROUNDS).document(str(round_number))
    batch.set(round_ref, {
        "status": "active",
        "start_ts": time.time(),
        "player_order": player_ids,
    }, merge=True)

    game_ref = db.collection(COL_GAME).document("current")
    batch.set(game_ref, {"status": "active"}, merge=True)

    batch.commit()
    _set_state("active", start_ts=time.time())

    for i, uid in enumerate(player_ids):
        target_id = player_ids[(i + 1) % n]
        try:
            _dm(client, uid, blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"*Water Assassins — Round {round_number} has begun!* :droplet::dagger_knife:\n\n"
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
        text=f"Round {round_number} has begun! {n} players are hunting.",
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f":droplet: *Water Assassins — Round {round_number} Started!*\n\n"
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
    game_id = state["game_id"]
    round_number = state["round_number"]

    alive = _get_alive_players(game_id)
    zero_kill_alive = [p for p in alive if p.get("kills_this_round", 0) == 0]
    survivors = [p for p in alive if p.get("kills_this_round", 0) > 0]

    db = _db()
    batch = db.batch()

    for p in zero_kill_alive:
        ref = db.collection(COL_PLAYERS).document(p["user_id"])
        batch.set(ref, {
            "status": "eliminated",
            "eliminated_in_round": round_number,
        }, merge=True)

    round_ref = db.collection(COL_ROUNDS).document(str(round_number))
    batch.set(round_ref, {"status": "ended", "end_ts": time.time()}, merge=True)

    # Determine if game should end
    eliminated_ids = {p["user_id"] for p in zero_kill_alive}
    remaining_alive = [p for p in alive if p["user_id"] not in eliminated_ids]

    if reason == "last_standing" or len(remaining_alive) <= 1:
        new_status = "game_over"
    else:
        new_status = "round_ended"

    batch.set(db.collection(COL_GAME).document("current"), {
        "status": new_status,
    }, merge=True)
    batch.commit()

    with _state_lock:
        _game_state["status"] = new_status
        _game_state["scheduled_end_ts"] = None

    for p in zero_kill_alive:
        try:
            _dm(client, p["user_id"],
                text="The round has ended. You were eliminated for not securing a kill this round. You're out of the game.")
        except Exception:
            pass

    all_game_players = _get_players_for_game(game_id)
    sorted_players = sorted(all_game_players, key=lambda p: p.get("total_kills", 0), reverse=True)

    medals = ["🥇", "🥈", "🥉"]
    leaderboard_lines = []
    rank = 0
    for p in sorted_players:
        if p.get("total_kills", 0) == 0:
            continue
        medal = medals[rank] if rank < 3 else f"{rank + 1}."
        leaderboard_lines.append(f"{medal} <@{p['user_id']}> — {p.get('total_kills', 0)} kill(s)")
        rank += 1

    leaderboard_text = "\n".join(leaderboard_lines) if leaderboard_lines else "No kills were recorded this round."

    if new_status == "game_over":
        if remaining_alive:
            winner_uid = remaining_alive[0]["user_id"]
            winner_line = f"🏆 *Winner: <@{winner_uid}>* — last hunter standing!"
        else:
            winner_line = "🏆 *Game over!* No survivors remain."
        end_note = "\n\n*The game is over.* Thanks for playing!"
    else:
        survivor_ids = [p["user_id"] for p in remaining_alive]
        winner_line = (
            "*Survivors advancing:* " + ", ".join(f"<@{uid}>" for uid in survivor_ids)
            if survivor_ids else "No survivors remain."
        )
        end_note = "\n\nThe GM will announce when the next round opens."

    zero_kill_ids = [p["user_id"] for p in zero_kill_alive]
    eliminated_line = (
        "\n\n*Eliminated (no kills this round):* " + ", ".join(f"<@{uid}>" for uid in zero_kill_ids)
        if zero_kill_ids else ""
    )

    client.chat_postMessage(
        channel=ASSASSIN_CHANNEL_ID,
        text=f"Water Assassins Round {round_number} has ended!",
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f":droplet: *Water Assassins — Round {round_number} Over!*\n\n"
                        f"{winner_line}"
                        f"{eliminated_line}"
                        f"{end_note}"
                    ),
                },
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Kill Leaderboard (All Rounds):*\n{leaderboard_text}",
                },
            },
        ],
    )


# ---------------------------------------------------------------------------
# Round advancement (pre-scheduled rounds)
# ---------------------------------------------------------------------------

def _advance_to_round(client, round_number, round_doc):
    """Automatically start the next pre-scheduled round after a round_ended transition."""
    state = _get_state()
    game_id = state["game_id"]

    alive = _get_alive_players(game_id)
    if len(alive) < 2:
        _db().collection(COL_GAME).document("current").set({"status": "game_over"}, merge=True)
        with _state_lock:
            _game_state["status"] = "game_over"
        client.chat_postMessage(
            channel=ASSASSIN_CHANNEL_ID,
            text="Water Assassins: Not enough surviving players for the next round. Game over!",
        )
        return

    start_ts = round_doc.get("start_ts")
    scheduled_end_ts = round_doc.get("scheduled_end_ts")

    db = _db()
    db.collection(COL_GAME).document("current").set(
        {"status": "pending", "round_number": round_number}, merge=True
    )
    db.collection(COL_ROUNDS).document(str(round_number)).set(
        {"status": "pending"}, merge=True
    )

    _set_state("pending", round_number=round_number, start_ts=start_ts,
               scheduled_end_ts=scheduled_end_ts)

    # Start time has already passed — assign targets immediately
    _assign_targets(client)


# ---------------------------------------------------------------------------
# Safe zone helpers
# ---------------------------------------------------------------------------

def _is_last_round():
    """Return True if the current round is the final round of the game."""
    state = _get_state()
    game = _get_game()
    if not game:
        return False
    total_rounds = game.get("total_rounds")
    if not total_rounds:
        return False
    return state.get("round_number", 0) >= total_rounds


def _next_safe_zone_ts():
    """Pick a random time today or the next valid weekday between 10am and 5pm (PT).

    Safe zones start no later than 5pm so they end by 6pm.
    Returns an epoch timestamp.
    """
    now = datetime.now(TIMEZONE)

    def _random_ts_on_date(year, month, day):
        """Return a random epoch timestamp between 10am and 5pm on the given date."""
        start_hour = SAFE_ZONE_WINDOW_START_HOUR
        end_hour = SAFE_ZONE_WINDOW_END_HOUR - 1  # last start at 5pm → ends at 6pm
        hour = random.randint(start_hour, end_hour)
        minute = random.randint(0, 59)
        naive = datetime(year, month, day, hour, minute, 0)
        return TIMEZONE.localize(naive).timestamp()

    # Try today first (if we have at least 15 minutes left in the window)
    window_close = now.replace(hour=SAFE_ZONE_WINDOW_END_HOUR - 1, minute=45, second=0, microsecond=0)
    if now.weekday() < 5 and now < window_close:
        candidate = _random_ts_on_date(now.year, now.month, now.day)
        # Must be at least 5 minutes in the future
        if candidate > time.time() + 300:
            return candidate

    # Otherwise advance to the next weekday
    candidate_date = now.date() + timedelta(days=1)
    while True:
        # 0=Mon … 4=Fri
        if candidate_date.weekday() < 5:
            return _random_ts_on_date(candidate_date.year, candidate_date.month, candidate_date.day)
        candidate_date += timedelta(days=1)


def _schedule_safe_zone():
    """Pick a location and schedule the next safe zone, including the 30-min warning."""
    ts = _next_safe_zone_ts()
    location = random.choice(SAFE_ZONE_LOCATIONS)
    warning_ts = ts - 1800  # 30 minutes before
    with _state_lock:
        _game_state["safe_zone_next_ts"] = ts
        _game_state["safe_zone_warning_ts"] = warning_ts
        _game_state["safe_zone_warning_sent"] = False
        _game_state["safe_zone_location"] = location
        _game_state["safe_zone_expires_ts"] = None
    dt = datetime.fromtimestamp(ts, tz=TIMEZONE)
    print(f"[Assassins] Next safe zone scheduled for {dt.strftime('%Y-%m-%d %H:%M %Z')} at {location}")


def _warn_safe_zone(client, location, start_ts):
    """Post the 30-minute heads-up for an upcoming safe zone."""
    start_dt = datetime.fromtimestamp(start_ts, tz=TIMEZONE)
    start_str = start_dt.strftime("%-I:%M %p")

    with _state_lock:
        _game_state["safe_zone_warning_sent"] = True

    client.chat_postMessage(
        channel=ASSASSIN_CHANNEL_ID,
        text=f":hourglass: Safe zone incoming: {location} at {start_str} PT (in 30 minutes)",
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f":hourglass: *Safe Zone in 30 minutes!*\n\n"
                        f"*Location:* {location}\n"
                        f"*Starts at:* {start_str} PT\n\n"
                        "Use it to regroup — no eliminations allowed at this location once it goes live."
                    ),
                },
            }
        ],
    )
    print(f"[Assassins] Safe zone 30-min warning sent: {location} at {start_str}")


def _activate_safe_zone(client, location):
    """Activate the safe zone and announce it."""
    expires_ts = time.time() + SAFE_ZONE_DURATION_SECONDS
    expires_dt = datetime.fromtimestamp(expires_ts, tz=TIMEZONE)
    expires_str = expires_dt.strftime("%-I:%M %p")

    with _state_lock:
        _game_state["safe_zone_expires_ts"] = expires_ts
        _game_state["safe_zone_next_ts"] = None
        _game_state["safe_zone_warning_ts"] = None

    client.chat_postMessage(
        channel=ASSASSIN_CHANNEL_ID,
        text=f":shield: Safe zone active: {location} (until {expires_str} PT)",
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f":shield: *Safe Zone Active!*\n\n"
                        f"*Location:* {location}\n"
                        f"*Duration:* 1 hour (until *{expires_str} PT*)\n\n"
                        "No eliminations may occur at this location while the safe zone is active. "
                        "Use it to breathe — but the hunt resumes the moment it ends."
                    ),
                },
            }
        ],
    )
    print(f"[Assassins] Safe zone activated: {location}, expires {expires_str}")


def _expire_safe_zone(client):
    """Announce safe zone expiry and schedule the next one."""
    with _state_lock:
        location = _game_state.get("safe_zone_location", "the safe zone")
        _game_state["safe_zone_location"] = None
        _game_state["safe_zone_expires_ts"] = None

    client.chat_postMessage(
        channel=ASSASSIN_CHANNEL_ID,
        text=f":warning: Safe zone at {location} has ended. The hunt resumes!",
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f":warning: *Safe zone at {location} has ended.*\n\n"
                        "The hunt is back on — stay sharp! :droplet:"
                    ),
                },
            }
        ],
    )
    print(f"[Assassins] Safe zone expired: {location}")
    _schedule_safe_zone()


# ---------------------------------------------------------------------------
# Background thread
# ---------------------------------------------------------------------------

def _tick(client):
    state = _get_state()

    if state["status"] == "pending" and state.get("start_ts"):
        if time.time() >= state["start_ts"]:
            print(f"[Assassins] Start time reached — assigning targets for round {state['round_number']}")
            _assign_targets(client)

    if state["status"] == "active" and state.get("scheduled_end_ts"):
        if time.time() >= state["scheduled_end_ts"]:
            print("[Assassins] Scheduled end time reached — ending round")
            _end_round(client, reason="scheduled")

    if state["status"] == "round_ended":
        next_rnd_num = state["round_number"] + 1
        next_rnd = _get_round(next_rnd_num)
        if next_rnd and next_rnd.get("status") == "scheduled" and next_rnd.get("start_ts"):
            if time.time() >= next_rnd["start_ts"]:
                print(f"[Assassins] Auto-advancing to pre-scheduled round {next_rnd_num}")
                _advance_to_round(client, next_rnd_num, next_rnd)

    # --- Safe zone tick (only during active non-final rounds) ---
    if state["status"] == "active" and not _is_last_round():
        now = time.time()

        # Check if an active safe zone has expired
        expires_ts = state.get("safe_zone_expires_ts")
        if expires_ts and now >= expires_ts:
            _expire_safe_zone(client)
            return  # state mutated; next tick will handle scheduling

        # Check if it's time to activate the safe zone
        next_ts = state.get("safe_zone_next_ts")
        if next_ts and now >= next_ts and not expires_ts:
            _activate_safe_zone(client, state["safe_zone_location"])
            return

        # Check if it's time to send the 30-min warning
        warning_ts = state.get("safe_zone_warning_ts")
        if warning_ts and now >= warning_ts and not state.get("safe_zone_warning_sent"):
            _warn_safe_zone(client, state["safe_zone_location"], next_ts)
            return

        # If nothing is scheduled or active, schedule the next safe zone
        if not expires_ts and not next_ts:
            _schedule_safe_zone()
    else:
        # Round not active — clear any lingering safe zone state silently
        with _state_lock:
            _game_state["safe_zone_next_ts"] = None
            _game_state["safe_zone_warning_ts"] = None
            _game_state["safe_zone_warning_sent"] = False
            _game_state["safe_zone_location"] = None
            _game_state["safe_zone_expires_ts"] = None


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
            _ephemeral(respond, "No game is accepting players right now. Wait for a GM to run `/assassin start YYYY-MM-DD`.")
        elif state["status"] == "active":
            _ephemeral(respond, "A round is already in progress. Wait for it to end.")
        elif state["status"] in ("round_ended", "game_over"):
            _ephemeral(respond, "Sign-ups are not open. Wait for the GM to open the next round.")
        else:
            _ephemeral(respond, "No active game right now.")
        return

    game_id = state["game_id"]
    existing = _get_player(user_id)

    if existing and existing.get("game_id") == game_id:
        if existing.get("status") == "eliminated":
            _ephemeral(respond, "You have been eliminated and cannot rejoin this game.")
            return
        if existing.get("status") in ("pending", "alive"):
            _ephemeral(respond, "You've already joined this game!")
            return

    rnd = _get_round()

    _db().collection(COL_PLAYERS).document(user_id).set({
        "user_id": user_id,
        "game_id": game_id,
        "status": "pending",
        "target_id": None,
        "kills_this_round": 0,
        "total_kills": 0,
        "killed_by": None,
        "eliminated_in_round": None,
        "joined_ts": time.time(),
        "assigned_ts": None,
    })

    all_players = _get_players_for_game(game_id)
    n = len(all_players)
    round_number = state["round_number"]
    start_date = rnd.get("start_date", "TBD") if rnd else "TBD"

    _ephemeral(respond, f"You've joined Water Assassins! :droplet: Round {round_number} targets will be assigned at 8am on {start_date}.")
    respond({
        "response_type": "ephemeral",
        "text": "Water Assassins — How to Play",
        "blocks": _build_rules_blocks(),
    })

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


def _build_start_modal_blocks(num_rounds=1, round_values=None, participants=None):
    """Build blocks for the game setup modal.

    round_values  — list of {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}, used to
                    re-fill dates when the modal is updated via "Add Another Round".
    participants  — list of user IDs to pre-select in the players picker (reserved for future use).
    """
    participants_el = {
        "type": "multi_users_select",
        "action_id": "participants_select",
        "placeholder": {"type": "plain_text", "text": "Select players…"},
    }

    blocks = [
        {
            "type": "input",
            "block_id": "participants_block",
            "label": {"type": "plain_text", "text": "Players"},
            "hint": {
                "type": "plain_text",
                "text": "Pre-filled from channel members. Add or remove anyone before creating the game.",
            },
            "element": participants_el,
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "Schedule all rounds below. Each round starts at *8am* on its start date "
                    "and ends at *11:59pm* on its end date. "
                    "Once eliminated, a player cannot continue in later rounds."
                ),
            },
        },
        {"type": "divider"},
    ]

    for i in range(num_rounds):
        rnd_num = i + 1
        rv = (round_values[i] if round_values and i < len(round_values) else {}) or {}

        blocks.append({
            "type": "header",
            "text": {"type": "plain_text", "text": f"Round {rnd_num}"},
        })

        start_el = {
            "type": "datepicker",
            "action_id": f"round_{rnd_num}_start",
            "placeholder": {"type": "plain_text", "text": "Pick a date"},
        }
        if rv.get("start"):
            start_el["initial_date"] = rv["start"]

        blocks.append({
            "type": "input",
            "block_id": f"round_{rnd_num}_start_block",
            "label": {"type": "plain_text", "text": f"Round {rnd_num} Start Date"},
            "element": start_el,
        })

        end_el = {
            "type": "datepicker",
            "action_id": f"round_{rnd_num}_end",
            "placeholder": {"type": "plain_text", "text": "Pick a date (optional)"},
        }
        if rv.get("end"):
            end_el["initial_date"] = rv["end"]

        blocks.append({
            "type": "input",
            "block_id": f"round_{rnd_num}_end_block",
            "label": {"type": "plain_text", "text": f"Round {rnd_num} End Date"},
            "element": end_el,
            "optional": True,
        })

    blocks.append({"type": "divider"})
    blocks.append({
        "type": "actions",
        "block_id": "add_round_actions",
        "elements": [
            {
                "type": "button",
                "action_id": "assassin_add_round",
                "text": {"type": "plain_text", "text": "+ Add Another Round"},
                "value": str(num_rounds),
            }
        ],
    })

    return blocks


def _handle_start(body, client, respond):
    user_id = body["user_id"]
    state = _get_state()

    if state["status"] == "pending" and state.get("gm_id") != user_id:
        _ephemeral(respond, "A game is already pending. Only the current GM can update it.")
        return
    if state["status"] == "active":
        _ephemeral(respond, "A round is already in progress. End it first with `/assassin end`.")
        return
    if state["status"] == "round_ended":
        _ephemeral(respond, "A round just ended. Use `/assassin newround YYYY-MM-DD` to start the next round, or `/assassin endgame` to finish.")
        return

    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "assassin_start_modal",
            "title": {"type": "plain_text", "text": "New Game Setup"},
            "submit": {"type": "plain_text", "text": "Create Game"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "private_metadata": "1",
            "blocks": _build_start_modal_blocks(1),
        },
    )


def _handle_newround(body, client, respond):
    user_id = body["user_id"]
    text = (body.get("text") or "").strip()
    parts = text.split()
    date_str = parts[1] if len(parts) > 1 else ""
    end_date_str = parts[2] if len(parts) > 2 else ""

    state = _get_state()

    if state["status"] != "round_ended":
        if state["status"] == "active":
            _ephemeral(respond, "A round is in progress. End it first with `/assassin end`.")
        elif state["status"] == "none":
            _ephemeral(respond, "No game running. Start one with `/assassin start YYYY-MM-DD`.")
        elif state["status"] == "game_over":
            _ephemeral(respond, "The game is over. Start a new game with `/assassin start YYYY-MM-DD`.")
        else:
            _ephemeral(respond, f"Cannot start a new round right now (status: `{state['status']}`).")
        return

    if state.get("gm_id") != user_id:
        _ephemeral(respond, "Only the GM can start a new round.")
        return

    if not date_str:
        _ephemeral(respond, "Usage: `/assassin newround YYYY-MM-DD [end-YYYY-MM-DD]`")
        return

    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        _ephemeral(respond, f"Invalid date format `{date_str}`. Use YYYY-MM-DD.")
        return

    scheduled_end_ts = None
    if end_date_str:
        try:
            end_dt = datetime.strptime(end_date_str, "%Y-%m-%d")
        except ValueError:
            _ephemeral(respond, f"Invalid end date format `{end_date_str}`. Use YYYY-MM-DD.")
            return
        if end_dt <= dt:
            _ephemeral(respond, "End date must be after the start date.")
            return
        end_naive = end_dt.replace(hour=23, minute=59, second=59, microsecond=0)
        scheduled_end_ts = TIMEZONE.localize(end_naive).timestamp()

    start_naive = dt.replace(hour=8, minute=0, second=0, microsecond=0)
    start_aware = TIMEZONE.localize(start_naive)
    start_ts = start_aware.timestamp()

    if start_ts < time.time() - 3600:
        _ephemeral(respond, f"The date {date_str} is in the past.")
        return

    game_id = state["game_id"]
    alive = _get_alive_players(game_id)
    if len(alive) < 2:
        _ephemeral(respond, f"Not enough surviving players to start a new round ({len(alive)} alive). Use `/assassin endgame` to finish.")
        return

    new_round_number = state["round_number"] + 1

    db = _db()
    db.collection(COL_GAME).document("current").set({
        "status": "pending",
        "round_number": new_round_number,
    }, merge=True)

    db.collection(COL_ROUNDS).document(str(new_round_number)).set({
        "round_number": new_round_number,
        "status": "pending",
        "start_date": date_str,
        "start_ts": start_ts,
        "scheduled_end_date": end_date_str or None,
        "scheduled_end_ts": scheduled_end_ts,
        "end_ts": None,
        "player_order": [],
    })

    _set_state("pending", round_number=new_round_number, start_ts=start_ts,
               scheduled_end_ts=scheduled_end_ts)

    end_note = f" It will auto-end at 11:59pm on {end_date_str}." if end_date_str else ""
    _ephemeral(respond, f"Round {new_round_number} created! Targets assigned at 8am on {date_str}.{end_note} {len(alive)} players advance.")

    end_line = f" Ends automatically on *{end_date_str}*." if end_date_str else ""
    client.chat_postMessage(
        channel=ASSASSIN_CHANNEL_ID,
        text=f"Water Assassins Round {new_round_number} is opening! Targets assigned at 8am on {date_str}.",
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f":droplet: *Water Assassins — Round {new_round_number} is opening!*\n\n"
                        f"*{len(alive)} players* advance from Round {new_round_number - 1}. "
                        f"Targets will be re-randomized at *8am on {date_str}*.{end_line}\n\n"
                        "New players can still join with `/assassin join`!"
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

    round_number = state["round_number"]
    pending = _get_pending_report_for_reporter(user_id, round_number)
    if pending:
        _ephemeral(respond, "You already have a kill report pending GM validation. Wait for it to be resolved.")
        return

    target_id = player.get("target_id")
    if not target_id:
        _ephemeral(respond, "You have no assigned target.")
        return

    evidence_link = None
    evidence_ts = None
    try:
        history = client.conversations_history(channel=ASSASSIN_CHANNEL_ID, limit=50)
        for msg in history.get("messages", []):
            if msg.get("user") == user_id and msg.get("files"):
                evidence_link = msg["files"][0].get("permalink")
                evidence_ts = msg.get("ts")
                break
    except Exception:
        pass

    if not evidence_link:
        _ephemeral(respond, f"No evidence found. Post your video in <#{ASSASSIN_CHANNEL_ID}> first, then run `/assassin report`.")
        return

    already_used = list(
        _db().collection(COL_REPORTS)
        .where(filter=FieldFilter("round_number", "==", round_number))
        .where(filter=FieldFilter("evidence_link", "==", evidence_link))
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
            "private_metadata": f"{user_id}|{target_id}|{round_number}|{evidence_link}|{evidence_ts}",
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

    game_id = state["game_id"]
    player = _get_player(user_id)
    rnd = _get_round()

    if not player or player.get("game_id") != game_id:
        _ephemeral(respond, "You are not registered in the current game. Use `/assassin join` to join.")
        return

    status = player.get("status", "unknown")
    kills_this_round = player.get("kills_this_round", 0)
    total_kills = player.get("total_kills", 0)
    target_id = player.get("target_id")
    round_number = state["round_number"]

    if status == "pending":
        start_date = rnd.get("start_date", "TBD") if rnd else "TBD"
        msg = (
            f"*Status:* Registered, waiting for Round {round_number} to start "
            f"(targets assigned at 8am on {start_date})\n"
            f"*Total kills:* {total_kills}"
        )
    elif status == "alive":
        target_line = f"*Current target:* <@{target_id}>" if target_id else "*Current target:* None"
        end_date = rnd.get("scheduled_end_date") if rnd else None
        end_line = f"\n*Round ends:* {end_date}" if end_date else ""
        msg = (
            f"*Status:* Alive :large_green_circle: (Round {round_number})\n"
            f"{target_line}\n"
            f"*Kills this round:* {kills_this_round}\n"
            f"*Total kills:* {total_kills}"
            f"{end_line}"
        )
    elif status == "eliminated":
        killer = player.get("killed_by")
        killer_line = f" by <@{killer}>" if killer else ""
        elim_round = player.get("eliminated_in_round", "?")
        msg = f"*Status:* Eliminated :red_circle:{killer_line} (Round {elim_round})\n*Total kills:* {total_kills}"
    else:
        msg = f"*Status:* {status}\n*Total kills:* {total_kills}"

    _ephemeral(respond, msg)


def _handle_leaderboard(body, client, respond):
    state = _get_state()

    if state["status"] == "none":
        _ephemeral(respond, "No Water Assassins game data available.")
        return

    game_id = state["game_id"]
    all_players = _get_players_for_game(game_id)
    sorted_players = sorted(all_players, key=lambda p: p.get("total_kills", 0), reverse=True)

    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for idx, p in enumerate(sorted_players):
        medal = medals[idx] if idx < 3 else f"{idx + 1}."
        status_icon = ":large_green_circle:" if p.get("status") == "alive" else ":red_circle:"
        lines.append(f"{medal} {status_icon} <@{p['user_id']}> — {p.get('total_kills', 0)} kill(s) total")

    text = "\n".join(lines) if lines else "No players registered yet."
    _ephemeral(respond, f":droplet: *Water Assassins — Leaderboard*\n\n{text}")


def _build_rules_blocks():
    state = _get_state()
    rnd = _get_round()
    round_number = state.get("round_number", 1)

    if state["status"] == "pending" and rnd:
        join_line = (
            f":pencil: *Sign-ups are open for Round {round_number}!* "
            f"Run `/assassin join` to enter. Targets assigned at *8am on {rnd.get('start_date', 'TBD')}*."
        )
    elif state["status"] == "active":
        end_date = rnd.get("scheduled_end_date") if rnd else None
        end_note = f" Ends *{end_date}*." if end_date else ""
        join_line = f":lock: Round {round_number} is currently in progress.{end_note}"
    elif state["status"] == "round_ended":
        join_line = f":hourglass: Round {round_number} has ended. Waiting for the GM to open the next round."
    elif state["status"] == "game_over":
        join_line = ":checkered_flag: The game has ended. A GM will announce the next game."
    else:
        join_line = ":hourglass: No round is open yet. A GM will announce when sign-ups begin."

    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "💧 Water Assassins"},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "A social elimination game played across multiple rounds. Every player is secretly assigned a target. "
                    "Hunt them down with a *sock* — but watch your back, because someone is hunting *you*."
                ),
            },
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "*How it works:*\n"
                    ":one:  Sign up with `/assassin join` before the round starts.\n"
                    ":two:  At *8am on the start date*, you'll receive a DM with your target's name.\n"
                    ":three:  Hit your target with a *sock* and record it. Post the video in this channel.\n"
                    ":four:  Run `/assassin report` — the bot will attach your video and send it to the GM for review.\n"
                    ":five:  Once the GM validates your kill, you inherit your target's target and keep hunting.\n"
                    ":six:  Each round, targets are re-randomized among surviving players. Last one standing wins!"
                ),
            },
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "*Elimination rules:*\n"
                    "• You are eliminated if your hunter hits you with a sock *and* the GM validates their recording.\n"
                    "• You are also eliminated at the end of each round if you haven't scored *at least one kill*.\n"
                    "• Once eliminated in any round, you cannot rejoin the game."
                ),
            },
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "*Commands:*\n"
                    "• `/assassin join` — join the current game\n"
                    "• `/assassin report` — report a kill (post your video here first)\n"
                    "• `/assassin status` — check your target and kill count\n"
                    "• `/assassin players` — see who's still alive\n"
                    "• `/assassin leaderboard` — view the kill leaderboard"
                ),
            },
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": join_line},
        },
    ]


def _handle_rules(body, client, respond):
    respond({
        "response_type": "ephemeral",
        "text": "Water Assassins — How to Play",
        "blocks": _build_rules_blocks(),
    })


def _handle_add(body, client, respond):
    gm_id = body["user_id"]
    state = _get_state()

    if state["status"] != "pending":
        if state["status"] == "active":
            _ephemeral(respond, "The round is already in progress — players can't be added after targets are assigned.")
        else:
            _ephemeral(respond, "No pending round. Create one first with `/assassin start YYYY-MM-DD`.")
        return

    if state.get("gm_id") != gm_id:
        _ephemeral(respond, "Only the GM can add players.")
        return

    rnd = _get_round()
    game_id = state["game_id"]
    round_number = state["round_number"]

    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "assassin_add_players_modal",
            "title": {"type": "plain_text", "text": "Add Players"},
            "submit": {"type": "plain_text", "text": "Add"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "private_metadata": game_id,
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f":droplet: *Adding players to Round {round_number} (starting {rnd.get('start_date', 'TBD') if rnd else 'TBD'}).*\nPlayers already registered or previously eliminated will be skipped.",
                    },
                },
                {
                    "type": "input",
                    "block_id": "players_block",
                    "label": {"type": "plain_text", "text": "Select players to add"},
                    "element": {
                        "type": "multi_users_select",
                        "action_id": "players_select",
                        "placeholder": {"type": "plain_text", "text": "Choose people…"},
                    },
                },
            ],
        },
    )


def _handle_eliminate(body, client, respond):
    gm_id = body["user_id"]
    state = _get_state()

    if state["status"] != "active":
        _ephemeral(respond, "There is no active round.")
        return

    if state.get("gm_id") != gm_id:
        _ephemeral(respond, "Only the GM can manually eliminate players.")
        return

    game_id = state["game_id"]
    round_number = state["round_number"]
    alive = _get_alive_players(game_id)

    if not alive:
        _ephemeral(respond, "No alive players to eliminate.")
        return

    options = []
    for p in alive:
        uid = p["user_id"]
        if uid.startswith("UBOT"):
            name = uid
        else:
            try:
                info = client.users_info(user=uid)
                profile = info["user"].get("profile", {})
                name = (
                    profile.get("real_name")
                    or profile.get("display_name")
                    or info["user"].get("real_name")
                    or info["user"].get("name")
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
            "private_metadata": f"{game_id}|{round_number}",
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


def _do_eliminate(client, gm_id, target_id, game_id, round_number):
    """Shared elimination logic used by modal submit and debug commands."""
    target = _get_player(target_id)
    if not target or target.get("game_id") != game_id:
        return False, f"<@{target_id}> is not a registered player in the current game."
    if target.get("status") != "alive":
        return False, f"<@{target_id}> is already eliminated."

    db = _db()
    batch = db.batch()

    batch.set(db.collection(COL_PLAYERS).document(target_id), {
        "status": "eliminated",
        "killed_by": gm_id,
        "eliminated_in_round": round_number,
    }, merge=True)

    new_target_id = target.get("target_id")
    alive_players = _get_alive_players(game_id)
    for p in alive_players:
        if p.get("target_id") == target_id:
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
        _dm(client, target_id, text="You have been manually eliminated from the game by the GM.")
    except Exception:
        pass

    alive = _get_alive_players(game_id)
    if len(alive) <= 1:
        _end_round(client, reason="last_standing")

    return True, f"<@{target_id}> has been eliminated."


def _handle_end(body, client, respond):
    user_id = body["user_id"]
    text = (body.get("text") or "").strip()
    parts = text.split()
    end_date_str = parts[1] if len(parts) > 1 else ""

    state = _get_state()

    if state["status"] != "active":
        _ephemeral(respond, "There is no active round to end.")
        return

    if state.get("gm_id") != user_id:
        _ephemeral(respond, "Only the GM can end the round.")
        return

    if end_date_str:
        try:
            end_dt = datetime.strptime(end_date_str, "%Y-%m-%d")
        except ValueError:
            _ephemeral(respond, f"Invalid date format `{end_date_str}`. Use YYYY-MM-DD.")
            return

        end_naive = end_dt.replace(hour=23, minute=59, second=59, microsecond=0)
        scheduled_end_ts = TIMEZONE.localize(end_naive).timestamp()

        if scheduled_end_ts < time.time():
            _ephemeral(respond, f"The date {end_date_str} is in the past.")
            return

        round_number = state["round_number"]
        _db().collection(COL_ROUNDS).document(str(round_number)).set({
            "scheduled_end_date": end_date_str,
            "scheduled_end_ts": scheduled_end_ts,
        }, merge=True)
        _set_state("active", scheduled_end_ts=scheduled_end_ts)

        _ephemeral(respond, f"Scheduled: the round will automatically end at 11:59pm on {end_date_str}.")
        client.chat_postMessage(
            channel=ASSASSIN_CHANNEL_ID,
            text=f"The Water Assassins round has been scheduled to end on {end_date_str}.",
            blocks=[{
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f":droplet: The Water Assassins round will automatically end on *{end_date_str}* at 11:59pm. Make your moves!",
                },
            }],
        )
        return

    _ephemeral(respond, "Ending the round...")

    def run():
        _end_round(client, reason="gm_ended")

    threading.Thread(target=run, daemon=True).start()


def _handle_endgame(body, client, respond):
    user_id = body["user_id"]
    state = _get_state()

    if state["status"] not in ("round_ended", "active", "pending"):
        _ephemeral(respond, "No active game to end.")
        return

    if state.get("gm_id") != user_id:
        _ephemeral(respond, "Only the GM can end the game.")
        return

    if state["status"] == "active":
        _end_round(client, reason="gm_ended")

    _db().collection(COL_GAME).document("current").set({
        "status": "game_over",
    }, merge=True)

    with _state_lock:
        _game_state["status"] = "game_over"

    _ephemeral(respond, "The game has been ended.")
    client.chat_postMessage(
        channel=ASSASSIN_CHANNEL_ID,
        text="The Water Assassins game has ended!",
        blocks=[{
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": ":droplet: *Water Assassins — Game Over!* The game has been ended by the GM. Thanks for playing!",
            },
        }],
    )


def _handle_leave(body, client, respond):
    user_id = body["user_id"]
    state = _get_state()

    if state["status"] != "pending":
        if state["status"] == "active":
            _ephemeral(respond, "The round is already in progress — you cannot leave once targets have been assigned.")
        else:
            _ephemeral(respond, "There is no pending round to leave.")
        return

    game_id = state["game_id"]
    player = _get_player(user_id)
    if not player or player.get("game_id") != game_id:
        _ephemeral(respond, "You are not registered in the current game.")
        return

    _db().collection(COL_PLAYERS).document(user_id).delete()

    all_players = _get_players_for_game(game_id)
    n = len(all_players)

    _ephemeral(respond, "You have left the Water Assassins game.")

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

    if state["status"] == "none":
        _ephemeral(respond, "No active game.")
        return

    game_id = state["game_id"]
    all_players = _get_players_for_game(game_id)
    alive = [p for p in all_players if p.get("status") == "alive"]
    pending = [p for p in all_players if p.get("status") == "pending"]
    eliminated = [p for p in all_players if p.get("status") == "eliminated"]

    if not all_players:
        _ephemeral(respond, "No players registered yet.")
        return

    lines = []
    if alive:
        lines.append(f"*Alive ({len(alive)}):*")
        lines.extend(
            f"• <@{p['user_id']}> — {p.get('kills_this_round', 0)} kill(s) this round, {p.get('total_kills', 0)} total"
            for p in alive
        )
    if pending:
        lines.append("*Signed up (waiting for round start):*")
        lines.extend(f"• <@{p['user_id']}>" for p in pending)
    if eliminated:
        lines.append(f"*Eliminated ({len(eliminated)}):*")
        lines.extend(
            f"• <@{p['user_id']}> — eliminated Round {p.get('eliminated_in_round', '?')}"
            for p in eliminated
        )

    _ephemeral(respond, "\n".join(lines) if lines else "No active players.")


# ---------------------------------------------------------------------------
# Debug handlers
# ---------------------------------------------------------------------------

def _handle_debug(body, client, respond, raw_text):
    """
    Debug subcommands (ephemeral only):
      /assassin debug state        — dump game + round + all players + pending reports
      /assassin debug assign       — force target assignment right now (skip 8am)
      /assassin debug kill         — auto-validate a kill for you against your current target
      /assassin debug reset        — wipe all Firestore game data and reset in-memory state
      /assassin debug addbot [n]   — add n fake bot players to the pending round (default 2)
      /assassin debug safezone     — immediately trigger a safe zone right now
    """
    parts = raw_text.lower().split()
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
    elif subcmd == "safezone":
        _debug_safezone(client, respond)
    elif subcmd == "echo":
        _ephemeral(respond, f"raw text: `{raw_text}`")
    else:
        _ephemeral(respond, (
            "*Debug commands:*\n"
            "• `/assassin debug state` — dump current game, round, players, and pending reports\n"
            "• `/assassin debug assign` — force target assignment now (skips 8am)\n"
            "• `/assassin debug kill` — instantly validate your kill against your current target\n"
            "• `/assassin debug reset` — wipe all game data and reset state\n"
            "• `/assassin debug addbot [n]` — add n fake bot players to the pending round (default 2)\n"
            "• `/assassin debug safezone` — immediately trigger a safe zone right now"
        ))


def _debug_state(respond):
    game = _get_game()
    state = _get_state()
    rnd = _get_round()

    if not game:
        _ephemeral(respond, "No game document found in Firestore.")
        return

    lines = [
        "*— Game —*",
        f"game_id: `{game.get('game_id')}`",
        f"status: `{game.get('status')}`",
        f"gm_id: <@{game['gm_id']}>" if game.get('gm_id') else "gm_id: none",
        f"round_number: `{game.get('round_number')}`",
        f"in-memory status: `{state['status']}`",
    ]

    if rnd:
        lines += [
            "",
            f"*— Round {rnd.get('round_number')} —*",
            f"status: `{rnd.get('status')}`",
            f"start_date: `{rnd.get('start_date')}`",
            f"start_ts: `{rnd.get('start_ts')}`",
            f"scheduled_end_date: `{rnd.get('scheduled_end_date') or 'none'}`",
            f"scheduled_end_ts: `{rnd.get('scheduled_end_ts') or 'none'}`",
        ]

    lines += ["", "*— Players —*"]
    game_id = game.get("game_id", "")
    players = _get_players_for_game(game_id)
    if not players:
        lines.append("(none)")
    for p in players:
        target = f"→ <@{p['target_id']}>" if p.get("target_id") else "→ none"
        lines.append(
            f"<@{p['user_id']}> [{p.get('status')}] {target}  "
            f"kills_round={p.get('kills_this_round', 0)}  total={p.get('total_kills', 0)}"
        )

    lines += ["", "*— Pending kill reports —*"]
    round_number = state.get("round_number", 0)
    reports = list(
        _db().collection(COL_REPORTS)
        .where(filter=FieldFilter("round_number", "==", round_number))
        .where(filter=FieldFilter("status", "==", "pending"))
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

    if state["status"] == "round_ended":
        next_rnd_num = state["round_number"] + 1
        next_rnd = _get_round(next_rnd_num)
        if next_rnd and next_rnd.get("status") == "scheduled":
            _ephemeral(respond, f"Advancing to round {next_rnd_num} and assigning targets…")
            _advance_to_round(client, next_rnd_num, next_rnd)
            _ephemeral(respond, "Done. Check your DMs for your target.")
        else:
            _ephemeral(respond, "No scheduled next round found. Use `/assassin newround YYYY-MM-DD` to start one manually.")
        return

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
    game_id = state["game_id"]
    round_number = state["round_number"]

    db = _db()
    batch = db.batch()
    batch.set(db.collection(COL_PLAYERS).document(target_id), {
        "status": "eliminated",
        "killed_by": user_id,
        "eliminated_in_round": round_number,
    }, merge=True)
    batch.set(db.collection(COL_PLAYERS).document(user_id), {
        "kills_this_round": firestore.Increment(1),
        "total_kills": firestore.Increment(1),
        "target_id": new_target_id,
    }, merge=True)
    batch.commit()

    alive = _get_alive_players(game_id)
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

    game_id = state["game_id"]
    existing = [
        p["user_id"] for p in _get_players_for_game(game_id)
        if p["user_id"].startswith("UBOT")
    ]
    start_index = len(existing) + 1

    db = _db()
    added = []
    for i in range(start_index, start_index + n):
        bot_id = f"UBOT{i:03d}"
        db.collection(COL_PLAYERS).document(bot_id).set({
            "user_id": bot_id,
            "game_id": game_id,
            "status": "pending",
            "target_id": None,
            "kills_this_round": 0,
            "total_kills": 0,
            "killed_by": None,
            "eliminated_in_round": None,
            "joined_ts": time.time(),
            "assigned_ts": None,
        })
        added.append(bot_id)

    total = len(_get_players_for_game(game_id))
    bot_list = ", ".join(f"`{b}`" for b in added)
    _ephemeral(respond, f"[DEBUG] Added {n} bot(s): {bot_list}\nTotal players in game: {total}")


def _debug_reset(respond):
    db = _db()

    db.collection(COL_GAME).document("current").delete()

    for doc in db.collection(COL_ROUNDS).stream():
        doc.reference.delete()

    for doc in db.collection(COL_PLAYERS).stream():
        doc.reference.delete()

    for doc in db.collection(COL_REPORTS).stream():
        doc.reference.delete()

    with _state_lock:
        _game_state["status"] = "none"
        _game_state["game_id"] = None
        _game_state["gm_id"] = None
        _game_state["round_number"] = 0
        _game_state["start_ts"] = None
        _game_state["scheduled_end_ts"] = None

    _ephemeral(respond, "[DEBUG] All game data wiped. State reset to `none`.")


def _debug_safezone(client, respond):
    state = _get_state()

    if state["status"] != "active":
        _ephemeral(respond, f"[DEBUG] Round is not active (status: `{state['status']}`). Cannot trigger safe zone.")
        return

    if _is_last_round():
        _ephemeral(respond, "[DEBUG] This is the last round — safe zones are disabled.")
        return

    if state.get("safe_zone_expires_ts"):
        location = state.get("safe_zone_location", "unknown")
        _ephemeral(respond, f"[DEBUG] A safe zone is already active at *{location}*.")
        return

    location = random.choice(SAFE_ZONE_LOCATIONS)
    with _state_lock:
        _game_state["safe_zone_next_ts"] = None
        _game_state["safe_zone_warning_ts"] = None
        _game_state["safe_zone_warning_sent"] = False
        _game_state["safe_zone_location"] = location

    _activate_safe_zone(client, location)
    _ephemeral(respond, f"[DEBUG] Safe zone forced at *{location}*.")


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
                elif sub == "newround":
                    _handle_newround(body, client, respond)
                elif sub == "add":
                    _handle_add(body, client, respond)
                elif sub == "report":
                    _handle_report(body, client, respond)
                elif sub == "status":
                    _handle_status(body, client, respond)
                elif sub == "leaderboard":
                    _handle_leaderboard(body, client, respond)
                elif sub == "end":
                    _handle_end(body, client, respond)
                elif sub == "endgame":
                    _handle_endgame(body, client, respond)
                elif sub == "eliminate":
                    _handle_eliminate(body, client, respond)
                elif sub == "rules":
                    _handle_rules(body, client, respond)
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
                            "• `/assassin rules` — how-to-play info\n"
                            "• `/assassin join` — join the current game\n"
                            "• `/assassin leave` — leave before the round starts\n"
                            "• `/assassin start` — (GM) open a form to create a new game and schedule all rounds\n"
                            "• `/assassin newround YYYY-MM-DD [end-YYYY-MM-DD]` — (GM) manually start an unplanned next round\n"
                            "• `/assassin end` — (GM) end the current round immediately\n"
                            "• `/assassin end YYYY-MM-DD` — (GM) schedule the round to end on a date\n"
                            "• `/assassin endgame` — (GM) fully end the game\n"
                            "• `/assassin add` — (GM) add players to the pending round\n"
                            "• `/assassin report` — report a kill (GM validates)\n"
                            "• `/assassin status` — view your current status & target\n"
                            "• `/assassin players` — list all players and their status\n"
                            "• `/assassin leaderboard` — view the kill leaderboard"
                        ),
                    })
            except Exception as e:
                logger.exception(f"[Assassins] cmd error: {e}")

        if sub in ("report", "eliminate", "add", "start"):
            run()
        else:
            threading.Thread(target=run, daemon=True).start()

    @app.action("assassin_add_round")
    def action_add_round(ack, body, client, logger):
        ack()
        try:
            view = body["view"]
            num_rounds = int(view.get("private_metadata") or "1")
            state_values = view["state"]["values"]

            # Preserve participants selection
            participants = (state_values.get("participants_block") or {}).get(
                "participants_select", {}
            ).get("selected_users") or []

            # Preserve round dates already filled in
            round_values = []
            for i in range(1, num_rounds + 1):
                start = (state_values.get(f"round_{i}_start_block") or {}).get(
                    f"round_{i}_start", {}
                ).get("selected_date")
                end = (state_values.get(f"round_{i}_end_block") or {}).get(
                    f"round_{i}_end", {}
                ).get("selected_date")
                round_values.append({"start": start, "end": end})

            new_num = num_rounds + 1
            client.views_update(
                view_id=view["id"],
                view={
                    "type": "modal",
                    "callback_id": "assassin_start_modal",
                    "title": {"type": "plain_text", "text": "New Game Setup"},
                    "submit": {"type": "plain_text", "text": "Create Game"},
                    "close": {"type": "plain_text", "text": "Cancel"},
                    "private_metadata": str(new_num),
                    "blocks": _build_start_modal_blocks(new_num, round_values, participants),
                },
            )
        except Exception as e:
            logger.exception(f"[Assassins] action_add_round error: {e}")

    @app.view("assassin_start_modal")
    def view_start_game(ack, body, client, logger):
        num_rounds = int(body["view"].get("private_metadata") or "1")
        state_values = body["view"]["state"]["values"]

        # --- Validate participants ---
        selected_users = (state_values.get("participants_block") or {}).get(
            "participants_select", {}
        ).get("selected_users") or []

        errors = {}
        if not selected_users:
            errors["participants_block"] = "Select at least one player."

        # --- Validate round dates ---
        rounds = []
        for i in range(1, num_rounds + 1):
            start_date = (state_values.get(f"round_{i}_start_block") or {}).get(
                f"round_{i}_start", {}
            ).get("selected_date")
            end_date = (state_values.get(f"round_{i}_end_block") or {}).get(
                f"round_{i}_end", {}
            ).get("selected_date")

            if not start_date:
                errors[f"round_{i}_start_block"] = f"Round {i} start date is required."
                continue

            dt = datetime.strptime(start_date, "%Y-%m-%d")
            start_naive = dt.replace(hour=8, minute=0, second=0, microsecond=0)
            start_ts = TIMEZONE.localize(start_naive).timestamp()

            if start_ts < time.time() - 3600:
                errors[f"round_{i}_start_block"] = f"Round {i} start date is in the past."
                continue

            scheduled_end_ts = None
            if end_date:
                end_dt = datetime.strptime(end_date, "%Y-%m-%d")
                if end_dt <= dt:
                    errors[f"round_{i}_end_block"] = (
                        f"Round {i} end date must be after its start date."
                    )
                    continue
                end_naive = end_dt.replace(hour=23, minute=59, second=59, microsecond=0)
                scheduled_end_ts = TIMEZONE.localize(end_naive).timestamp()

            rounds.append({
                "round_number": i,
                "start_date": start_date,
                "start_ts": start_ts,
                "end_date": end_date,
                "scheduled_end_ts": scheduled_end_ts,
            })

        if not rounds and "round_1_start_block" not in errors:
            errors["round_1_start_block"] = "At least one round with a start date is required."

        cur_state = _get_state()
        if cur_state["status"] == "active":
            errors["round_1_start_block"] = "A round is already in progress. End it first."

        if errors:
            ack(response_action="errors", errors=errors)
            return

        ack()

        def run():
            try:
                user_id = body["user"]["id"]
                game_id = str(uuid.uuid4())
                db = _db()

                db.collection(COL_GAME).document("current").set({
                    "game_id": game_id,
                    "status": "pending",
                    "gm_id": user_id,
                    "round_number": 1,
                    "total_rounds": len(rounds),
                    "created_ts": time.time(),
                })

                for rnd in rounds:
                    status = "pending" if rnd["round_number"] == 1 else "scheduled"
                    db.collection(COL_ROUNDS).document(str(rnd["round_number"])).set({
                        "round_number": rnd["round_number"],
                        "status": status,
                        "start_date": rnd["start_date"],
                        "start_ts": rnd["start_ts"],
                        "scheduled_end_date": rnd["end_date"],
                        "scheduled_end_ts": rnd["scheduled_end_ts"],
                        "end_ts": None,
                        "player_order": [],
                    })

                now = time.time()
                for uid in selected_users:
                    db.collection(COL_PLAYERS).document(uid).set({
                        "user_id": uid,
                        "game_id": game_id,
                        "status": "pending",
                        "target_id": None,
                        "kills_this_round": 0,
                        "total_kills": 0,
                        "killed_by": None,
                        "eliminated_in_round": None,
                        "joined_ts": now,
                        "assigned_ts": None,
                    })

                _set_state(
                    "pending",
                    game_id=game_id,
                    gm_id=user_id,
                    round_number=1,
                    start_ts=rounds[0]["start_ts"],
                    scheduled_end_ts=rounds[0].get("scheduled_end_ts"),
                )

                schedule_lines = []
                for rnd in rounds:
                    line = f"• *Round {rnd['round_number']}:* starts {rnd['start_date']}"
                    if rnd.get("end_date"):
                        line += f", ends {rnd['end_date']}"
                    schedule_lines.append(line)

                total_rounds = len(rounds)
                schedule_text = "\n".join(schedule_lines)
                player_mentions = " ".join(f"<@{uid}>" for uid in selected_users)

                client.chat_postMessage(
                    channel=ASSASSIN_CHANNEL_ID,
                    text=(
                        f"A new {total_rounds}-round Water Assassins game has been created "
                        f"by <@{user_id}>!"
                    ),
                    blocks=[
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": (
                                    ":droplet: *A new Water Assassins game is starting!*\n\n"
                                    f"Created by <@{user_id}>. *{total_rounds} round(s)* planned.\n\n"
                                    f"{schedule_text}\n\n"
                                    f"*Players ({len(selected_users)}):* {player_mentions}"
                                ),
                            },
                        }
                    ],
                )
            except Exception as e:
                logger.exception(f"[Assassins] view_start_game error: {e}")

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
                reporter_id = parts[0]
                target_id = parts[1]
                round_number = int(parts[2])
                evidence_link = parts[3] if len(parts) > 3 and parts[3] != "None" else None
                evidence_ts = parts[4] if len(parts) > 4 and parts[4] != "None" else None

                values = body["view"]["state"]["values"]
                checked = values.get("confirmation_block", {}).get("confirmation_check", {}).get("selected_options", [])
                if not checked:
                    return

                state = _get_state()
                game_id = state.get("game_id")

                report_id = str(uuid.uuid4())
                _db().collection(COL_REPORTS).document(report_id).set({
                    "report_id": report_id,
                    "round_number": round_number,
                    "game_id": game_id,
                    "reporter_id": reporter_id,
                    "target_id": target_id,
                    "status": "pending",
                    "evidence_link": evidence_link,
                    "evidence_ts": evidence_ts,
                    "gm_dm_channel": None,
                    "gm_dm_ts": None,
                    "created_ts": time.time(),
                    "resolved_ts": None,
                })

                gm_id = state.get("gm_id")
                if not gm_id:
                    return

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
                                    f":droplet: *Kill Report — Round {round_number}*\n\n"
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

                _db().collection(COL_REPORTS).document(report_id).set({
                    "gm_dm_channel": gm_channel,
                    "gm_dm_ts": msg["ts"],
                }, merge=True)

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
                round_number = report["round_number"]
                game_id = state.get("game_id")

                target_player = _get_player(target_id)
                if not target_player or target_player.get("status") != "alive":
                    client.chat_postEphemeral(
                        channel=body["container"]["channel_id"],
                        user=validator_id,
                        text=f"<@{target_id}> is already eliminated. Cannot validate.",
                    )
                    return

                new_target_id = target_player.get("target_id")

                db = _db()
                batch = db.batch()

                batch.set(db.collection(COL_REPORTS).document(report_id), {
                    "status": "validated",
                    "resolved_ts": time.time(),
                }, merge=True)

                batch.set(db.collection(COL_PLAYERS).document(target_id), {
                    "status": "eliminated",
                    "killed_by": reporter_id,
                    "eliminated_in_round": round_number,
                }, merge=True)

                batch.set(db.collection(COL_PLAYERS).document(reporter_id), {
                    "kills_this_round": firestore.Increment(1),
                    "total_kills": firestore.Increment(1),
                    "target_id": new_target_id,
                }, merge=True)

                batch.commit()

                evidence_ts = report.get("evidence_ts")
                if evidence_ts:
                    try:
                        client.reactions_add(
                            channel=ASSASSIN_CHANNEL_ID,
                            name="knife",
                            timestamp=evidence_ts,
                        )
                    except Exception:
                        pass

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

                try:
                    _dm(client, target_id, blocks=[
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": (
                                    f":red_circle: *You have been eliminated* by <@{reporter_id}>.\n\n"
                                    "You're out of the game. Better luck next time!"
                                ),
                            },
                        }
                    ])
                except Exception:
                    pass

                alive = _get_alive_players(game_id)
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

                evidence_ts = report.get("evidence_ts")
                if evidence_ts:
                    try:
                        client.reactions_add(
                            channel=ASSASSIN_CHANNEL_ID,
                            name="no_good",
                            timestamp=evidence_ts,
                        )
                    except Exception:
                        pass

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
                metadata = body["view"]["private_metadata"]
                parts = metadata.split("|")
                game_id = parts[0]
                round_number = int(parts[1]) if len(parts) > 1 else _get_state()["round_number"]
                values = body["view"]["state"]["values"]
                target_id = values["player_block"]["player_select"]["selected_option"]["value"]

                ok, msg = _do_eliminate(client, gm_id, target_id, game_id, round_number)
                if not ok:
                    logger.warning(f"[Assassins] eliminate modal: {msg}")
            except Exception as e:
                logger.exception(f"[Assassins] view_eliminate error: {e}")

        threading.Thread(target=run, daemon=True).start()

    @app.view("assassin_add_players_modal")
    def view_add_players(ack, body, client, logger):
        ack()

        def run():
            try:
                game_id = body["view"]["private_metadata"]
                values = body["view"]["state"]["values"]
                selected_ids = values["players_block"]["players_select"].get("selected_users") or []

                game = _get_game()
                if not game or game.get("game_id") != game_id or game.get("status") != "pending":
                    return

                existing_players = _get_players_for_game(game_id)
                existing_ids = {p["user_id"] for p in existing_players}
                eliminated_ids = {p["user_id"] for p in existing_players if p.get("status") == "eliminated"}
                added = []
                skipped = []

                for uid in selected_ids:
                    if uid in eliminated_ids:
                        skipped.append(uid)
                        continue
                    if uid in existing_ids:
                        skipped.append(uid)
                        continue
                    _db().collection(COL_PLAYERS).document(uid).set({
                        "user_id": uid,
                        "game_id": game_id,
                        "status": "pending",
                        "target_id": None,
                        "kills_this_round": 0,
                        "total_kills": 0,
                        "killed_by": None,
                        "eliminated_in_round": None,
                        "joined_ts": time.time(),
                        "assigned_ts": None,
                    })
                    added.append(uid)

                if not added:
                    return

                total = len(_get_players_for_game(game_id))
                added_mentions = ", ".join(f"<@{uid}>" for uid in added)
                client.chat_postMessage(
                    channel=ASSASSIN_CHANNEL_ID,
                    text=f"{added_mentions} have been added to the Water Assassins game.",
                    blocks=[{
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f":droplet: {added_mentions} {'has' if len(added) == 1 else 'have'} been added to the hunt! *{total} player(s)* registered so far.",
                        },
                    }],
                )
            except Exception as e:
                logger.exception(f"[Assassins] view_add_players error: {e}")

        threading.Thread(target=run, daemon=True).start()

    @app.event("member_joined_channel")
    def handle_assassins_channel_join(event, client, logger):
        if event.get("channel") != ASSASSIN_CHANNEL_ID:
            return

        def run():
            try:
                user_id = event.get("user")
                client.chat_postEphemeral(
                    channel=ASSASSIN_CHANNEL_ID,
                    user=user_id,
                    text="Welcome to Water Assassins!",
                    blocks=_build_rules_blocks(),
                )
            except Exception as e:
                logger.exception(f"[Assassins] channel join rules error: {e}")

        threading.Thread(target=run, daemon=True).start()

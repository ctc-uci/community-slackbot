"""Study location bot: share where you're studying, tag others, cancel, list who's studying."""
import json
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
import requests

from slack_sdk import WebClient
import pytz
from firebase_admin import firestore
from firebase_client import get_firebase_app

# Channel where announcements are posted
# Use TEST_CHANNEL_ID if ENV is "development", otherwise use the hardcoded channel

# STUDY_CHANNEL_ID = "C0ACQP6P3T2"
STUDY_CHANNEL_ID = os.environ.get("TEST_CHANNEL_ID")
if STUDY_CHANNEL_ID == 'C0ABVSK5QH0':
    print("TEST_CHANNEL_ID is set")
else:
    print("TEST_CHANNEL_ID is not set")


# Set your timezone here (e.g., 'America/Los_Angeles', 'America/New_York', 'America/Chicago')
TIMEZONE = pytz.timezone(os.environ.get("TZ", "America/Los_Angeles"))

UCI_LOCATIONS = [
    "Science Library",
    "CSL",
    "ALP",
    "Gateway Study Center",
    "Langson Library",
    "Student Center",
    "DBH",
    "ISEB",
    "Humanities Gateway 2nd Floor (Ryan's Secret Spot)",
    "Engineering Quad",
    "Other",
]

# session_id -> { user_id, user_name, location, end_ts, ... }
active_sessions = {}

# user_id -> list of { expired_at, session } for sessions that expired today
expired_today = {}

def _db():
    get_firebase_app()
    return firestore.client()

def _add_study_seconds(user_id, seconds):
    """Atomically add seconds to a user's study_hours document."""
    if seconds <= 0:
        return
    _db().collection("study_hours").document(user_id).set(
        {"total_seconds": firestore.Increment(seconds)},
        merge=True,
    )

def _get_all_study_hours():
    """Return {user_id: total_seconds} for all users."""
    return {doc.id: doc.to_dict().get("total_seconds", 0) for doc in _db().collection("study_hours").stream()}

VIBE_LABELS = {
    "lock_in": "🟥 Lock In",
    "chill_vibes": "🟦 Chill Vibes",
}

def _build_announcement_text(session):
    names = session["participants"]
    with_suffix = ""
    if len(names) > 1:
        with_suffix = " with " + " ".join(f"<@{uid}>" for uid in names[1:])

    return (
        f"📍 <@{names[0]}> is studying at *{session['location']}*"
        f"{with_suffix} *{session['time_range']}*."
    )

def _meta_context_block(session):
    """Return a context block with vibe and/or capacity info, or None if neither is set."""
    elements = []
    vibe = session.get("vibe")
    if vibe in VIBE_LABELS:
        elements.append({"type": "mrkdwn", "text": VIBE_LABELS[vibe]})
    capacity = session.get("capacity")
    if capacity:
        taken = len(session.get("participants", []))
        elements.append({"type": "mrkdwn", "text": f"👥 {taken}/{capacity} spots"})
    if not elements:
        return None
    return {"type": "context", "elements": elements}

def _build_full_text(session):
    base = _build_announcement_text(session)
    if session.get("description"):
        base += f"\n_{session['description']}_"
    if session.get("image_url"):
        base += "\nPhoto attached"
    return base

def _clean_expired_sessions(client=None):
    """Remove expired sessions and send 5-min reminders if client is provided."""
    now = time.time()
    today = datetime.now(TIMEZONE).date()
    expired = [sid for sid, s in active_sessions.items() if s["end_ts"] < now]

    # Remove expired sessions
    for sid in expired:
        s = active_sessions[sid]
        user_id = s["user_id"]
        # Save to expired_today if it expired today (for /study-edit reactivation)
        expired_at_dt = datetime.fromtimestamp(s["end_ts"], tz=TIMEZONE)
        if expired_at_dt.date() == today:
            if user_id not in expired_today:
                expired_today[user_id] = []
            expired_today[user_id].append({"expired_at": s["end_ts"], "session": dict(s)})
        # Accumulate studied time
        if s.get("created_ts"):
            _add_study_seconds(user_id, s["end_ts"] - s["created_ts"])
        channel_id = s.get("channel_id")
        message_ts = s.get("message_ts")
        if client and channel_id and message_ts:
            try:
                client.pins_remove(channel=channel_id, timestamp=message_ts)
            except Exception:
                pass
        del active_sessions[sid]

    # Clean up expired_today entries from previous days
    for uid in list(expired_today.keys()):
        expired_today[uid] = [
            e for e in expired_today[uid]
            if datetime.fromtimestamp(e["expired_at"], tz=TIMEZONE).date() == today
        ]
        if not expired_today[uid]:
            del expired_today[uid]

    # Send 5-min reminders
    for sid, session in active_sessions.items():
        time_left = session["end_ts"] - now
        if client and not session.get("reminder_sent") and 0 < time_left <= 360:  # 5 mins
            try:
                client.chat_postEphemeral(
                    channel=session["channel_id"],
                    user=session["user_id"],
                    text=f"⏰ Your study session at *{session['location']}* ends in 5 minutes. Extend your time?",
                    blocks=[
                        {"type": "section", "text": {"type": "mrkdwn", "text": f"⏰ Your study session at *{session['location']}* ends in 5 minutes. Extend your time?"}},
                        {
                            "type": "actions",
                            "block_id": "study_extend_actions",
                            "elements": [
                                {"type": "button", "text": {"type": "plain_text", "text": "Extend 30 mins"}, "action_id": "study_extend_30", "value": sid},
                                {"type": "button", "text": {"type": "plain_text", "text": "Extend 1 hour"}, "action_id": "study_extend_60", "value": sid},
                            ],
                        },
                    ],
                )
                session["reminder_sent"] = True
            except Exception:
                pass


def _extend_session(client, sid, minutes, response_url=None):
    """Extend the session by X minutes and update the original announcement."""
    if sid not in active_sessions:
        return
    session = active_sessions[sid]
    session["end_ts"] += minutes * 60  # extend in seconds

    # Recalculate time range for the announcement
    end_dt = datetime.fromtimestamp(session["end_ts"], tz=TIMEZONE)
    start_str = session["time_range"].split("–")[0].strip()
    end_str = end_dt.strftime("%-I:%M %p") if os.name != "nt" else end_dt.strftime("%I:%M %p")
    session["time_range"] = f"{start_str} – {end_str}"

    user_id = session["user_id"]
    channel_id = session["channel_id"]
    message_ts = session["message_ts"]

    # Update the original announcement message
    if channel_id and message_ts:
        blocks = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": _build_announcement_text(session)},
            }
        ]
        if session.get("description"):
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"_{session['description']}_"},
            })
        if session.get("image_url"):
            blocks.append({"type": "divider"})
            blocks.append({
                "type": "image",
                "image_url": session["image_url"],
                "alt_text": f"Study spot photo from {session.get('user_name','')}",
                "block_id": "study_image_block",
            })
        vibe_block = _meta_context_block(session)
        if vibe_block:
            blocks.append(vibe_block)
        blocks.append({
            "type": "actions",
            "block_id": "study_join_actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "🙋 Join"},
                    "action_id": "study_join",
                    "value": sid,
                }
            ],
        })

        client.chat_update(
            channel=channel_id,
            ts=message_ts,
            text=_build_announcement_text(session),
            blocks=blocks,
        )

    # ✅ Update ephemeral confirmation using response_url
    if response_url:
        confirm_text = f"✅ Your study session at *{session['location']}* has been extended by {minutes} minutes."
        requests.post(response_url, json={
            "replace_original": True,
            "text": confirm_text,
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": confirm_text}}
            ]
        })

    # Reset reminder so it could remind again 5 mins before the new end time
    session["reminder_sent"] = False



def _accumulate_hours_on_cancel(session):
    """Credit the user for time studied up to now when a session is cancelled early."""
    if session.get("created_ts"):
        elapsed = min(time.time(), session["end_ts"]) - session["created_ts"]
        _add_study_seconds(session["user_id"], elapsed)

def _get_user_session(user_id):
    """Return (session_id, session) for user's current active session, or (None, None)."""
    now = time.time()
    for sid, s in active_sessions.items():
        if s["user_id"] == user_id and s["end_ts"] > now:
            return sid, s
    return None, None


def _expiry_cleanup_loop():
    """Background loop: every 60s, unpin and remove expired sessions."""
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        return
    client = WebClient(token=token)
    while True:
        try:
            _clean_expired_sessions(client)
        except Exception:
            pass
        time.sleep(60)



def _build_study_modal_blocks(session_data=None):
    """
    Build Slack modal blocks for creating or editing a study session.
    If session_data is provided, prefill fields with previous values.
    """
    location_options = [{"text": {"type": "plain_text", "text": loc}, "value": loc} for loc in UCI_LOCATIONS]
    hour_options = [{"text": {"type": "plain_text", "text": str(h)}, "value": str(h)} for h in range(1, 13)]
    minute_options = [{"text": {"type": "plain_text", "text": f"{m:02d}"}, "value": str(m)} for m in range(60)]
    ampm_options = [{"text": {"type": "plain_text", "text": t}, "value": t} for t in ["AM", "PM"]]

    # Defaults
    now = datetime.now(TIMEZONE)
    start_dt = now
    end_dt = now + timedelta(hours=1)
    location_value = None
    other_location_value = None
    description_value = ""
    participants_value = []
    vibe_value = None
    capacity_value = None

    if session_data:
        # Extract location and "Other" if needed
        full_location = session_data.get("location", "")
        if "—" in full_location:
            location_value, other_location_value = [s.strip() for s in full_location.split("—", 1)]
        elif full_location in UCI_LOCATIONS:
            location_value = full_location
        else:
            location_value = "Other"
            other_location_value = full_location

        # Description
        description_value = session_data.get("description", "")

        # Participants (exclude owner)
        participants_value = [uid for uid in session_data.get("participants", []) if uid != session_data.get("user_id")]

        # Vibe tag
        vibe_value = session_data.get("vibe")

        # Capacity
        capacity_value = session_data.get("capacity")

        # Parse start/end times from time_range
        time_range = session_data.get("time_range", "")
        try:
            start_str, end_str = [s.strip() for s in time_range.split("–")]
            start_dt = datetime.strptime(start_str, "%I:%M %p").replace(year=now.year, month=now.month, day=now.day)
            end_dt = datetime.strptime(end_str, "%I:%M %p").replace(year=now.year, month=now.month, day=now.day)
            if end_dt <= start_dt:
                end_dt += timedelta(days=1)
        except Exception:
            start_dt = now
            end_dt = now + timedelta(hours=1)

    # Convert to 12-hour display for dropdowns
    def _to_12h(dt):
        h12 = dt.hour % 12 or 12
        ampm = "AM" if dt.hour < 12 else "PM"
        return h12, dt.minute, ampm

    start_hour, start_minute, start_ampm = _to_12h(start_dt)
    end_hour, end_minute, end_ampm = _to_12h(end_dt)

    start_hour_initial = {"text": {"type": "plain_text", "text": str(start_hour)}, "value": str(start_hour)}
    start_minute_initial = {"text": {"type": "plain_text", "text": f"{start_minute:02d}"}, "value": str(start_minute)}
    start_ampm_initial = {"text": {"type": "plain_text", "text": start_ampm}, "value": start_ampm}

    end_hour_initial = {"text": {"type": "plain_text", "text": str(end_hour)}, "value": str(end_hour)}
    end_minute_initial = {"text": {"type": "plain_text", "text": f"{end_minute:02d}"}, "value": str(end_minute)}
    end_ampm_initial = {"text": {"type": "plain_text", "text": end_ampm}, "value": end_ampm}

    blocks = [
        {
            "type": "input",
            "block_id": "location_block",
            "element": {
                "type": "static_select",
                "action_id": "location_select",
                "placeholder": {"type": "plain_text", "text": "Where are you studying?"},
                "options": location_options,
                **({"initial_option": {"text": {"type": "plain_text", "text": location_value}, "value": location_value}} if location_value else {})
            },
            "label": {"type": "plain_text", "text": "Location"},
        },
        {
            "type": "input",
            "block_id": "other_location_block",
            "optional": True,
            "element": {
                "type": "plain_text_input",
                "action_id": "other_location_input",
                "placeholder": {"type": "plain_text", "text": "e.g. 4th floor Langson"},
                "initial_value": other_location_value or "",
            },
            "label": {"type": "plain_text", "text": "Specific spot"},
        },
        {
            "type": "input",
            "block_id": "studying_with_block",
            "optional": True,
            "element": {
                "type": "multi_users_select",
                "action_id": "studying_with_input",
                "placeholder": {"type": "plain_text", "text": "Tag people studying with you"},
                **({"initial_users": participants_value} if participants_value else {})
            },
            "label": {"type": "plain_text", "text": "Studying with"},
        },
        {
            "type": "input",
            "block_id": "description_block",
            "optional": True,
            "element": {
                "type": "plain_text_input",
                "action_id": "description_input",
                "placeholder": {"type": "plain_text", "text": "e.g. Studying for CS161, feel free to join!"},
                "multiline": True,
                "initial_value": description_value,
            },
            "label": {"type": "plain_text", "text": "Description"},
        },
        {
            "type": "input",
            "block_id": "image_block",
            "optional": True,
            "element": {
                "type": "file_input",
                "action_id": "image_input",
                "filetypes": ["png", "jpg", "jpeg", "gif", "webp"],
            },
            "label": {"type": "plain_text", "text": "Share a photo to show your study spot"},
        },
        {
            "type": "input",
            "block_id": "vibe_block",
            "optional": True,
            "element": {
                "type": "radio_buttons",
                "action_id": "vibe_input",
                "options": [
                    {"text": {"type": "plain_text", "text": "🟥 Lock In"}, "value": "lock_in"},
                    {"text": {"type": "plain_text", "text": "🟦 Chill Vibes"}, "value": "chill_vibes"},
                ],
                **({"initial_option": {"text": {"type": "plain_text", "text": VIBE_LABELS[vibe_value]}, "value": vibe_value}} if vibe_value in VIBE_LABELS else {}),
            },
            "label": {"type": "plain_text", "text": "Vibe"},
        },
        {
            "type": "input",
            "block_id": "capacity_block",
            "optional": True,
            "element": {
                "type": "number_input",
                "action_id": "capacity_input",
                "is_decimal_allowed": False,
                "min_value": "1",
                "max_value": "50",
                "placeholder": {"type": "plain_text", "text": "e.g. 4"},
                **({"initial_value": str(capacity_value)} if capacity_value else {}),
            },
            "label": {"type": "plain_text", "text": "Capacity (max spots)"},
        },
        {"type": "header", "block_id": "start_time_header", "text": {"type": "plain_text", "text": "Start time", "emoji": True}},
        {
            "type": "actions",
            "block_id": "start_time_actions",
            "elements": [
                {"type": "static_select", "action_id": "start_hour_input", "options": hour_options, "initial_option": start_hour_initial},
                {"type": "static_select", "action_id": "start_minute_input", "options": minute_options, "initial_option": start_minute_initial},
                {"type": "static_select", "action_id": "start_ampm_input", "options": ampm_options, "initial_option": start_ampm_initial},
            ],
        },
        {"type": "header", "block_id": "end_time_header", "text": {"type": "plain_text", "text": "End time", "emoji": True}},
        {
            "type": "actions",
            "block_id": "end_time_actions",
            "elements": [
                {"type": "static_select", "action_id": "end_hour_input", "options": hour_options, "initial_option": end_hour_initial},
                {"type": "static_select", "action_id": "end_minute_input", "options": minute_options, "initial_option": end_minute_initial},
                {"type": "static_select", "action_id": "end_ampm_input", "options": ampm_options, "initial_option": end_ampm_initial},
            ],
        },
    ]

    return blocks





def register_study_handlers(app):
    """Register /study, study_modal, and study_cancel with the Bolt app."""

    def _open_already_studying_modal(trigger_id, session_id, session, client):
        """Open modal prompting user to cancel their existing session."""
        end_dt = datetime.fromtimestamp(session["end_ts"])
        end_str = end_dt.strftime("%-I:%M %p") if os.name != "nt" else end_dt.strftime("%I:%M %p")
        location = session["location"]
        client.views_open(
            trigger_id=trigger_id,
            view={
                "type": "modal",
                "callback_id": "study_already_modal",
                "title": {"type": "plain_text", "text": "Already studying"},
                "close": {"type": "plain_text", "text": "Keep it"},
                "submit": {"type": "plain_text", "text": "Cancel & create new"},
                "private_metadata": session_id,
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"You're already listed as studying at *{location}* (until ~{end_str}).\n\nCancel that announcement first, then you can share a new location.",
                        },
                    },
                ],
            },
        )

    @app.command("/study")
    def cmd_study(ack, body, client, logger):
        try:
            ack()
            trigger_id = body.get("trigger_id")
            if not trigger_id:
                logger.error("Missing trigger_id in /study payload")
                return
            user_id = body["user_id"]
            _clean_expired_sessions(client)

            # /study edit — edit active session only
            if body.get("text", "").strip().lower() == "edit":
                existing_sid, existing_session = _get_user_session(user_id)
                if existing_sid:
                    client.views_open(
                        trigger_id=trigger_id,
                        view={
                            "type": "modal",
                            "callback_id": "study_modal",
                            "title": {"type": "plain_text", "text": "Edit study session"},
                            "submit": {"type": "plain_text", "text": "Update"},
                            "private_metadata": existing_sid,
                            "blocks": _build_study_modal_blocks(session_data=existing_session),
                        },
                    )
                    return

                client.chat_postEphemeral(
                    channel=body["channel_id"],
                    user=user_id,
                    text="You don't have an active study session. Use `/study reactivate` to bring back a past one.",
                )
                return

            # /study reactivate — reactivate an expired session from today
            if body.get("text", "").strip().lower() == "reactivate":
                existing_sid, _ = _get_user_session(user_id)
                if existing_sid:
                    client.chat_postEphemeral(
                        channel=body["channel_id"],
                        user=user_id,
                        text="You already have an active study session. Use `/study edit` to modify it.",
                    )
                    return

                user_expired = expired_today.get(user_id, [])
                if user_expired:
                    most_recent = max(user_expired, key=lambda e: e["expired_at"])
                    expired_session = dict(most_recent["session"])
                    expired_session.pop("time_range", None)
                    meta = json.dumps({
                        "reactivate": True,
                        "channel_id": most_recent["session"].get("channel_id"),
                        "message_ts": most_recent["session"].get("message_ts"),
                        "image_url": most_recent["session"].get("image_url"),
                    })
                    client.views_open(
                        trigger_id=trigger_id,
                        view={
                            "type": "modal",
                            "callback_id": "study_modal",
                            "title": {"type": "plain_text", "text": "Reactivate study session"},
                            "submit": {"type": "plain_text", "text": "Reactivate"},
                            "private_metadata": meta,
                            "blocks": _build_study_modal_blocks(session_data=expired_session),
                        },
                    )
                    return

                client.chat_postEphemeral(
                    channel=body["channel_id"],
                    user=user_id,
                    text="No study sessions from today to reactivate. Use `/study` to start a new one.",
                )
                return

            # /study cancel — cancel the current active session
            if body.get("text", "").strip().lower() == "cancel":
                existing_sid, existing_session = _get_user_session(user_id)
                if not existing_sid:
                    client.chat_postEphemeral(
                        channel=body["channel_id"],
                        user=user_id,
                        text="You don't have an active study session to cancel.",
                    )
                    return
                _accumulate_hours_on_cancel(existing_session)
                del active_sessions[existing_sid]
                channel_id = existing_session.get("channel_id")
                message_ts = existing_session.get("message_ts")
                if channel_id and message_ts:
                    _update_message_cancelled(client, channel_id, message_ts, existing_session)
                    try:
                        client.pins_remove(channel=channel_id, timestamp=message_ts)
                    except Exception:
                        pass
                client.chat_postEphemeral(
                    channel=body["channel_id"],
                    user=user_id,
                    text="Your study session has been cancelled.",
                )
                return

            # /study extend — send extend buttons for the active session
            if body.get("text", "").strip().lower() == "extend":
                existing_sid, existing_session = _get_user_session(user_id)
                if not existing_sid:
                    client.chat_postEphemeral(
                        channel=body["channel_id"],
                        user=user_id,
                        text="You don't have an active study session to extend.",
                    )
                    return
                client.chat_postEphemeral(
                    channel=body["channel_id"],
                    user=user_id,
                    text="Extend your study session:",
                    blocks=[
                        {"type": "section", "text": {"type": "mrkdwn", "text": f"Extend your session at *{existing_session['location']}*?"}},
                        {
                            "type": "actions",
                            "block_id": "study_extend_actions",
                            "elements": [
                                {"type": "button", "text": {"type": "plain_text", "text": "Extend 30 mins"}, "action_id": "study_extend_30", "value": existing_sid},
                                {"type": "button", "text": {"type": "plain_text", "text": "Extend 1 hour"}, "action_id": "study_extend_60", "value": existing_sid},
                            ],
                        },
                    ],
                )
                return

            # /study leaderboard — post the top studied hours publicly
            if body.get("text", "").strip().lower() == "leaderboard":
                all_hours = _get_all_study_hours()
                if not all_hours:
                    client.chat_postEphemeral(
                        channel=body["channel_id"],
                        user=user_id,
                        text="No study hours tracked yet. Start a session with `/study`!",
                    )
                    return
                sorted_users = sorted(all_hours.items(), key=lambda x: x[1], reverse=True)[:10]
                medals = ["🥇", "🥈", "🥉"]
                lines = []
                for i, (uid, secs) in enumerate(sorted_users):
                    hours, rem = divmod(int(secs), 3600)
                    mins = rem // 60
                    rank = medals[i] if i < 3 else f"{i + 1}."
                    duration = f"{hours}h {mins}m" if hours else f"{mins}m"
                    lines.append(f"{rank} <@{uid}> — {duration}")
                text = "*🏆 Study Leaderboard*\n" + "\n".join(lines)
                client.chat_postEphemeral(
                    channel=body["channel_id"],
                    user=user_id,
                    text=text,
                    blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
                )
                return

            existing_sid, existing_session = _get_user_session(user_id)
            if existing_sid is not None:
                _open_already_studying_modal(trigger_id, existing_sid, existing_session, client)
                return
            result = client.views_open(
                trigger_id=trigger_id,
                view={
                    "type": "modal",
                    "callback_id": "study_modal",
                    "title": {"type": "plain_text", "text": "Share study location"},
                    "submit": {"type": "plain_text", "text": "Announce"},
                    "blocks": _build_study_modal_blocks(),
                },
            )
            logger.info("Modal opened: %s", result)
        except Exception as e:
            logger.exception("Failed to open /study modal: %s", e)
            raise

    # Ack block_actions from time/AM-PM dropdowns in the study modal (no-op, state is in view_submission)
    def _ack_modal_select(ack):
        ack()

    for action_id in (
        "start_hour_input",
        "start_minute_input",
        "start_ampm_input",
        "end_hour_input",
        "end_minute_input",
        "end_ampm_input",
    ):
        app.action(action_id)(_ack_modal_select)

    @app.view("study_modal")
    def handle_study_modal_submit(ack, body, client, view):
        ack()
        _clean_expired_sessions(client)

        user_id = body["user"]["id"]
        user_name = body["user"].get("name", "Someone")

        # ------------------------
        # Extract form inputs
        # ------------------------
        location_block = view["state"]["values"]["location_block"]
        location = location_block["location_select"]["selected_option"]["value"]

        other_block = view["state"]["values"]["other_location_block"]
        other_raw = (other_block.get("other_location_input") or {}).get("value") or ""
        other = other_raw.strip() if isinstance(other_raw, str) else ""
        if location == "Other":
            location = other or "Somewhere on campus"
        elif other:
            location = f"{location} — {other}"

        studying_with_block = view["state"]["values"].get("studying_with_block") or {}
        studying_with_obj = studying_with_block.get("studying_with_input") or {}
        selected_user_ids = studying_with_obj.get("selected_users") or []

        description_block = view["state"]["values"].get("description_block") or {}
        description_raw = (description_block.get("description_input") or {}).get("value") or ""
        description = description_raw.strip() if isinstance(description_raw, str) else ""

        vibe_block = view["state"]["values"].get("vibe_block") or {}
        vibe_opt = (vibe_block.get("vibe_input") or {}).get("selected_option")
        vibe = vibe_opt["value"] if vibe_opt else None

        capacity_raw = ((view["state"]["values"].get("capacity_block") or {}).get("capacity_input") or {}).get("value")
        capacity = int(capacity_raw) if capacity_raw else None

        # Handle image upload
        image_block = view["state"]["values"].get("image_block") or {}
        image_obj = image_block.get("image_input") or {}
        image_files = image_obj.get("files") or []
        image_url = None
        image_block = view["state"]["values"].get("image_block") or {}
        image_obj = image_block.get("image_input") or {}
        image_files = image_obj.get("files") or []

        if image_files and len(image_files) > 0:
            # New file uploaded
            file_data = image_files[0]
            permalink_public = file_data.get("permalink_public")
            url_private = file_data.get("url_private")
            if permalink_public and url_private:
                pub_secret = permalink_public.split("-")[-1] if "-" in permalink_public else None
                if pub_secret:
                    image_url = f"{url_private}?pub_secret={pub_secret}"
        else:
            # No new file uploaded: preserve existing image if editing/reactivating
            raw_meta = view.get("private_metadata") or ""
            try:
                meta = json.loads(raw_meta)
                if meta.get("reactivate"):
                    image_url = meta.get("image_url")
                elif raw_meta in active_sessions:
                    image_url = active_sessions[raw_meta].get("image_url")
            except (json.JSONDecodeError, TypeError):
                if raw_meta in active_sessions:
                    image_url = active_sessions[raw_meta].get("image_url")

        def _get_select(block_id, action_id, default=None):
            obj = (view["state"]["values"].get(block_id) or {}).get(action_id) or {}
            opt = obj.get("selected_option")
            return opt.get("value") if opt else default

        start_h = int(_get_select("start_time_actions", "start_hour_input") or 9)
        start_m = int(_get_select("start_time_actions", "start_minute_input") or 0)
        start_ampm = _get_select("start_time_actions", "start_ampm_input") or "AM"
        end_h = int(_get_select("end_time_actions", "end_hour_input") or 5)
        end_m = int(_get_select("end_time_actions", "end_minute_input") or 0)
        end_ampm = _get_select("end_time_actions", "end_ampm_input") or "PM"

        def _to_24(h, ampm):
            if ampm == "AM":
                return 0 if h == 12 else h
            return 12 if h == 12 else h + 12

        today = datetime.now().date()
        start_dt = datetime.combine(
            today,
            datetime.strptime(f"{_to_24(start_h, start_ampm):02d}:{start_m:02d}", "%H:%M").time(),
        )
        end_dt = datetime.combine(
            today,
            datetime.strptime(f"{_to_24(end_h, end_ampm):02d}:{end_m:02d}", "%H:%M").time(),
        )
        if end_dt <= start_dt:
            end_dt += timedelta(days=1)
        end_ts = end_dt.timestamp()

        start_str = start_dt.strftime("%-I:%M %p") if os.name != "nt" else start_dt.strftime("%I:%M %p")
        end_str = end_dt.strftime("%-I:%M %p") if os.name != "nt" else end_dt.strftime("%I:%M %p")
        time_range = f"{start_str} – {end_str}"

        # ------------------------
        # Check if reactivating an expired session
        # ------------------------
        raw_meta = view.get("private_metadata") or ""
        try:
            meta = json.loads(raw_meta)
        except (json.JSONDecodeError, TypeError):
            meta = {}

        if meta.get("reactivate"):
            channel_id = meta["channel_id"]
            message_ts = meta["message_ts"]
            session_id = str(uuid.uuid4())
            active_sessions[session_id] = {
                "user_id": user_id,
                "user_name": user_name,
                "location": location,
                "time_range": time_range,
                "end_ts": end_ts,
                "image_url": image_url,
                "participants": [user_id] + list(selected_user_ids),
                "description": description,
                "vibe": vibe,
                "capacity": capacity,
                "channel_id": channel_id,
                "message_ts": message_ts,
                "created_ts": time.time(),
                "reminder_sent": False,
            }
            session = active_sessions[session_id]
            blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": _build_announcement_text(session)}}]
            if description:
                blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"_{description}_"}})
            if image_url:
                blocks.append({"type": "divider"})
                blocks.append({"type": "image", "image_url": image_url, "alt_text": f"Study spot photo from {user_name}", "block_id": "study_image_block"})
            vibe_block = _meta_context_block(session)
            if vibe_block:
                blocks.append(vibe_block)
            blocks.append({
                "type": "actions",
                "block_id": "study_join_actions",
                "elements": [
                    {"type": "button", "text": {"type": "plain_text", "text": "🙋 Join"}, "action_id": "study_join", "value": session_id}
                ]
            })
            client.chat_update(channel=channel_id, ts=message_ts, text=_build_announcement_text(session), blocks=blocks)
            try:
                client.pins_add(channel=channel_id, timestamp=message_ts)
            except Exception:
                pass
            client.chat_postEphemeral(
                channel=channel_id,
                user=user_id,
                text="Manage your study announcement",
                blocks=[
                    {"type": "section", "text": {"type": "mrkdwn", "text": "Your study session has been reactivated:"}},
                    {
                        "type": "actions",
                        "block_id": "study_ephemeral_actions",
                        "elements": [
                            {"type": "button", "text": {"type": "plain_text", "text": "Cancel announcement"}, "action_id": "study_cancel", "value": session_id, "style": "danger"},
                            {"type": "button", "text": {"type": "plain_text", "text": "Edit announcement"}, "action_id": "study_edit", "value": session_id, "style": "primary"},
                        ],
                    },
                ],
            )
            return

        # ------------------------
        # Check if editing existing session
        # ------------------------
        session_id = view.get("private_metadata")
        if session_id and session_id in active_sessions:
            # Update existing session
            session = active_sessions[session_id]
            session.update({
                "location": location,
                "description": description,
                "image_url": image_url,
                "participants": [user_id] + list(selected_user_ids),
                "time_range": time_range,
                "end_ts": end_ts,
                "vibe": vibe,
                "capacity": capacity,
                "reminder_sent": False
            })
            # Update original message
            if session.get("channel_id") and session.get("message_ts"):
                blocks = [
                    {"type": "section", "text": {"type": "mrkdwn", "text": _build_announcement_text(session)}}
                ]
                if description:
                    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"_{description}_"}})
                if image_url:
                    blocks.append({"type": "divider"})
                    blocks.append({"type": "image", "image_url": image_url, "alt_text": f"Study spot photo from {user_name}", "block_id": "study_image_block"})
                vibe_block = _meta_context_block(session)
                if vibe_block:
                    blocks.append(vibe_block)
                blocks.append({
                    "type": "actions",
                    "block_id": "study_join_actions",
                    "elements": [
                        {"type": "button", "text": {"type": "plain_text", "text": "🙋 Join"}, "action_id": "study_join", "value": session_id}
                    ]
                })
                client.chat_update(
                    channel=session["channel_id"],
                    ts=session["message_ts"],
                    text=_build_announcement_text(session),
                    blocks=blocks
                )
            return  # exit after updating

        # ------------------------
        # Otherwise, create a new session
        # ------------------------
        session_id = str(uuid.uuid4())
        channel_id = STUDY_CHANNEL_ID

        active_sessions[session_id] = {
            "user_id": user_id,
            "user_name": user_name,
            "location": location,
            "time_range": time_range,
            "end_ts": end_ts,
            "image_url": image_url,
            "participants": [user_id] + list(selected_user_ids),
            "description": description,
            "vibe": vibe,
            "capacity": capacity,
            "channel_id": channel_id,
            "message_ts": None,
            "created_ts": time.time(),
            "reminder_sent": False,
        }

        # Post announcement
        msg_text = _build_announcement_text(active_sessions[session_id])
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": msg_text}}]
        if description:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"_{description}_"}})
        if image_url:
            blocks.append({"type": "divider"})
            blocks.append({"type": "image", "image_url": image_url, "alt_text": f"Study spot photo from {user_name}", "block_id": "study_image_block"})
        vibe_block = _meta_context_block(active_sessions[session_id])
        if vibe_block:
            blocks.append(vibe_block)
        blocks.append({
            "type": "actions",
            "block_id": "study_join_actions",
            "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "🙋 Join"}, "action_id": "study_join", "value": session_id}
            ]
        })

        result = client.chat_postMessage(channel=channel_id, text=msg_text, blocks=blocks)
        active_sessions[session_id]["message_ts"] = result["ts"]

        # Pin the message
        try:
            client.pins_add(channel=channel_id, timestamp=result["ts"])
        except Exception:
            pass

        # Ephemeral Cancel/Edit message for the author
        client.chat_postEphemeral(
            channel=channel_id,
            user=user_id,
            text="Manage your study announcement",
            blocks=[
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "You can cancel or edit your study announcement:"}
                },
                {
                    "type": "actions",
                    "block_id": "study_ephemeral_actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Cancel announcement"},
                            "action_id": "study_cancel",
                            "value": session_id,
                            "style": "danger"
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Edit announcement"},
                            "action_id": "study_edit",
                            "value": session_id,
                            "style": "primary"
                        }
                    ]
                }
            ]
        )


    @app.action("study_extend_30")
    def handle_extend_30(ack, body, client):
        ack()
        sid = body["actions"][0]["value"]
        response_url = body.get("response_url")
        _extend_session(client, sid, 30, response_url)

    @app.action("study_extend_60")
    def handle_extend_60(ack, body, client):
        ack()
        sid = body["actions"][0]["value"]
        response_url = body.get("response_url")
        _extend_session(client, sid, 60, response_url)


    def _update_message_cancelled(client, channel_id, message_ts, session):
        full_text = _build_full_text(session)

        # Slack strikethrough per line
        lines = full_text.split("\n")
        cancelled = "\n".join(f"~{l}~" for l in lines) + " — Cancelled"

        client.chat_update(
            channel=channel_id,
            ts=message_ts,
            text=cancelled,
            blocks=[
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": cancelled},
                }
            ],
        )

    @app.view("study_already_modal")
    def handle_study_already_submit(ack, body, client, view):
        ack()
        session_id = view.get("private_metadata")
        if not session_id or session_id not in active_sessions:
            return
        session = active_sessions[session_id]
        channel_id = session.get("channel_id")
        message_ts = session.get("message_ts")
        user_id = session["user_id"]
        _accumulate_hours_on_cancel(session)
        del active_sessions[session_id]
        if channel_id and message_ts:
            _update_message_cancelled(client, channel_id, message_ts, session)
            try:
                client.pins_remove(channel=channel_id, timestamp=message_ts)
            except Exception:
                pass
        channel_for_ephemeral = channel_id or body.get("container", {}).get("channel_id")
        if channel_for_ephemeral:
            client.chat_postEphemeral(
                channel=channel_for_ephemeral,
                user=user_id,
                text="Cancelled. Use `/study` again to share a new location.",
            )
        else:
            dm_channel = client.conversations_open(users=[user_id])["channel"]["id"]
            client.chat_postMessage(
                channel=dm_channel,
                text="Cancelled. Use `/study` again to share a new location.",
            )

    
    @app.action("study_cancel")
    def handle_study_cancel(ack, body, client):
        ack()
        session_id = body["actions"][0]["value"]

        # Cancel the session
        if session_id in active_sessions:
            session = active_sessions.pop(session_id)
            _accumulate_hours_on_cancel(session)
            channel_id = session.get("channel_id")
            message_ts = session.get("message_ts")
            if channel_id and message_ts:
                _update_message_cancelled(client, channel_id, message_ts, session)
                try:
                    client.pins_remove(channel=channel_id, timestamp=message_ts)
                except Exception:
                    pass

        # ✅ Update ephemeral message via response_url
        response_url = body.get("response_url")
        if response_url:
            requests.post(response_url, json={
                "replace_original": True,
                "text": "What would you like to do?",
                "blocks": [
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": "✅ Your study announcement was cancelled. You can cancel it or edit it:"}
                    },
                    {
                        "type": "actions",
                        "block_id": "study_ephemeral_actions",
                        "elements": [
                            {
                                "type": "button",
                                "text": {"type": "plain_text", "text": "Cancel announcement"},
                                "action_id": "study_cancel",
                                "value": session_id,
                                "style": "danger"
                            },
                            {
                                "type": "button",
                                "text": {"type": "plain_text", "text": "Edit announcement"},
                                "action_id": "study_edit",
                                "value": session_id,
                                "style": "primary"
                            }
                        ]
                    }
                ]
            })


    @app.event("member_joined_channel")
    def handle_member_joined_channel(event, client, logger):
        """Send instructions to users when they join the study channel."""
        # Only send in the study channel
        if event.get("channel") != STUDY_CHANNEL_ID:
            return
        
        user_id = event.get("user")
        
        instructions = """👋 Welcome to Study Sessions!

        Here's how to use this channel:

        *Commands:*
        • `/study` — Share where you're studying and for how long
        • Check the pinned message for the current study sessions

        Happy studying! 📚"""
        
        try:
            client.chat_postEphemeral(
                channel=STUDY_CHANNEL_ID,
                user=user_id,
                text=instructions,
            )
        except Exception as e:
            logger.error(f"Failed to send welcome message: {e}")

    @app.event("app_mention")
    def handle_app_mention(event, client):
        """Handle when the bot is mentioned."""
        pass

    @app.action("study_join")
    def handle_study_join(ack, body, client):
        ack()

        user_id = body["user"]["id"]
        session_id = body["actions"][0]["value"]

        if session_id not in active_sessions:
            return

        session = active_sessions[session_id]

        if user_id not in session["participants"]:
            session["participants"].append(user_id)
        # session["participants"].append(user_id)

        full_text = _build_full_text(session)

        blocks = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": _build_announcement_text(session)},
            }
        ]

        if session.get("description"):
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"_{session['description']}_"},
            })

        if session.get("image_url"):
            blocks.append({"type": "divider"})
            blocks.append({
                "type": "image",
                "image_url": session["image_url"],
                "alt_text": f"Study spot photo from {session.get('user_name','')}",
                "block_id": "study_image_block",
            })

        vibe_block = _meta_context_block(session)
        if vibe_block:
            blocks.append(vibe_block)

        blocks.append({
            "type": "actions",
            "block_id": "study_join_actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "🙋 Join"},
                    "action_id": "study_join",
                    "value": session_id,
                }
            ],
        })

        client.chat_update(
            channel=session["channel_id"],
            ts=session["message_ts"],
            text=full_text,
            blocks=blocks,
        )

    @app.action("study_edit")
    def handle_study_edit(ack, body, client):
        ack()
        session_id = body["actions"][0]["value"]
        if session_id not in active_sessions:
            return
        session = active_sessions[session_id]

        trigger_id = body.get("trigger_id")
        if not trigger_id:
            return

        # Build modal blocks prefilled with current session info
        client.views_open(
            trigger_id=trigger_id,
            view={
                "type": "modal",
                "callback_id": "study_modal",
                "title": {"type": "plain_text", "text": "Edit study location"},
                "submit": {"type": "plain_text", "text": "Update"},
                "private_metadata": session_id,  # so we know which session to update
                "blocks": _build_study_modal_blocks(session_data=session)
            }
        )




    # Start background thread to unpin expired sessions every 60s
    t = threading.Thread(target=_expiry_cleanup_loop, daemon=True)
    t.start()

"""Study location bot: share where you're studying, tag others, cancel, list who's studying."""
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

from slack_sdk import WebClient
import pytz

# Channel where announcements are posted
# Use TEST_CHANNEL_ID if ENV is "development", otherwise use the hardcoded channel

# STUDY_CHANNEL_ID = "C0ACQP6P3T2"
STUDY_CHANNEL_ID = os.environ.get("TEST_CHANNEL_ID")


# Set your timezone here (e.g., 'America/Los_Angeles', 'America/New_York', 'America/Chicago')
TIMEZONE = pytz.timezone(os.environ.get("TZ", "America/Los_Angeles"))

UCI_LOCATIONS = [
    "Langson Library",
    "Science Library",
    "Gateway Study Center",
    "Student Center",
    "Aldrich Hall",
    "Engineering Tower",
    "Social Science Plaza",
    "Anteater Recreation Center (ARC)",
    "Other",
]

# session_id -> { user_id, user_name, location, end_ts }
active_sessions = {}

def _build_announcement_text(session):
    names = session["participants"]
    with_suffix = ""
    if len(names) > 1:
        with_suffix = " with " + " ".join(f"<@{uid}>" for uid in names[1:])

    return (
        f"📍 <@{names[0]}> is studying at *{session['location']}*"
        f"{with_suffix} *{session['time_range']}*."
    )

def _build_full_text(session):
    base = _build_announcement_text(session)
    if session.get("description"):
        base += f"\n_{session['description']}_"
    if session.get("image_url"):
        base += "\nPhoto attached"
    return base

def _clean_expired_sessions(client=None):
    """Remove expired sessions and unpin their messages if client is provided."""
    now = time.time()
    expired = [sid for sid, s in active_sessions.items() if s["end_ts"] < now]
    for sid in expired:
        s = active_sessions[sid]
        if client and s.get("channel_id") and s.get("message_ts"):
            try:
                client.pins_remove(channel=s["channel_id"], timestamp=s["message_ts"])
            except Exception:
                pass
        del active_sessions[sid]


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
        time.sleep(60)
        try:
            _clean_expired_sessions(client)
        except Exception:
            pass


def _build_study_modal_blocks(other_location_value=None):
    location_options = [
        {"text": {"type": "plain_text", "text": loc}, "value": loc}
        for loc in UCI_LOCATIONS
    ]
    hour_options = [{"text": {"type": "plain_text", "text": str(h)}, "value": str(h)} for h in range(1, 13)]
    minute_options = [
        {"text": {"type": "plain_text", "text": f"{m:02d}"}, "value": str(m)}
        for m in range(60)
    ]
    ampm_options = [
        {"text": {"type": "plain_text", "text": "AM"}, "value": "AM"},
        {"text": {"type": "plain_text", "text": "PM"}, "value": "PM"},
    ]

    # Prefill start time = now, end time = now + 1 hour (same minutes)
    now = datetime.now(TIMEZONE)
    end_default = now + timedelta(hours=1)

    # Start
    start_hour_12 = now.hour % 12 or 12
    start_minute = now.minute
    start_ampm = "AM" if now.hour < 12 else "PM"

    start_hour_initial = {"text": {"type": "plain_text", "text": str(start_hour_12)}, "value": str(start_hour_12)}
    start_minute_initial = {"text": {"type": "plain_text", "text": f"{start_minute:02d}"}, "value": str(start_minute)}
    start_ampm_initial = {"text": {"type": "plain_text", "text": start_ampm}, "value": start_ampm}

    # End = +1 hour, same minute
    end_hour_12 = end_default.hour % 12 or 12
    end_minute = end_default.minute
    end_ampm = "AM" if end_default.hour < 12 else "PM"

    end_hour_initial = {"text": {"type": "plain_text", "text": str(end_hour_12)}, "value": str(end_hour_12)}
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
        {"type": "header", "block_id": "start_time_header", "text": {"type": "plain_text", "text": "Start time", "emoji": True}},
        {
            "type": "actions",
            "block_id": "start_time_actions",
            "elements": [
                {
                    "type": "static_select",
                    "action_id": "start_hour_input",
                    "placeholder": {"type": "plain_text", "text": "Hour"},
                    "options": hour_options,
                    "initial_option": start_hour_initial,
                },
                {
                    "type": "static_select",
                    "action_id": "start_minute_input",
                    "placeholder": {"type": "plain_text", "text": "Min"},
                    "options": minute_options,
                    "initial_option": start_minute_initial,
                },
                {
                    "type": "static_select",
                    "action_id": "start_ampm_input",
                    "placeholder": {"type": "plain_text", "text": "AM/PM"},
                    "options": ampm_options,
                    "initial_option": start_ampm_initial,
                },
            ],
        },
        {"type": "header", "block_id": "end_time_header", "text": {"type": "plain_text", "text": "End time", "emoji": True}},
        {
            "type": "actions",
            "block_id": "end_time_actions",
            "elements": [
                {
                    "type": "static_select",
                    "action_id": "end_hour_input",
                    "placeholder": {"type": "plain_text", "text": "Hour"},
                    "options": hour_options,
                    "initial_option": end_hour_initial,
                },
                {
                    "type": "static_select",
                    "action_id": "end_minute_input",
                    "placeholder": {"type": "plain_text", "text": "Min"},
                    "options": minute_options,
                    "initial_option": end_minute_initial,
                },
                {
                    "type": "static_select",
                    "action_id": "end_ampm_input",
                    "placeholder": {"type": "plain_text", "text": "AM/PM"},
                    "options": ampm_options,
                    "initial_option": end_ampm_initial,
                },
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
        with_suffix = ""
        if selected_user_ids:
            with_suffix = " with " + " ".join(f"<@{uid}>" for uid in selected_user_ids) + " "

        description_block = view["state"]["values"].get("description_block") or {}
        description_raw = (description_block.get("description_input") or {}).get("value") or ""
        description = description_raw.strip() if isinstance(description_raw, str) else ""

        # Handle image upload
        image_block = view["state"]["values"].get("image_block") or {}
        image_obj = image_block.get("image_input") or {}
        image_files = image_obj.get("files") or []
        image_url = None
        if image_files and len(image_files) > 0:
            file_data = image_files[0]
            # The file data from modal already contains permalink_public and url_private
            permalink_public = file_data.get("permalink_public")
            url_private = file_data.get("url_private")
            if permalink_public and url_private:
                # Extract the pub_secret from permalink_public and construct direct URL
                # permalink_public format: https://slack-files.com/TEAM-FILE-PUBSECRET
                # We need to use the url_private with ?pub_secret=PUBSECRET
                pub_secret = permalink_public.split("-")[-1] if "-" in permalink_public else None
                if pub_secret:
                    image_url = f"{url_private}?pub_secret={pub_secret}"
            print(f"[DEBUG] final image_url: {image_url}")

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

        session_id = str(uuid.uuid4())
        channel_id = STUDY_CHANNEL_ID
        start_str = start_dt.strftime("%-I:%M %p") if os.name != "nt" else start_dt.strftime("%I:%M %p")
        end_str = end_dt.strftime("%-I:%M %p") if os.name != "nt" else end_dt.strftime("%I:%M %p")
        time_range = f"{start_str} – {end_str}"

        active_sessions[session_id] = {
            "user_id": user_id,
            "user_name": user_name,
            "location": location,
            "time_range": time_range,
            "end_ts": end_ts,
            "image_url": image_url,
            "participants": [user_id] + list(selected_user_ids),
            "description": description,   # ← add this
        }



        if not channel_id:
            channel_id = client.conversations_open(users=[user_id])["channel"]["id"]
            msg = (
                f"✅ You're now listed as studying at *{location}* *{time_range}*. "
                "Set `STUDY_CHANNEL_ID` in your app config to announce to a channel."
            )
            if description:
                msg += f"\n_{description}_"
            client.chat_postMessage(channel=channel_id, text=msg)
        else:
            msg = _build_announcement_text(active_sessions[session_id])

            blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": msg}}]
            if description:
                blocks.append({
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"_{description}_"},
                })
            if image_url:
                blocks.append({"type": "divider"})
                blocks.append({
                    "type": "image",
                    "image_url": image_url,
                    "alt_text": f"Study spot photo from {user_name}",
                    "block_id": "study_image_block",
                })

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

            full_text = f"{msg}\n_{description}_" if description else msg
            if image_url:
                full_text += f"\nPhoto attached"
            result = client.chat_postMessage(
                channel=channel_id,
                text=full_text,
                blocks=blocks,
            )
            active_sessions[session_id]["channel_id"] = channel_id
            active_sessions[session_id]["message_ts"] = result["ts"]
            try:
                client.pins_add(channel=channel_id, timestamp=result["ts"])
            except Exception:
                pass
            # Ephemeral message: only the author sees the Cancel button
            client.chat_postEphemeral(
                channel=channel_id,
                user=user_id,
                text="Cancel your study announcement",
                blocks=[
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": "Cancel your study announcement?"},
                    },
                    {
                        "type": "actions",
                        "block_id": "study_cancel_ephemeral_actions",
                        "elements": [
                            {
                                "type": "button",
                                "text": {"type": "plain_text", "text": "Cancel announcement"},
                                "action_id": "study_cancel",
                                "value": session_id,
                            },
                        ],
                    },
                ],
            )

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
        # Button may be on ephemeral message; use session to find the channel announcement
        if session_id in active_sessions:
            session = active_sessions[session_id]
            channel_id = session.get("channel_id")
            message_ts = session.get("message_ts")
            del active_sessions[session_id]
            if channel_id and message_ts:
                _update_message_cancelled(client, channel_id, message_ts, session)
                try:
                    client.pins_remove(channel=channel_id, timestamp=message_ts)
                except Exception:
                    pass
            # Confirm to the user (they see this in the ephemeral thread)
            user_id = session.get("user_id")
            if user_id and body.get("channel", {}).get("id"):
                client.chat_postEphemeral(
                    channel=body["channel"]["id"],
                    user=user_id,
                    text="Cancelled. Use `/study` again to share a new location.",
                )

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




    # Start background thread to unpin expired sessions every 60s
    t = threading.Thread(target=_expiry_cleanup_loop, daemon=True)
    t.start()

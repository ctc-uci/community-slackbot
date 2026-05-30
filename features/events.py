"""Events: create, RSVP, edit, and cancel club events. Supports per-event ridesheets in-thread."""
import json
import os
import uuid
from datetime import datetime

import pytz
from firebase_admin import firestore
from firebase_client import get_firebase_app

EVENTS_CHANNEL_ID = os.environ.get("TEST_CHANNEL_ID", "C0ABVSK5QH0")
TIMEZONE = pytz.timezone(os.environ.get("TZ", "America/Los_Angeles"))


# ---------------------------------------------------------------------------
# Firebase helpers
# ---------------------------------------------------------------------------

def _db():
    get_firebase_app()
    return firestore.client()


def _save_event(event_id, event):
    _db().collection("events").document(event_id).set(event)


def _get_event(event_id):
    doc = _db().collection("events").document(event_id).get()
    return doc.to_dict() if doc.exists else None


def _delete_event(event_id):
    _db().collection("events").document(event_id).delete()


def _get_upcoming_events():
    """Return list of (event_id, event) for events that haven't ended yet, sorted soonest first."""
    now_ts = datetime.now(TIMEZONE).timestamp()
    results = []
    for doc in _db().collection("events").stream():
        data = doc.to_dict()
        end = data.get("end_ts") or data.get("start_ts", 0)
        if end >= now_ts:
            results.append((doc.id, data))
    results.sort(key=lambda x: x[1].get("start_ts", 0))
    return results


def _get_user_upcoming_events(user_id):
    return [(eid, e) for eid, e in _get_upcoming_events() if e.get("creator_id") == user_id]


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_ts(ts):
    """Format a unix timestamp to human-readable local time like '7:30 PM'."""
    return datetime.fromtimestamp(ts, tz=TIMEZONE).strftime("%-I:%M %p")


def _fmt_date(date_str):
    """Format YYYY-MM-DD to 'Monday, June 1'."""
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").strftime("%A, %B %-d")
    except (ValueError, TypeError):
        return date_str or "TBD"


def _event_datetime_str(event):
    date_str = _fmt_date(event.get("date", ""))
    start_ts = event.get("start_ts")
    end_ts = event.get("end_ts")
    if start_ts and end_ts:
        return f"{date_str}  ·  {_fmt_ts(start_ts)} – {_fmt_ts(end_ts)}"
    if start_ts:
        return f"{date_str}  ·  {_fmt_ts(start_ts)}"
    return date_str


def _announcement_fallback_text(event):
    return f"📣 *{event.get('title', 'Event')}* — {_event_datetime_str(event)}"


# ---------------------------------------------------------------------------
# Announcement blocks
# ---------------------------------------------------------------------------

def _build_going_text(going, capacity=None):
    lines = []
    if capacity:
        lines.append(f"{len(going)}/{capacity} spots")
    if not going:
        lines.append("_No one is going yet — be the first!_")
    else:
        mentions = "  ·  ".join(f"<@{uid}>" for uid in going)
        lines.append(f"Going: {mentions}")
    return "\n".join(lines)


def _build_announcement_blocks(event_id, event):
    title = event.get("title", "Untitled Event")
    location = event.get("location", "")
    description = event.get("description", "")
    image_url = event.get("image_url")
    capacity = event.get("capacity")
    rsvps = event.get("rsvps", {})

    going = [uid for uid, s in rsvps.items() if s == "going"]

    meta_parts = [f"📅 {_event_datetime_str(event)}"]
    if location:
        meta_parts.append(f"📍 {location}")

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": title, "emoji": True}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": "  ·  ".join(meta_parts)}]},
    ]

    if description:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"_{description}_"}})

    if image_url:
        blocks.append({
            "type": "image",
            "image_url": image_url,
            "alt_text": title,
            "block_id": "event_image_block",
        })

    blocks.append({"type": "divider"})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": _build_going_text(going, capacity)}]})
    blocks.append({
        "type": "actions",
        "block_id": "event_rsvp_actions",
        "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "✅ Going"}, "action_id": "event_rsvp_going", "value": event_id, "style": "primary"},
        ],
    })

    return blocks


# ---------------------------------------------------------------------------
# Modal builder
# ---------------------------------------------------------------------------

def _build_event_modal_blocks(event_data=None):
    now = datetime.now(TIMEZONE)

    title_value = ""
    date_value = now.strftime("%Y-%m-%d")
    location_value = ""
    description_value = ""
    capacity_value = None

    hour_options = [{"text": {"type": "plain_text", "text": str(h)}, "value": str(h)} for h in range(1, 13)]
    minute_options = [{"text": {"type": "plain_text", "text": f"{m:02d}"}, "value": str(m)} for m in range(60)]
    ampm_options = [{"text": {"type": "plain_text", "text": t}, "value": t} for t in ["AM", "PM"]]

    # Default start = next whole hour, end = start + 2h
    from datetime import timedelta
    start_dt = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    end_dt = start_dt + timedelta(hours=2)

    if event_data:
        title_value = event_data.get("title", "")
        location_value = event_data.get("location", "")
        description_value = event_data.get("description", "")
        capacity_value = event_data.get("capacity")
        if event_data.get("date"):
            date_value = event_data["date"]
        if event_data.get("start_ts"):
            start_dt = datetime.fromtimestamp(event_data["start_ts"], tz=TIMEZONE)
        if event_data.get("end_ts"):
            end_dt = datetime.fromtimestamp(event_data["end_ts"], tz=TIMEZONE)

    def _to_12h(dt):
        h12 = dt.hour % 12 or 12
        ampm = "AM" if dt.hour < 12 else "PM"
        return h12, dt.minute, ampm

    sh, sm, sa = _to_12h(start_dt)
    eh, em, ea = _to_12h(end_dt)

    start_h_opt  = {"text": {"type": "plain_text", "text": str(sh)},   "value": str(sh)}
    start_m_opt  = {"text": {"type": "plain_text", "text": f"{sm:02d}"}, "value": str(sm)}
    start_a_opt  = {"text": {"type": "plain_text", "text": sa},          "value": sa}
    end_h_opt    = {"text": {"type": "plain_text", "text": str(eh)},   "value": str(eh)}
    end_m_opt    = {"text": {"type": "plain_text", "text": f"{em:02d}"}, "value": str(em)}
    end_a_opt    = {"text": {"type": "plain_text", "text": ea},          "value": ea}

    return [
        {
            "type": "input",
            "block_id": "event_title_block",
            "element": {
                "type": "plain_text_input",
                "action_id": "event_title_input",
                "placeholder": {"type": "plain_text", "text": "e.g. Spring General Meeting"},
                "initial_value": title_value,
            },
            "label": {"type": "plain_text", "text": "Event title"},
        },
        {
            "type": "input",
            "block_id": "event_date_block",
            "element": {
                "type": "datepicker",
                "action_id": "event_date_input",
                "initial_date": date_value,
                "placeholder": {"type": "plain_text", "text": "Select a date"},
            },
            "label": {"type": "plain_text", "text": "Date"},
        },
        {"type": "header", "block_id": "event_start_time_header", "text": {"type": "plain_text", "text": "Start time", "emoji": True}},
        {
            "type": "actions",
            "block_id": "event_start_time_actions",
            "elements": [
                {"type": "static_select", "action_id": "event_start_hour",   "options": hour_options,   "initial_option": start_h_opt},
                {"type": "static_select", "action_id": "event_start_minute", "options": minute_options, "initial_option": start_m_opt},
                {"type": "static_select", "action_id": "event_start_ampm",   "options": ampm_options,   "initial_option": start_a_opt},
            ],
        },
        {"type": "header", "block_id": "event_end_time_header", "text": {"type": "plain_text", "text": "End time", "emoji": True}},
        {
            "type": "actions",
            "block_id": "event_end_time_actions",
            "elements": [
                {"type": "static_select", "action_id": "event_end_hour",   "options": hour_options,   "initial_option": end_h_opt},
                {"type": "static_select", "action_id": "event_end_minute", "options": minute_options, "initial_option": end_m_opt},
                {"type": "static_select", "action_id": "event_end_ampm",   "options": ampm_options,   "initial_option": end_a_opt},
            ],
        },
        {
            "type": "input",
            "block_id": "event_location_block",
            "element": {
                "type": "plain_text_input",
                "action_id": "event_location_input",
                "placeholder": {"type": "plain_text", "text": "e.g. DBH 1200"},
                "initial_value": location_value,
            },
            "label": {"type": "plain_text", "text": "Location"},
            "optional": True,
        },
        {
            "type": "input",
            "block_id": "event_description_block",
            "element": {
                "type": "plain_text_input",
                "action_id": "event_description_input",
                "placeholder": {"type": "plain_text", "text": "What's happening at this event?"},
                "multiline": True,
                "initial_value": description_value,
            },
            "label": {"type": "plain_text", "text": "Description"},
            "optional": True,
        },
        {
            "type": "input",
            "block_id": "event_image_block",
            "element": {
                "type": "file_input",
                "action_id": "event_image_input",
                "filetypes": ["png", "jpg", "jpeg", "gif", "webp"],
            },
            "label": {"type": "plain_text", "text": "Event photo (flyer, venue, etc.)"},
            "optional": True,
        },
        {
            "type": "input",
            "block_id": "event_capacity_block",
            "element": {
                "type": "number_input",
                "action_id": "event_capacity_input",
                "is_decimal_allowed": False,
                "min_value": "1",
                "max_value": "500",
                "placeholder": {"type": "plain_text", "text": "e.g. 30"},
                **({"initial_value": str(capacity_value)} if capacity_value else {}),
            },
            "label": {"type": "plain_text", "text": "Capacity (max attendees)"},
            "optional": True,
        },
        # Only show ridesheet option on new events; on edit a ridesheet may already exist
        *([{
            "type": "input",
            "block_id": "event_ridesheet_block",
            "optional": True,
            "element": {
                "type": "radio_buttons",
                "action_id": "event_ridesheet_input",
                "options": [
                    {"text": {"type": "plain_text", "text": "🚗 Yes — create a ridesheet for this event"}, "value": "normal"},
                ],
            },
            "label": {"type": "plain_text", "text": "Ridesheet"},
        }] if event_data is None else []),
    ]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _extract_event_fields(view):
    """Pull all form values out of a submitted event modal view."""
    def _val(block_id, action_id):
        return ((view["state"]["values"].get(block_id) or {}).get(action_id) or {})

    title = (_val("event_title_block", "event_title_input").get("value") or "").strip()
    date_str = _val("event_date_block", "event_date_input").get("selected_date") or datetime.now(TIMEZONE).strftime("%Y-%m-%d")
    location = (_val("event_location_block", "event_location_input").get("value") or "").strip()
    description = (_val("event_description_block", "event_description_input").get("value") or "").strip()
    capacity_raw = _val("event_capacity_block", "event_capacity_input").get("value")
    capacity = int(capacity_raw) if capacity_raw else None

    def _sel(block_id, action_id, default=None):
        opt = _val(block_id, action_id).get("selected_option")
        return opt["value"] if opt else default

    def _to_24(h, ampm):
        if ampm == "AM":
            return 0 if h == 12 else h
        return 12 if h == 12 else h + 12

    year, month, day = map(int, date_str.split("-"))

    sh = int(_sel("event_start_time_actions", "event_start_hour") or 12)
    sm = int(_sel("event_start_time_actions", "event_start_minute") or 0)
    sa = _sel("event_start_time_actions", "event_start_ampm") or "PM"
    eh = int(_sel("event_end_time_actions", "event_end_hour") or 12)
    em = int(_sel("event_end_time_actions", "event_end_minute") or 0)
    ea = _sel("event_end_time_actions", "event_end_ampm") or "PM"

    start_ts = TIMEZONE.localize(datetime(year, month, day, _to_24(sh, sa), sm)).timestamp()
    end_ts = TIMEZONE.localize(datetime(year, month, day, _to_24(eh, ea), em)).timestamp()

    # Image
    image_files = _val("event_image_block", "event_image_input").get("files") or []
    image_url = None
    if image_files:
        f = image_files[0]
        permalink = f.get("permalink_public", "")
        url_private = f.get("url_private", "")
        if permalink and url_private:
            secret = permalink.split("-")[-1] if "-" in permalink else None
            if secret:
                image_url = f"{url_private}?pub_secret={secret}"

    ridesheet_opt = _val("event_ridesheet_block", "event_ridesheet_input").get("selected_option")
    ridesheet_mode = ridesheet_opt["value"] if ridesheet_opt else None

    return title, date_str, start_ts, end_ts, location, description, capacity, image_url, ridesheet_mode


def _cancel_event(client, event_id, event):
    """Strike through the announcement and unpin it."""
    channel_id = event.get("channel_id")
    message_ts = event.get("message_ts")
    title = event.get("title", "Event")
    dt_str = _event_datetime_str(event)
    cancelled_text = f"~📣 *{title}* — {dt_str}~ — Cancelled"
    if channel_id and message_ts:
        client.chat_update(
            channel=channel_id,
            ts=message_ts,
            text=cancelled_text,
            blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": cancelled_text}}],
        )
        try:
            client.pins_remove(channel=channel_id, timestamp=message_ts)
        except Exception:
            pass
    _delete_event(event_id)


# ---------------------------------------------------------------------------
# Handler registration
# ---------------------------------------------------------------------------

def register_events_handlers(app):

    # Ack the time dropdown selections (state is captured on modal submit, not here)
    def _ack(ack):
        ack()

    for _action_id in (
        "event_start_hour", "event_start_minute", "event_start_ampm",
        "event_end_hour", "event_end_minute", "event_end_ampm",
    ):
        app.action(_action_id)(_ack)

    # ------------------------------------------------------------------
    # /event command
    # ------------------------------------------------------------------

    @app.command("/event")
    def cmd_event(ack, body, client, logger):
        try:
            ack()
            user_id = body["user_id"]
            trigger_id = body.get("trigger_id")
            subcmd = body.get("text", "").strip().lower()

            if subcmd == "help":
                help_text = (
                    "*📣 Event Commands*\n\n"
                    "*Creating & Managing*\n"
                    "• `/event` — post a new event\n"
                    "• `/event edit` — edit your next upcoming event\n"
                    "• `/event cancel` — cancel your next upcoming event\n\n"
                    "*Discovery*\n"
                    "• `/event list` — see all upcoming events\n\n"
                    "*RSVP*\n"
                    "• ✅ *Going* — click to mark yourself going, click again to remove yourself\n\n"
                    "*Ridesheets*\n"
                    "• Optionally include a ridesheet when creating an event — it'll be posted in the event thread"
                )
                client.chat_postEphemeral(
                    channel=body["channel_id"],
                    user=user_id,
                    text=help_text,
                    blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": help_text}}],
                )
                return

            if subcmd == "list":
                upcoming = _get_upcoming_events()
                if not upcoming:
                    client.chat_postEphemeral(
                        channel=body["channel_id"],
                        user=user_id,
                        text="No upcoming events. Create one with `/event`!",
                    )
                    return

                blocks = [
                    {"type": "section", "text": {"type": "mrkdwn", "text": f"*📅 {len(upcoming)} upcoming event{'s' if len(upcoming) != 1 else ''}*"}},
                    {"type": "divider"},
                ]
                for event_id, event in upcoming:
                    rsvps = event.get("rsvps", {})
                    going = sum(1 for s in rsvps.values() if s == "going")
                    line = f"*{event.get('title', 'Event')}*\n📅 {_event_datetime_str(event)}"
                    if event.get("location"):
                        line += f"\n📍 {event['location']}"
                    line += f"\n✅ {going} going"
                    blocks.append({
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": line},
                        "accessory": {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "✅ RSVP Going"},
                            "action_id": "event_rsvp_going",
                            "value": event_id,
                            "style": "primary",
                        },
                    })
                client.chat_postEphemeral(
                    channel=body["channel_id"],
                    user=user_id,
                    text="Upcoming events:",
                    blocks=blocks,
                )
                return

            if subcmd == "edit":
                user_events = _get_user_upcoming_events(user_id)
                if not user_events:
                    client.chat_postEphemeral(
                        channel=body["channel_id"],
                        user=user_id,
                        text="You don't have any upcoming events to edit.",
                    )
                    return
                event_id, event = user_events[0]
                client.views_open(
                    trigger_id=trigger_id,
                    view={
                        "type": "modal",
                        "callback_id": "event_modal",
                        "title": {"type": "plain_text", "text": "Edit event"},
                        "submit": {"type": "plain_text", "text": "Update"},
                        "private_metadata": event_id,
                        "blocks": _build_event_modal_blocks(event_data=event),
                    },
                )
                return

            if subcmd == "cancel":
                user_events = _get_user_upcoming_events(user_id)
                if not user_events:
                    client.chat_postEphemeral(
                        channel=body["channel_id"],
                        user=user_id,
                        text="You don't have any upcoming events to cancel.",
                    )
                    return
                event_id, event = user_events[0]
                _cancel_event(client, event_id, event)
                client.chat_postEphemeral(
                    channel=body["channel_id"],
                    user=user_id,
                    text=f"✅ *{event.get('title')}* has been cancelled.",
                )
                return

            # Default: open create modal
            client.views_open(
                trigger_id=trigger_id,
                view={
                    "type": "modal",
                    "callback_id": "event_modal",
                    "title": {"type": "plain_text", "text": "Create an event"},
                    "submit": {"type": "plain_text", "text": "Post event"},
                    "private_metadata": "",
                    "blocks": _build_event_modal_blocks(),
                },
            )
        except Exception as e:
            logger.exception("Failed in /event: %s", e)
            raise

    # ------------------------------------------------------------------
    # Modal submit
    # ------------------------------------------------------------------

    @app.view("event_modal")
    def handle_event_modal_submit(ack, body, client, view):
        ack()
        user_id = body["user"]["id"]
        user_name = body["user"].get("name", "Someone")

        title, date_str, start_ts, end_ts, location, description, capacity, image_url, ridesheet_mode = _extract_event_fields(view)
        event_id = (view.get("private_metadata") or "").strip()
        is_edit = bool(event_id)

        if is_edit:
            existing = _get_event(event_id)
            if not existing:
                return
            if not image_url:
                image_url = existing.get("image_url")
            existing.update({
                "title": title,
                "date": date_str,
                "start_ts": start_ts,
                "end_ts": end_ts,
                "location": location,
                "description": description,
                "capacity": capacity,
                "image_url": image_url,
            })
            _save_event(event_id, existing)
            channel_id = existing.get("channel_id")
            message_ts = existing.get("message_ts")
            if channel_id and message_ts:
                client.chat_update(
                    channel=channel_id,
                    ts=message_ts,
                    text=_announcement_fallback_text(existing),
                    blocks=_build_announcement_blocks(event_id, existing),
                )
            return

        # New event
        event_id = str(uuid.uuid4())
        channel_id = EVENTS_CHANNEL_ID
        event = {
            "creator_id": user_id,
            "creator_name": user_name,
            "title": title,
            "date": date_str,
            "start_ts": start_ts,
            "end_ts": end_ts,
            "location": location,
            "description": description,
            "image_url": image_url,
            "capacity": capacity,
            "rsvps": {user_id: "going"},
            "channel_id": channel_id,
            "message_ts": None,
            "created_ts": datetime.now(TIMEZONE).timestamp(),
        }

        result = client.chat_postMessage(
            channel=channel_id,
            text=_announcement_fallback_text(event),
            blocks=_build_announcement_blocks(event_id, event),
        )
        event["message_ts"] = result["ts"]
        _save_event(event_id, event)

        if ridesheet_mode:
            from features.ridesheet import create_ridesheet_for_event
            start_time_str = datetime.fromtimestamp(start_ts, tz=TIMEZONE).strftime("%H:%M") if start_ts else None
            end_time_str = datetime.fromtimestamp(end_ts, tz=TIMEZONE).strftime("%H:%M") if end_ts else None
            create_ridesheet_for_event(
                client=client,
                channel_id=channel_id,
                thread_ts=result["ts"],
                title=title,
                location=location,
                date_str=date_str,
                start_time_str=start_time_str,
                end_time_str=end_time_str,
                mode=ridesheet_mode,
            )

        try:
            client.pins_add(channel=channel_id, timestamp=result["ts"])
        except Exception:
            pass

        client.chat_postEphemeral(
            channel=channel_id,
            user=user_id,
            text="Your event has been posted!",
            blocks=[
                {"type": "section", "text": {"type": "mrkdwn", "text": "Your event has been posted! Manage it here:"}},
                {
                    "type": "actions",
                    "block_id": "event_manage_actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Edit event"},
                            "action_id": "event_edit_btn",
                            "value": event_id,
                            "style": "primary",
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Cancel event"},
                            "action_id": "event_cancel_btn",
                            "value": event_id,
                            "style": "danger",
                        },
                    ],
                },
            ],
        )

    # ------------------------------------------------------------------
    # RSVP actions
    # ------------------------------------------------------------------

    @app.action("event_rsvp_going")
    def handle_rsvp_going(ack, body, client):
        ack()
        user_id = body["user"]["id"]
        event_id = body["actions"][0]["value"]

        event = _get_event(event_id)
        if not event:
            return

        rsvps = event.get("rsvps", {})

        if rsvps.get(user_id) == "going":
            del rsvps[user_id]
        else:
            if event.get("capacity"):
                going_count = sum(1 for s in rsvps.values() if s == "going")
                if going_count >= event["capacity"]:
                    client.chat_postEphemeral(
                        channel=body.get("channel", {}).get("id") or event["channel_id"],
                        user=user_id,
                        text=f"Sorry, this event is full ({event['capacity']}/{event['capacity']} spots taken).",
                    )
                    return
            rsvps[user_id] = "going"

        event["rsvps"] = rsvps
        _save_event(event_id, event)

        channel_id = event.get("channel_id")
        message_ts = event.get("message_ts")
        if channel_id and message_ts:
            client.chat_update(
                channel=channel_id,
                ts=message_ts,
                text=_announcement_fallback_text(event),
                blocks=_build_announcement_blocks(event_id, event),
            )

    # ------------------------------------------------------------------
    # Edit / Cancel buttons from ephemeral message
    # ------------------------------------------------------------------

    @app.action("event_edit_btn")
    def handle_event_edit_btn(ack, body, client):
        ack()
        event_id = body["actions"][0]["value"]
        trigger_id = body.get("trigger_id")
        if not trigger_id:
            return
        event = _get_event(event_id)
        if not event:
            return
        client.views_open(
            trigger_id=trigger_id,
            view={
                "type": "modal",
                "callback_id": "event_modal",
                "title": {"type": "plain_text", "text": "Edit event"},
                "submit": {"type": "plain_text", "text": "Update"},
                "private_metadata": event_id,
                "blocks": _build_event_modal_blocks(event_data=event),
            },
        )

    @app.action("event_cancel_btn")
    def handle_event_cancel_btn(ack, body, client):
        import requests
        ack()
        event_id = body["actions"][0]["value"]
        event = _get_event(event_id)
        if not event:
            return
        _cancel_event(client, event_id, event)
        response_url = body.get("response_url")
        if response_url:
            requests.post(response_url, json={
                "replace_original": True,
                "text": f"✅ *{event.get('title')}* has been cancelled.",
                "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": f"✅ *{event.get('title')}* has been cancelled."}}],
            })

"""Events bot: create, RSVP, edit, and cancel club events."""
import json
import os
import uuid
from datetime import datetime, timedelta

import pytz
from firebase_admin import firestore
from firebase_client import get_firebase_app

EVENTS_CHANNEL_ID = os.environ.get("TEST_CHANNEL_ID", "C0ABVSK5QH0")
TIMEZONE = pytz.timezone(os.environ.get("TZ", "America/Los_Angeles"))

RSVP_LABELS = {
    "going": "✅ Going",
    "maybe": "🤔 Maybe",
    "not_going": "❌ Can't make it",
}


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
    """Return list of (event_id, event) sorted by date/start_ts, excluding past events."""
    now_ts = datetime.now(TIMEZONE).timestamp()
    docs = _db().collection("events").stream()
    results = []
    for doc in docs:
        data = doc.to_dict()
        if data.get("start_ts", 0) >= now_ts or data.get("end_ts", 0) >= now_ts:
            results.append((doc.id, data))
    results.sort(key=lambda x: x[1].get("start_ts", 0))
    return results


def _get_user_upcoming_events(user_id):
    """Return list of (event_id, event) for events created by user_id, soonest first."""
    all_upcoming = _get_upcoming_events()
    return [(eid, e) for eid, e in all_upcoming if e.get("creator_id") == user_id]


# ---------------------------------------------------------------------------
# Message building
# ---------------------------------------------------------------------------

def _format_event_datetime(event):
    """Return a human-readable date/time string for the event."""
    start_ts = event.get("start_ts")
    end_ts = event.get("end_ts")
    if not start_ts:
        return event.get("date", "TBD")
    start_dt = datetime.fromtimestamp(start_ts, tz=TIMEZONE)
    date_str = start_dt.strftime("%A, %B %-d")
    start_time = start_dt.strftime("%-I:%M %p")
    if end_ts:
        end_dt = datetime.fromtimestamp(end_ts, tz=TIMEZONE)
        end_time = end_dt.strftime("%-I:%M %p")
        return f"{date_str}  ·  {start_time} – {end_time}"
    return f"{date_str}  ·  {start_time}"


def _build_announcement_blocks(event_id, event):
    title = event.get("title", "Untitled Event")
    location = event.get("location", "")
    description = event.get("description", "")
    image_url = event.get("image_url")
    capacity = event.get("capacity")
    rsvps = event.get("rsvps", {})

    going = [uid for uid, s in rsvps.items() if s == "going"]
    maybe = [uid for uid, s in rsvps.items() if s == "maybe"]

    datetime_str = _format_event_datetime(event)
    header_text = f"*{title}*\n📅 {datetime_str}"
    if location:
        header_text += f"\n📍 {location}"

    rsvp_summary = f"*{len(going)} going*"
    if maybe:
        rsvp_summary += f"  ·  {len(maybe)} maybe"
    if capacity:
        rsvp_summary += f"  ·  {len(going)}/{capacity} spots"

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": header_text}},
    ]

    if description:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"_{description}_"}})

    if image_url:
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "image",
            "image_url": image_url,
            "alt_text": f"Event photo for {title}",
            "block_id": "event_image_block",
        })

    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": rsvp_summary}]})

    blocks.append({
        "type": "actions",
        "block_id": "event_rsvp_actions",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "✅ Going"},
                "action_id": "event_rsvp_going",
                "value": event_id,
                "style": "primary",
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "🤔 Maybe"},
                "action_id": "event_rsvp_maybe",
                "value": event_id,
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "❌ Can't make it"},
                "action_id": "event_rsvp_not_going",
                "value": event_id,
            },
        ],
    })

    return blocks


def _build_announcement_text(event):
    title = event.get("title", "Untitled Event")
    return f"📣 *{title}* — {_format_event_datetime(event)}"


# ---------------------------------------------------------------------------
# Modal builder
# ---------------------------------------------------------------------------

def _build_event_modal_blocks(event_data=None):
    hour_options = [{"text": {"type": "plain_text", "text": str(h)}, "value": str(h)} for h in range(1, 13)]
    minute_options = [{"text": {"type": "plain_text", "text": f"{m:02d}"}, "value": str(m)} for m in range(0, 60, 5)]
    ampm_options = [{"text": {"type": "plain_text", "text": t}, "value": t} for t in ["AM", "PM"]]

    now = datetime.now(TIMEZONE)
    title_value = ""
    date_value = now.strftime("%Y-%m-%d")
    location_value = ""
    description_value = ""
    capacity_value = None

    # Start time defaults: next whole hour
    start_dt = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    end_dt = start_dt + timedelta(hours=2)

    if event_data:
        title_value = event_data.get("title", "")
        location_value = event_data.get("location", "")
        description_value = event_data.get("description", "")
        capacity_value = event_data.get("capacity")
        if event_data.get("start_ts"):
            start_dt = datetime.fromtimestamp(event_data["start_ts"], tz=TIMEZONE)
            date_value = start_dt.strftime("%Y-%m-%d")
        if event_data.get("end_ts"):
            end_dt = datetime.fromtimestamp(event_data["end_ts"], tz=TIMEZONE)

    def _to_12h(dt):
        h12 = dt.hour % 12 or 12
        ampm = "AM" if dt.hour < 12 else "PM"
        return h12, dt.minute, ampm

    sh, sm, sa = _to_12h(start_dt)
    eh, em, ea = _to_12h(end_dt)

    # Snap minutes to nearest 5 for the dropdown
    sm = (sm // 5) * 5
    em = (em // 5) * 5

    start_h_opt = {"text": {"type": "plain_text", "text": str(sh)}, "value": str(sh)}
    start_m_opt = {"text": {"type": "plain_text", "text": f"{sm:02d}"}, "value": str(sm)}
    start_a_opt = {"text": {"type": "plain_text", "text": sa}, "value": sa}
    end_h_opt = {"text": {"type": "plain_text", "text": str(eh)}, "value": str(eh)}
    end_m_opt = {"text": {"type": "plain_text", "text": f"{em:02d}"}, "value": str(em)}
    end_a_opt = {"text": {"type": "plain_text", "text": ea}, "value": ea}

    blocks = [
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
        {"type": "header", "block_id": "event_start_header", "text": {"type": "plain_text", "text": "Start time", "emoji": True}},
        {
            "type": "actions",
            "block_id": "event_start_time_actions",
            "elements": [
                {"type": "static_select", "action_id": "event_start_hour", "options": hour_options, "initial_option": start_h_opt},
                {"type": "static_select", "action_id": "event_start_minute", "options": minute_options, "initial_option": start_m_opt},
                {"type": "static_select", "action_id": "event_start_ampm", "options": ampm_options, "initial_option": start_a_opt},
            ],
        },
        {"type": "header", "block_id": "event_end_header", "text": {"type": "plain_text", "text": "End time (optional)", "emoji": True}},
        {
            "type": "actions",
            "block_id": "event_end_time_actions",
            "elements": [
                {"type": "static_select", "action_id": "event_end_hour", "options": hour_options, "initial_option": end_h_opt},
                {"type": "static_select", "action_id": "event_end_minute", "options": minute_options, "initial_option": end_m_opt},
                {"type": "static_select", "action_id": "event_end_ampm", "options": ampm_options, "initial_option": end_a_opt},
            ],
        },
        {
            "type": "input",
            "block_id": "event_location_block",
            "optional": True,
            "element": {
                "type": "plain_text_input",
                "action_id": "event_location_input",
                "placeholder": {"type": "plain_text", "text": "e.g. DBH 1200"},
                "initial_value": location_value,
            },
            "label": {"type": "plain_text", "text": "Location"},
        },
        {
            "type": "input",
            "block_id": "event_description_block",
            "optional": True,
            "element": {
                "type": "plain_text_input",
                "action_id": "event_description_input",
                "placeholder": {"type": "plain_text", "text": "What's happening at this event?"},
                "multiline": True,
                "initial_value": description_value,
            },
            "label": {"type": "plain_text", "text": "Description"},
        },
        {
            "type": "input",
            "block_id": "event_image_block",
            "optional": True,
            "element": {
                "type": "file_input",
                "action_id": "event_image_input",
                "filetypes": ["png", "jpg", "jpeg", "gif", "webp"],
            },
            "label": {"type": "plain_text", "text": "Event photo (flyer, venue, etc.)"},
        },
        {
            "type": "input",
            "block_id": "event_capacity_block",
            "optional": True,
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
        },
    ]

    return blocks


# ---------------------------------------------------------------------------
# Time parsing helpers
# ---------------------------------------------------------------------------

def _to_24(h, ampm):
    if ampm == "AM":
        return 0 if h == 12 else h
    return 12 if h == 12 else h + 12


def _get_select(view, block_id, action_id, default=None):
    obj = (view["state"]["values"].get(block_id) or {}).get(action_id) or {}
    opt = obj.get("selected_option")
    return opt.get("value") if opt else default


def _parse_times(view, date_str):
    """Parse start/end datetimes from modal view state. Returns (start_ts, end_ts or None)."""
    sh = int(_get_select(view, "event_start_time_actions", "event_start_hour") or 12)
    sm = int(_get_select(view, "event_start_time_actions", "event_start_minute") or 0)
    sa = _get_select(view, "event_start_time_actions", "event_start_ampm") or "PM"
    eh = int(_get_select(view, "event_end_time_actions", "event_end_hour") or 0)
    em = int(_get_select(view, "event_end_time_actions", "event_end_minute") or 0)
    ea = _get_select(view, "event_end_time_actions", "event_end_ampm") or "PM"

    year, month, day = map(int, date_str.split("-"))
    start_dt = TIMEZONE.localize(datetime(year, month, day, _to_24(sh, sa), sm))
    end_dt = TIMEZONE.localize(datetime(year, month, day, _to_24(eh, ea), em))
    if end_dt <= start_dt:
        end_dt += timedelta(days=1)

    return start_dt.timestamp(), end_dt.timestamp()


# ---------------------------------------------------------------------------
# Handler registration
# ---------------------------------------------------------------------------

def register_events_handlers(app):

    # Ack time dropdown actions in the modal (state is captured on submit)
    def _ack(ack):
        ack()

    for action_id in (
        "event_start_hour", "event_start_minute", "event_start_ampm",
        "event_end_hour", "event_end_minute", "event_end_ampm",
    ):
        app.action(action_id)(_ack)

    # ------------------------------------------------------------------
    # /event command
    # ------------------------------------------------------------------

    @app.command("/event")
    def cmd_event(ack, body, client, logger):
        try:
            ack()
            trigger_id = body.get("trigger_id")
            user_id = body["user_id"]
            subcmd = body.get("text", "").strip().lower()

            # /event help
            if subcmd == "help":
                help_text = (
                    "*📣 Event Commands*\n\n"
                    "*Creating & Managing*\n"
                    "• `/event` — announce a new event\n"
                    "• `/event edit` — edit your next upcoming event\n"
                    "• `/event cancel` — cancel your next upcoming event\n\n"
                    "*Discovery*\n"
                    "• `/event list` — see all upcoming events with RSVP buttons\n\n"
                    "*RSVP Options*\n"
                    "• ✅ *Going* — you'll be there\n"
                    "• 🤔 *Maybe* — not sure yet\n"
                    "• ❌ *Can't make it* — mark yourself as not going"
                )
                client.chat_postEphemeral(
                    channel=body["channel_id"],
                    user=user_id,
                    text=help_text,
                    blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": help_text}}],
                )
                return

            # /event list
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
                    title = event.get("title", "Untitled Event")
                    dt_str = _format_event_datetime(event)
                    location = event.get("location", "")
                    text = f"*{title}*\n📅 {dt_str}"
                    if location:
                        text += f"\n📍 {location}"
                    text += f"\n✅ {going} going"

                    blocks.append({
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": text},
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

            # /event edit
            if subcmd == "edit":
                user_events = _get_user_upcoming_events(user_id)
                if not user_events:
                    client.chat_postEphemeral(
                        channel=body["channel_id"],
                        user=user_id,
                        text="You don't have any upcoming events to edit. Create one with `/event`.",
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

            # /event cancel
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
                    text=f"✅ Your event *{event.get('title')}* has been cancelled.",
                )
                return

            # /event (default) — open create modal
            if not trigger_id:
                logger.error("Missing trigger_id in /event payload")
                return

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

        # Extract fields
        title = ((view["state"]["values"].get("event_title_block") or {}).get("event_title_input") or {}).get("value", "").strip()
        date_str = ((view["state"]["values"].get("event_date_block") or {}).get("event_date_input") or {}).get("selected_date") or datetime.now(TIMEZONE).strftime("%Y-%m-%d")
        location = (((view["state"]["values"].get("event_location_block") or {}).get("event_location_input") or {}).get("value") or "").strip()
        description = (((view["state"]["values"].get("event_description_block") or {}).get("event_description_input") or {}).get("value") or "").strip()
        capacity_raw = ((view["state"]["values"].get("event_capacity_block") or {}).get("event_capacity_input") or {}).get("value")
        capacity = int(capacity_raw) if capacity_raw else None

        start_ts, end_ts = _parse_times(view, date_str)

        # Image
        image_files = ((view["state"]["values"].get("event_image_block") or {}).get("event_image_input") or {}).get("files") or []
        image_url = None
        if image_files:
            file_data = image_files[0]
            permalink_public = file_data.get("permalink_public")
            url_private = file_data.get("url_private")
            if permalink_public and url_private:
                pub_secret = permalink_public.split("-")[-1] if "-" in permalink_public else None
                if pub_secret:
                    image_url = f"{url_private}?pub_secret={pub_secret}"

        event_id = view.get("private_metadata") or ""
        is_edit = bool(event_id)

        if is_edit:
            # Editing existing event
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
                blocks = _build_announcement_blocks(event_id, existing)
                client.chat_update(
                    channel=channel_id,
                    ts=message_ts,
                    text=_build_announcement_text(existing),
                    blocks=blocks,
                )
            return

        # Creating a new event
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
            "rsvps": {},
            "channel_id": channel_id,
            "message_ts": None,
            "created_ts": datetime.now(TIMEZONE).timestamp(),
        }

        blocks = _build_announcement_blocks(event_id, event)
        msg_text = _build_announcement_text(event)
        result = client.chat_postMessage(channel=channel_id, text=msg_text, blocks=blocks)
        event["message_ts"] = result["ts"]
        _save_event(event_id, event)

        try:
            client.pins_add(channel=channel_id, timestamp=result["ts"])
        except Exception:
            pass

        # Ephemeral manage buttons for the creator
        client.chat_postEphemeral(
            channel=channel_id,
            user=user_id,
            text="Manage your event",
            blocks=[
                {"type": "section", "text": {"type": "mrkdwn", "text": "Your event has been posted! Manage it here:"}},
                {
                    "type": "actions",
                    "block_id": "event_ephemeral_actions",
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

    def _handle_rsvp(status, ack, body, client):
        ack()
        user_id = body["user"]["id"]
        event_id = body["actions"][0]["value"]

        event = _get_event(event_id)
        if not event:
            return

        rsvps = event.get("rsvps", {})
        prev_status = rsvps.get(user_id)

        # Toggle off if clicking the same status again
        if prev_status == status:
            del rsvps[user_id]
        else:
            # Enforce capacity for "going"
            if status == "going" and event.get("capacity"):
                going_count = sum(1 for s in rsvps.values() if s == "going")
                if going_count >= event["capacity"] and prev_status != "going":
                    client.chat_postEphemeral(
                        channel=body.get("channel", {}).get("id", event["channel_id"]),
                        user=user_id,
                        text=f"Sorry, this event is full ({event['capacity']}/{event['capacity']} spots taken).",
                    )
                    return
            rsvps[user_id] = status

        event["rsvps"] = rsvps
        _save_event(event_id, event)

        channel_id = event.get("channel_id")
        message_ts = event.get("message_ts")
        if channel_id and message_ts:
            blocks = _build_announcement_blocks(event_id, event)
            client.chat_update(
                channel=channel_id,
                ts=message_ts,
                text=_build_announcement_text(event),
                blocks=blocks,
            )

        # Confirm to user
        if user_id in event.get("rsvps", {}):
            confirm = f"You're marked as *{RSVP_LABELS[status]}* for *{event.get('title')}*."
        else:
            confirm = f"You've removed your RSVP for *{event.get('title')}*."
        client.chat_postEphemeral(
            channel=body.get("channel", {}).get("id", event["channel_id"]),
            user=user_id,
            text=confirm,
        )

    @app.action("event_rsvp_going")
    def handle_rsvp_going(ack, body, client):
        _handle_rsvp("going", ack, body, client)

    @app.action("event_rsvp_maybe")
    def handle_rsvp_maybe(ack, body, client):
        _handle_rsvp("maybe", ack, body, client)

    @app.action("event_rsvp_not_going")
    def handle_rsvp_not_going(ack, body, client):
        _handle_rsvp("not_going", ack, body, client)

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
                "text": f"✅ Event *{event.get('title')}* has been cancelled.",
                "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": f"✅ Event *{event.get('title')}* has been cancelled."}}],
            })


# ---------------------------------------------------------------------------
# Cancel helper (updates the posted message)
# ---------------------------------------------------------------------------

def _cancel_event(client, event_id, event):
    channel_id = event.get("channel_id")
    message_ts = event.get("message_ts")
    title = event.get("title", "Untitled Event")
    dt_str = _format_event_datetime(event)
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

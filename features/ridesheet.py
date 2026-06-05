import json
import re
from firebase_admin import firestore # type: ignore
import os
import time
from datetime import datetime
from slack_sdk import WebClient # type: ignore
import threading
from firebase_client import get_firebase_app

_SLACK_ID_RE = re.compile(r"^U[A-Z0-9]{6,}$")

def _fmt_user(uid):
    """Format a user identifier: @mention for Slack IDs, plain text for names."""
    return f"<@{uid}>" if _SLACK_ID_RE.match(uid) else uid

def _fmt_time(t):
    """Convert HH:MM (24h) to 12-hour time. Returns t unchanged if not parseable."""
    try:
        return datetime.strptime(t, "%H:%M").strftime("%-I:%M %p")
    except (ValueError, TypeError):
        return t

def _fmt_date_line(meta):
    date = meta.get("start_date", "TBD")
    start = meta.get("start_time")
    end = meta.get("end_time")
    if start and end:
        return f"📅 *Date:* {date}  ·  {_fmt_time(start)} – {_fmt_time(end)}"
    if start:
        return f"📅 *Date:* {date}  ·  {_fmt_time(start)}"
    return f"📅 *Date:* {date}"


def _is_event_thread_ridesheet(meta: dict) -> bool:
    """Event ridesheets are thread replies; parent message already has event details."""
    return meta.get("source") == "event"


def _slack_ridesheet_text(state: dict) -> str:
    meta = state.get("metadata", {})
    if _is_event_thread_ridesheet(meta):
        return "🚗 Ridesheet"
    return f"🚗 Ridesheet: {meta.get('title', 'Ridesheet')}"

firebase_app = get_firebase_app()
db = firestore.client(app=firebase_app)


from firebase_admin import firestore

def get_all_active_ridesheets():
    """
    Fetches all ridesheets from Firestore that are currently pinned.
    Returns a dictionary formatted as: {"channel_id|message_ts": state_dict}
    """
    active_ridesheets = {}

    collection_name = "ridesheets"

    try:
        docs = db.collection(collection_name).stream()

        for doc in docs:
            state = doc.to_dict()
            meta = state.get("metadata", {})

            if not meta.get("pinned", False):
                continue

            channel_id = meta.get("channel_id")
            ts = meta.get("message_ts")

            if not channel_id or not ts:
                try:
                    channel_id, ts = doc.id.split("_", 1)
                except ValueError:
                    print(f"Could not parse channel/ts for doc {doc.id}. Skipping.")
                    continue

            key = f"{channel_id}|{ts}"
            active_ridesheets[key] = state

    except Exception as e:
        print(f"Error fetching ridesheets from Firestore: {e}")

    return active_ridesheets

def _ridesheet_cleanup_loop():
    """Background loop: periodically unpin expired ridesheets."""
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        return
    client = WebClient(token=token)

    while True:
        try:
            _clean_expired_ridesheets(client)
        except Exception as e:
            print(f"Ridesheet cleanup error: {e}")

        time.sleep(3600)

def _clean_expired_ridesheets(client):
    """Unpins ridesheets where the end_date has passed."""
    active_ridesheets = get_all_active_ridesheets()

    today_str = datetime.now().strftime("%Y-%m-%d")

    for key, state in active_ridesheets.items():
        meta = state.get("metadata", {})
        end_date = meta.get("end_date")
        is_pinned = meta.get("pinned", False)

        if end_date and is_pinned and end_date < today_str:
            chan, ts = key.split("|")

            try:
                client.pins_remove(channel=chan, timestamp=ts)
            except Exception:
                pass

            state["metadata"]["pinned"] = False
            save_state(chan, ts, state)

def get_state(channel_id, message_ts):
    """Fetches ridesheet state from Firestore."""
    doc_ref = db.collection("ridesheets").document(f"{channel_id}_{message_ts}")
    doc = doc_ref.get()
    if doc.exists:
        return doc.to_dict()
    return None

def save_state(channel_id, message_ts, state):
    """Saves ridesheet state to Firestore."""
    doc_ref = db.collection("ridesheets").document(f"{channel_id}_{message_ts}")
    doc_ref.set(state)

def _build_ridesheet_blocks(state, channel_id, message_ts):
    """Builds the Slack blocks for displaying the ridesheet."""
    meta = state.get("metadata", {})
    is_random = meta.get("mode") == "random"

    if _is_event_thread_ridesheet(meta):
        blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "🚗 Ridesheet", "emoji": True},
            }
        ]
    else:
        blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"🚗 {meta.get('title', 'Carpool Ridesheet')}"},
            },
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": f"📍 *Location:* {meta.get('location', 'TBD')}"},
                    {"type": "mrkdwn", "text": _fmt_date_line(meta)},
                ],
            },
            {"type": "divider"},
        ]

    if is_random:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "🎲 *Blind Random Mode* — Passengers are randomly assigned to cars. Join from the website to get a random seat."
            }
        })
        blocks.append({"type": "divider"})

    cars = state.get("cars", {})
    if not cars:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "_No cars added yet. Be the first to volunteer to drive!_"}
        })

    for driver_id, car in cars.items():
        passengers = car.get("passengers", [])
        capacity = car.get("capacity", 4)

        direction = car.get("direction", "both")
        desc = car.get("description", "").strip()

        row_text = f"🚗 {_fmt_user(driver_id)} · {len(passengers)}/{capacity} seats"
        if direction == "there":
            row_text += "  ·  ⚠️ There only"
        elif direction == "return":
            row_text += "  ·  ⚠️ Return only"

        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": row_text}
        })

        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"⏰ Departs {_fmt_time(car.get('departure', 'TBD'))}"}]
        })

        if not is_random:
            pass_str = "  ·  ".join(_fmt_user(p) for p in passengers) if passengers else "_none yet_"
            blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"👥 {pass_str}"}]
            })

        if desc:
            blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"📝 {desc}"}]
            })

        blocks.append({"type": "divider"})

    domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
    web_base = (os.environ.get("RIDESHEET_WEB_URL") or (f"https://{domain}" if domain else "")).rstrip("/")
    if web_base and message_ts:
        blocks.append({
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "🚗 Join/Add Car"},
                    "action_id": "ridesheet_open_web",
                    "url": f"{web_base}/{channel_id}/{message_ts}",
                    "style": "primary"
                }
            ]
        })

    return blocks

def refresh_ridesheet_message(channel_id, message_ts):
    """Re-fetches Firestore state and updates the Slack message. Called by the website after edits."""
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        return
    client = WebClient(token=token)
    state = get_state(channel_id, message_ts)
    if not state:
        return
    blocks = _build_ridesheet_blocks(state, channel_id, message_ts)
    try:
        client.chat_update(
            channel=channel_id,
            ts=message_ts,
            text=_slack_ridesheet_text(state),
            blocks=blocks,
        )
    except Exception as e:
        print(f"Failed to refresh ridesheet Slack message: {e}")

def create_ridesheet_for_event(client, channel_id, thread_ts, title, location, date_str, start_time_str, end_time_str, mode="normal"):
    """Post a ridesheet as a thread reply to an event message."""
    meta = {
        "source": "event",
        "title": title,
        "location": location or "TBD",
        "start_date": date_str,
        "end_date": date_str,
        "dates": f"{date_str} to {date_str}",
        "start_time": start_time_str,
        "end_time": end_time_str,
        "mode": mode,
        "channel_id": channel_id,
        "pinned": False,
    }
    state = {"metadata": meta, "cars": {}}
    slack_text = _slack_ridesheet_text(state)

    res = client.chat_postMessage(
        channel=channel_id,
        thread_ts=thread_ts,
        text=slack_text,
        blocks=_build_ridesheet_blocks(state, channel_id, ""),
    )
    ts = res["ts"]
    save_state(channel_id, ts, state)

    # Re-render with final ts so the web join URL resolves correctly
    client.chat_update(
        channel=channel_id,
        ts=ts,
        text=slack_text,
        blocks=_build_ridesheet_blocks(state, channel_id, ts),
    )
    return ts


def _build_ridesheet_meta_modal(meta, initial_data=None):
    """Build the Create Ridesheet modal. Exported so other features can open it."""
    if initial_data is None:
        initial_data = {}

    is_random = meta.get("ridesheet_mode") == "random"

    init_title = initial_data.get("title") or ""
    init_loc = initial_data.get("location") or ""
    init_start = initial_data.get("start_date")
    init_end = initial_data.get("end_date")
    init_start_time = initial_data.get("start_time")
    init_end_time = initial_data.get("end_time")

    blocks = []

    if is_random:
        blocks.append({
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": "🎲 *Blind Random Mode* — Passengers will be randomly assigned to cars."}
            ]
        })

    blocks += [
        {
            "type": "input",
            "block_id": "title_block",
            "element": {
                "type": "plain_text_input",
                "action_id": "title_input",
                "initial_value": init_title
            },
            "label": {"type": "plain_text", "text": "Event Title"}
        },
        {
            "type": "input",
            "block_id": "location_block",
            "element": {
                "type": "plain_text_input",
                "action_id": "location_input",
                "initial_value": init_loc
            },
            "label": {"type": "plain_text", "text": "Location"}
        }
    ]

    start_element = {"type": "datepicker", "action_id": "start_date_input"}
    if init_start:
        start_element["initial_date"] = init_start

    blocks.append({
        "type": "input",
        "block_id": "start_date_block",
        "element": start_element,
        "label": {"type": "plain_text", "text": "Start Date"}
    })

    end_element = {"type": "datepicker", "action_id": "end_date_input"}
    if init_end:
        end_element["initial_date"] = init_end

    blocks.append({
        "type": "input",
        "block_id": "end_date_block",
        "element": end_element,
        "label": {"type": "plain_text", "text": "End Date"}
    })

    start_time_element = {"type": "timepicker", "action_id": "start_time_input"}
    if init_start_time:
        start_time_element["initial_time"] = init_start_time

    blocks.append({
        "type": "input",
        "block_id": "start_time_block",
        "element": start_time_element,
        "label": {"type": "plain_text", "text": "Start Time"},
        "optional": True
    })

    end_time_element = {"type": "timepicker", "action_id": "end_time_input"}
    if init_end_time:
        end_time_element["initial_time"] = init_end_time

    blocks.append({
        "type": "input",
        "block_id": "end_time_block",
        "element": end_time_element,
        "label": {"type": "plain_text", "text": "End Time"},
        "optional": True
    })

    return {
        "type": "modal",
        "callback_id": "ridesheet_meta_modal",
        "private_metadata": json.dumps(meta),
        "title": {"type": "plain_text", "text": "Create Ridesheet"},
        "submit": {"type": "plain_text", "text": "Create"},
        "blocks": blocks
    }


def register_ridesheet_handlers(app):

    @app.command("/ridesheet")
    def cmd_ridesheet(ack, body, client):
        ack()
        text = body.get("text", "").strip().lower()
        ridesheet_mode = "random" if text == "random" else "normal"
        meta = {"mode": "create", "channel_id": body.get("channel_id"), "ridesheet_mode": ridesheet_mode}
        client.views_open(
            trigger_id=body["trigger_id"],
            view=_build_ridesheet_meta_modal(meta)
        )

    @app.view("ridesheet_meta_modal")
    def handle_meta_submit(ack, body, client, view):
        ack()
        meta = json.loads(view["private_metadata"])
        vals = view["state"]["values"]

        start_date = vals["start_date_block"]["start_date_input"]["selected_date"]
        end_date = vals["end_date_block"]["end_date_input"]["selected_date"]
        start_time = (vals.get("start_time_block") or {}).get("start_time_input", {}).get("selected_time")
        end_time = (vals.get("end_time_block") or {}).get("end_time_input", {}).get("selected_time")

        new_meta = {
            "title": vals["title_block"]["title_input"]["value"],
            "location": vals["location_block"]["location_input"]["value"],
            "start_date": start_date,
            "end_date": end_date,
            "dates": f"{start_date} to {end_date}",
            "start_time": start_time,
            "end_time": end_time,
        }

        if meta.get("ridesheet_mode"):
            new_meta["mode"] = meta["ridesheet_mode"]

        channel_id = meta["channel_id"]
        thread_ts = meta.get("thread_ts")
        new_meta["channel_id"] = channel_id
        new_meta["pinned"] = True
        state = {"metadata": new_meta, "cars": {}}

        try:
            client.conversations_join(channel=channel_id)
        except Exception:
            pass

        post_kwargs = {"channel": channel_id, "text": "🚗 Generating Ridesheet...", "blocks": _build_ridesheet_blocks(state, channel_id, "")}
        if thread_ts:
            post_kwargs["thread_ts"] = thread_ts

        res = client.chat_postMessage(**post_kwargs)
        ts = res["ts"]

        state["metadata"]["message_ts"] = ts
        save_state(channel_id, ts, state)

        blocks = _build_ridesheet_blocks(state, channel_id, ts)
        client.chat_update(channel=channel_id, ts=ts, text=f"🚗 Ridesheet: {new_meta['title']}", blocks=blocks)

        if not thread_ts:
            try:
                client.pins_add(channel=channel_id, timestamp=ts)
            except Exception as e:
                print(f"Failed to pin ridesheet: {e}")

    @app.action("ridesheet_open_web")
    def action_open_web(ack, body, client):
        ack()

    threading.Thread(target=_ridesheet_cleanup_loop, daemon=True).start()

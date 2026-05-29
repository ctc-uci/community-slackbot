import json
import re
from slack_sdk.errors import SlackApiError # type: ignore
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

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"🚗 {meta.get('title', 'Carpool Ridesheet')}"}
        },
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f"📍 *Location:* {meta.get('location', 'TBD')}"},
                {"type": "mrkdwn", "text": f"📅 *Dates:* {meta.get('dates', 'TBD')}"}
            ]
        },
        {"type": "divider"}
    ]

    if is_random:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "🎲 *Blind Random Mode* — Passengers are randomly assigned to cars. Join from the website to get a random seat. You won't be able to see who else is in your car!"
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
        dir_str = ""
        if direction == "there":
            dir_str = "\n⚠️ *Note:* Driving THERE only"
        elif direction == "return":
            dir_str = "\n⚠️ *Note:* Returning ONLY"

        desc = car.get("description", "").strip()
        desc_str = f"\n📝 *Notes:* {desc}" if desc else ""

        if is_random:
            row_text = (
                f"🚗 *Driver:* {_fmt_user(driver_id)}  |  🕰️ *Leaves:* {car.get('departure', 'TBD')}  |  💺 *Seats:* {len(passengers)}/{capacity} filled"
                f"{dir_str}"
                f"{desc_str}"
            )
        else:
            pass_str = ", ".join(_fmt_user(p) for p in passengers) if passengers else "_None yet_"
            row_text = (
                f"🚗 *Driver:* {_fmt_user(driver_id)}  |  🕰️ *Leaves:* {car.get('departure', 'TBD')}  |  💺 *Capacity:* {len(passengers)}/{capacity}"
                f"{dir_str}\n"
                f"🧍 *Passengers:* {pass_str}"
                f"{desc_str}"
            )

        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": row_text}
        })

        blocks.append({
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "💬 Make Group Chat"},
                    "action_id": "ridesheet_make_group_chat",
                    "value": f"{channel_id}|{message_ts}|{driver_id}"
                }
            ]
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
                    "text": {"type": "plain_text", "text": "🌐 Manage Ridesheet"},
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
    title = state.get("metadata", {}).get("title", "Ridesheet")
    try:
        client.chat_update(channel=channel_id, ts=message_ts, text=f"🚗 Ridesheet: {title}", blocks=blocks)
    except Exception as e:
        print(f"Failed to refresh ridesheet Slack message: {e}")

def register_ridesheet_handlers(app):

    def _build_meta_modal(meta, initial_data=None):
        if initial_data is None:
            initial_data = {}

        is_random = meta.get("ridesheet_mode") == "random"

        init_title = initial_data.get("title") or ""
        init_loc = initial_data.get("location") or ""
        init_start = initial_data.get("start_date")
        init_end = initial_data.get("end_date")

        blocks = []

        if is_random:
            blocks.append({
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": "🎲 *Blind Random Mode* — Passengers will be randomly assigned to cars and won't see who they're riding with."}
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

        return {
            "type": "modal",
            "callback_id": "ridesheet_meta_modal",
            "private_metadata": json.dumps(meta),
            "title": {"type": "plain_text", "text": "Create Ridesheet"},
            "submit": {"type": "plain_text", "text": "Create"},
            "blocks": blocks
        }

    @app.command("/ridesheet")
    def cmd_ridesheet(ack, body, client):
        ack()
        text = body.get("text", "").strip().lower()
        ridesheet_mode = "random" if text == "random" else "normal"
        meta = {"mode": "create", "channel_id": body.get("channel_id"), "ridesheet_mode": ridesheet_mode}
        client.views_open(
            trigger_id=body["trigger_id"],
            view=_build_meta_modal(meta)
        )

    @app.view("ridesheet_meta_modal")
    def handle_meta_submit(ack, body, client, view):
        ack()
        meta = json.loads(view["private_metadata"])
        vals = view["state"]["values"]

        start_date = vals["start_date_block"]["start_date_input"]["selected_date"]
        end_date = vals["end_date_block"]["end_date_input"]["selected_date"]

        new_meta = {
            "title": vals["title_block"]["title_input"]["value"],
            "location": vals["location_block"]["location_input"]["value"],
            "start_date": start_date,
            "end_date": end_date,
            "dates": f"{start_date} to {end_date}"
        }

        if meta.get("ridesheet_mode"):
            new_meta["mode"] = meta["ridesheet_mode"]

        channel_id = meta["channel_id"]
        new_meta["pinned"] = True
        state = {"metadata": new_meta, "cars": {}}

        blocks = _build_ridesheet_blocks(state, channel_id, "")
        res = client.chat_postMessage(channel=channel_id, text="🚗 Generating Ridesheet...", blocks=blocks)
        ts = res["ts"]

        save_state(channel_id, ts, state)

        blocks = _build_ridesheet_blocks(state, channel_id, ts)
        client.chat_update(channel=channel_id, ts=ts, text=f"🚗 Ridesheet: {new_meta['title']}", blocks=blocks)

        try:
            client.pins_add(channel=channel_id, timestamp=ts)
        except Exception as e:
            print(f"Failed to pin ridesheet: {e}")

    @app.action("ridesheet_open_web")
    def action_open_web(ack, body, client):
        ack()

    def _invite_to_gc(client, gc_id, user_ids):
        """Invite users to an existing conversation, ignoring already-in-channel errors."""
        for uid in user_ids:
            try:
                client.conversations_invite(channel=gc_id, users=uid)
            except SlackApiError as e:
                if e.response.get("error") != "already_in_channel":
                    raise

    @app.action("ridesheet_make_group_chat")
    def action_make_group_chat(ack, body, client):
        ack()
        val = body["actions"][0]["value"]
        chan, ts, driver_id = val.split("|")
        user_id = body["user"]["id"]

        state = get_state(chan, ts)
        if not state or driver_id not in state.get("cars", {}): return

        car = state["cars"][driver_id]
        users_to_invite = list(set([driver_id] + car["passengers"] + [user_id]))

        if len(users_to_invite) <= 1:
            client.chat_postEphemeral(channel=chan, user=user_id, text="You need at least one passenger to start a group chat!")
            return

        existing_gc = car.get("group_chat_id")

        try:
            if existing_gc:
                _invite_to_gc(client, existing_gc, users_to_invite)
                client.chat_postEphemeral(channel=chan, user=user_id, text="Group chat already exists — any new members have been added! 💬")
            else:
                res = client.conversations_open(users=",".join(users_to_invite))
                mpim_id = res["channel"]["id"]

                state["cars"][driver_id]["group_chat_id"] = mpim_id
                save_state(chan, ts, state)

                title = state["metadata"].get("title", "Upcoming Trip")
                client.chat_postMessage(channel=mpim_id, text=f"🚗 Ridesheet chat for *{title}*.")
                client.chat_postEphemeral(channel=chan, user=user_id, text="Group chat created successfully! 💬")
        except SlackApiError as e:
            client.chat_postEphemeral(channel=chan, user=user_id, text=f"Could not open group chat: `{e.response['error']}` (Ensure the bot has `mpim:write` or `conversations:write` scopes).")

    threading.Thread(target=_ridesheet_cleanup_loop, daemon=True).start()

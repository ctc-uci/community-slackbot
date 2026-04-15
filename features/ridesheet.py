import json
from slack_sdk.errors import SlackApiError # type: ignore
from firebase_admin import firestore # type: ignore
import os
import time
from datetime import datetime
from slack_sdk import WebClient # type: ignore
import threading
# Import your custom Firebase client (Adjust the import path if necessary)
from firebase_client import get_firebase_app

# Initialize Firebase using your custom client wrapper
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
                    channel_id, ts = doc.id.split("-") 
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

def register_ridesheet_handlers(app):

    def _build_ridesheet_blocks(state, channel_id, message_ts):
        """Builds the Slack blocks for the ridesheet message."""
        meta = state.get("metadata", {})
        
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

        cars = state.get("cars", {})
        if not cars:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": "_No cars added yet. Be the first to volunteer to drive!_"}
            })

        for driver_id, car in cars.items():
            passengers = car.get("passengers", [])
            capacity = car.get("capacity", 4)
            pass_str = ", ".join(f"<@{p}>" for p in passengers) if passengers else "_None yet_"
            
            # Fetch direction and create the warning string if needed
            direction = car.get("direction", "both")
            dir_str = ""
            if direction == "there":
                dir_str = "\n⚠️ *Note:* Driving THERE only"
            elif direction == "return":
                dir_str = "\n⚠️ *Note:* Returning ONLY"

            desc = car.get("description", "").strip()
            desc_str = f"\n📝 *Notes:* {desc}" if desc else ""
            
            row_text = (
                f"🚗 *Driver:* <@{driver_id}>  |  🕰️ *Leaves:* {car.get('departure', 'TBD')}  |  💺 *Capacity:* {len(passengers)}/{capacity}"
                f"{dir_str}\n"
                f"🧍 *Passengers:* {pass_str}"
                f"{desc_str}"
            )

            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": row_text}
            })

            btn_val = f"{channel_id}|{message_ts}|{driver_id}"
            
            # Add buttons for this specific car
            blocks.append({
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "🙋 Join / Leave"},
                        "action_id": "ridesheet_join_passenger",
                        "value": btn_val
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "💬 Make Group Chat"},
                        "action_id": "ridesheet_make_group_chat",
                        "value": btn_val
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "🗑️ Remove Car"},
                        "style": "danger",
                        "action_id": "ridesheet_remove_car",
                        "value": btn_val,
                        "confirm": {
                            "title": {"type": "plain_text", "text": "Remove your car?"},
                            "text": {"type": "plain_text", "text": "This will remove your car from the ridesheet entirely."},
                            "confirm": {"type": "plain_text", "text": "Remove"},
                            "deny": {"type": "plain_text", "text": "Cancel"}
                        }
                    }
                ]
            })
            blocks.append({"type": "divider"})

        val_meta = f"{channel_id}|{message_ts}" if message_ts else "new"
        blocks.append({
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "🚘 Add / Edit Car"},
                    "action_id": "ridesheet_join_driver",
                    "value": val_meta,
                    "style": "primary"
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✏️ Edit Details"},
                    "action_id": "ridesheet_edit_meta",
                    "value": val_meta
                }
            ]
        })

        return blocks

    @app.command("/ridesheet")
    def cmd_ridesheet(ack, body, client):
        ack()
        meta = {"mode": "create", "channel_id": body.get("channel_id")}
        client.views_open(
            trigger_id=body["trigger_id"],
            view=_build_meta_modal(meta)
        )

    @app.action("ridesheet_edit_meta")
    def action_edit_meta(ack, body, client):
        ack()
        val = body["actions"][0]["value"]
        if not val or val == "new": return
        
        channel_id, message_ts = val.split("|")
        state = get_state(channel_id, message_ts)
        if not state: return

        meta = {"mode": "edit", "channel_id": channel_id, "message_ts": message_ts}
        m_data = state.get("metadata", {})
        client.views_open(
            trigger_id=body["trigger_id"],
            view=_build_meta_modal(meta, m_data)
        )

    def _build_meta_modal(meta, initial_data=None):
        if initial_data is None:
            initial_data = {}
            
        title = "Edit Ridesheet" if meta["mode"] == "edit" else "Create Ridesheet"
        submit = "Save" if meta["mode"] == "edit" else "Create"

        # Safely extract initial values
        init_title = initial_data.get("title") or ""
        init_loc = initial_data.get("location") or ""
        init_start = initial_data.get("start_date")
        init_end = initial_data.get("end_date")

        # Build the base blocks
        blocks = [
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

        # Dynamically build Start Date block
        start_element = {"type": "datepicker", "action_id": "start_date_input"}
        if init_start:
            start_element["initial_date"] = init_start

        blocks.append({
            "type": "input",
            "block_id": "start_date_block",
            "element": start_element,
            "label": {"type": "plain_text", "text": "Start Date"}
        })

        # Dynamically build End Date block
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
            "title": {"type": "plain_text", "text": title},
            "submit": {"type": "plain_text", "text": submit},
            "blocks": blocks
        }

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

        channel_id = meta["channel_id"]

        if meta["mode"] == "create":
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

        elif meta["mode"] == "edit":
            ts = meta["message_ts"]
            state = get_state(channel_id, ts)
            if state:
                state["metadata"].update(new_meta)
                save_state(channel_id, ts, state)
                blocks = _build_ridesheet_blocks(state, channel_id, ts)
                client.chat_update(channel=channel_id, ts=ts, text=f"🚗 Ridesheet: {new_meta['title']}", blocks=blocks)

    @app.action("ridesheet_join_driver")
    def action_join_driver(ack, body, client):
        ack()
        val = body["actions"][0]["value"]
        if not val or val == "new": return
        
        channel_id, message_ts = val.split("|")
        meta = {"channel_id": channel_id, "message_ts": message_ts}
        user_id = body["user"]["id"]

        state = get_state(channel_id, message_ts)
        car = state.get("cars", {}).get(user_id, {}) if state else {}
        
        existing_passengers = car.get("passengers", [])
        existing_desc = car.get("description", "")
        existing_direction = car.get("direction", "both") # Defaults to "both"
        
        passenger_element = {
            "type": "multi_users_select", 
            "action_id": "passengers_input", 
            "placeholder": {"type": "plain_text", "text": "Search for people..."}
        }
        if existing_passengers:
            passenger_element["initial_users"] = existing_passengers

        # Build description element safely
        desc_element = {
            "type": "plain_text_input", 
            "action_id": "description_input", 
            "multiline": True,
            "placeholder": {"type": "plain_text", "text": "e.g., Poop in trunk!"}
        }
        if existing_desc:
            desc_element["initial_value"] = existing_desc

        # Build direction element
        direction_options = [
            {"text": {"type": "plain_text", "text": "🔄 Both Ways (Default)"}, "value": "both"},
            {"text": {"type": "plain_text", "text": "➡️ Driving THERE only"}, "value": "there"},
            {"text": {"type": "plain_text", "text": "⬅️ Returning ONLY"}, "value": "return"}
        ]
        
        # Find the matching initial option based on saved state
        initial_dir_option = next((opt for opt in direction_options if opt["value"] == existing_direction), direction_options[0])

        client.views_open(
            trigger_id=body["trigger_id"],
            view={
                "type": "modal",
                "callback_id": "ridesheet_driver_modal",
                "private_metadata": json.dumps(meta),
                "title": {"type": "plain_text", "text": "Add or Edit Your Car"},
                "submit": {"type": "plain_text", "text": "Save Car"},
                "blocks": [
                    {
                        "type": "input",
                        "block_id": "capacity_block",
                        "element": {
                            "type": "static_select", 
                            "action_id": "capacity_input", 
                            "placeholder": {"type": "plain_text", "text": "Select seats available"},
                            "options": [
                                {
                                    "text": {"type": "plain_text", "text": f"{i} seats"}, 
                                    "value": str(i)
                                } for i in range(1, 10)
                            ]
                        },
                        "label": {"type": "plain_text", "text": "Passenger Capacity (Excluding you)"}
                    },
                    {
                        "type": "input",
                        "block_id": "departure_block",
                        "element": {
                            "type": "datetimepicker", 
                            "action_id": "departure_input"
                        },
                        "label": {"type": "plain_text", "text": "Departure Time"}
                    },
                    {
                        "type": "input",
                        "block_id": "direction_block",
                        "element": {
                            "type": "static_select",
                            "action_id": "direction_input",
                            "options": direction_options,
                            "initial_option": initial_dir_option
                        },
                        "label": {"type": "plain_text", "text": "Ride Direction"}
                    },
                    {
                        "type": "input",
                        "block_id": "passengers_block",
                        "optional": True,  
                        "element": passenger_element,
                        "label": {"type": "plain_text", "text": "Manage Passengers"}
                    },
                    {
                        "type": "input",
                        "block_id": "description_block",
                        "optional": True,  
                        "element": desc_element,
                        "label": {"type": "plain_text", "text": "Notes / Description"}
                    }
                ]
            }
        )

    @app.view("ridesheet_driver_modal")
    def handle_driver_submit(ack, body, client, view):
        ack()
        meta = json.loads(view["private_metadata"])
        chan = meta["channel_id"]
        ts = meta["message_ts"]
        user_id = body["user"]["id"]

        vals = view["state"]["values"]
        
        cap_str = vals["capacity_block"]["capacity_input"]["selected_option"]["value"]
        cap = int(cap_str)
            
        dep_timestamp = vals["departure_block"]["departure_input"]["selected_date_time"]
        dep = f"<!date^{dep_timestamp}^{{date_short}} at {{time}}|Time: {dep_timestamp}>"

        passengers = vals["passengers_block"]["passengers_input"]["selected_users"]
        if user_id in passengers:
            passengers.remove(user_id)

        desc = vals["description_block"]["description_input"]["value"] or ""
        
        direction = vals["direction_block"]["direction_input"]["selected_option"]["value"]

        state = get_state(chan, ts)
        if state:
            state.setdefault("cars", {})
            state["cars"][user_id] = {
                "capacity": cap,
                "departure": dep,
                "passengers": passengers,
                "description": desc,
                "direction": direction 
            }
            save_state(chan, ts, state)
            blocks = _build_ridesheet_blocks(state, chan, ts)
            client.chat_update(channel=chan, ts=ts, text="🚗 Ridesheet updated", blocks=blocks)

    @app.action("ridesheet_join_passenger")
    def action_join_passenger(ack, body, client):
        ack()
        val = body["actions"][0]["value"]
        chan, ts, driver_id = val.split("|")
        user_id = body["user"]["id"]

        state = get_state(chan, ts)
        if not state or driver_id not in state.get("cars", {}): return

        car = state["cars"][driver_id]

        if user_id == driver_id:
            client.chat_postEphemeral(channel=chan, user=user_id, text="You are the driver of this car! You don't need to join as a passenger. 🚘")
            return

        passengers = car["passengers"]
        if user_id in passengers:
            passengers.remove(user_id)
        else:
            if len(passengers) >= car["capacity"]:
                client.chat_postEphemeral(channel=chan, user=user_id, text="Sorry, this car is full! 🚙 Please join another or add your own.")
                return
            passengers.append(user_id)

        save_state(chan, ts, state)
        blocks = _build_ridesheet_blocks(state, chan, ts)
        client.chat_update(channel=chan, ts=ts, text="🚗 Ridesheet updated", blocks=blocks)

    @app.action("ridesheet_remove_car")
    def action_remove_car(ack, body, client):
        """Removes a car from the ridesheet state."""
        ack()
        val = body["actions"][0]["value"]
        chan, ts, driver_id = val.split("|")
        user_id = body["user"]["id"]

        if user_id != driver_id:
            client.chat_postEphemeral(channel=chan, user=user_id, text="Only the driver can remove their own car! 🛑")
            return

        state = get_state(chan, ts)
        if state and driver_id in state.get("cars", {}):
            del state["cars"][driver_id]
            save_state(chan, ts, state)
            blocks = _build_ridesheet_blocks(state, chan, ts)
            client.chat_update(channel=chan, ts=ts, text="🚗 Ridesheet updated", blocks=blocks)

    @app.action("ridesheet_make_group_chat")
    def action_make_group_chat(ack, body, client):
        ack()
        val = body["actions"][0]["value"]
        chan, ts, driver_id = val.split("|")
        user_id = body["user"]["id"]

        state = get_state(chan, ts)
        if not state or driver_id not in state.get("cars", {}): return

        car = state["cars"][driver_id]
        users_to_invite = [driver_id] + car["passengers"]

        if user_id not in users_to_invite:
            users_to_invite.append(user_id)

        users_to_invite = list(set(users_to_invite))

        if len(users_to_invite) <= 1:
            client.chat_postEphemeral(channel=chan, user=user_id, text="You need at least one passenger to start a group chat!")
            return

        try:
            res = client.conversations_open(users=",".join(users_to_invite))
            mpim_id = res["channel"]["id"]
            
            title = state['metadata'].get('title', 'Upcoming Trip')
            client.chat_postMessage(
                channel=mpim_id,
                text=f"🚗 Ridesheet chat for *{title}*."
            )
            client.chat_postEphemeral(channel=chan, user=user_id, text="Group chat created successfully!")
        except SlackApiError as e:
            client.chat_postEphemeral(channel=chan, user=user_id, text=f"Could not open group chat: `{e.response['error']}` (Ensure the bot has `mpim:write` or `conversations:write` scopes).")

    threading.Thread(target=_ridesheet_cleanup_loop, daemon=True).start()
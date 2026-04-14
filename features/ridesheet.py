"""
Ridesheet creation Slack bot: A tool to facilitate carpool coordination for events. 
Users can create a ridesheet, add their car with details, and others can join as passengers. 
The bot also supports creating group chats for each car.
"""

import json
from slack_sdk.errors import SlackApiError

# In-memory state
# Structure: { "channel_id": { "message_ts": { "metadata": {...}, "cars": {...} } } }
ridesheets_state = {}

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
            
            # The '3-column' approximation using a formatted text block
            row_text = (
                f"🚗 *Driver:* <@{driver_id}>  |  🕰️ *Leaves:* {car.get('departure', 'TBD')}  |  💺 *Capacity:* {len(passengers)}/{capacity}\n"
                f"🧍 *Passengers:* {pass_str}"
            )

            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": row_text}
            })

            # Buttons context value string
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
                    }
                ]
            })
            blocks.append({"type": "divider"})

        # Global actions at the bottom of the sheet
        val_meta = f"{channel_id}|{message_ts}" if message_ts else "new"
        blocks.append({
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "🚘 Add Car"},
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
        """Triggered when someone types /ridesheet."""
        ack()
        channel_id = body.get("channel_id")
        
        # Open the creation modal
        meta = {"mode": "create", "channel_id": channel_id}
        client.views_open(
            trigger_id=body["trigger_id"],
            view=_build_meta_modal(meta)
        )

    @app.action("ridesheet_edit_meta")
    def action_edit_meta(ack, body, client):
        """Triggered when someone clicks 'Edit Details'."""
        ack()
        val = body["actions"][0]["value"]
        if not val or val == "new": return
        
        channel_id, message_ts = val.split("|")
        state = ridesheets_state.get(channel_id, {}).get(message_ts)
        if not state: return

        meta = {"mode": "edit", "channel_id": channel_id, "message_ts": message_ts}
        m_data = state["metadata"]
        client.views_open(
            trigger_id=body["trigger_id"],
            view=_build_meta_modal(meta, m_data)
        )

    def _build_meta_modal(meta, initial_data=None):
        if initial_data is None:
            initial_data = {}
            
        title = "Edit Ridesheet" if meta["mode"] == "edit" else "Create Ridesheet"
        submit = "Save" if meta["mode"] == "edit" else "Create"

        view = {
            "type": "modal",
            "callback_id": "ridesheet_meta_modal",
            "private_metadata": json.dumps(meta),
            "title": {"type": "plain_text", "text": title},
            "submit": {"type": "plain_text", "text": submit},
            "blocks": [
                {
                    "type": "input",
                    "block_id": "title_block",
                    "element": {
                        "type": "plain_text_input", 
                        "action_id": "title_input",
                        "initial_value": initial_data.get("title", "")
                    },
                    "label": {"type": "plain_text", "text": "Event Title"}
                },
                {
                    "type": "input",
                    "block_id": "location_block",
                    "element": {
                        "type": "plain_text_input", 
                        "action_id": "location_input",
                        "initial_value": initial_data.get("location", "")
                    },
                    "label": {"type": "plain_text", "text": "Location"}
                },
                {
                    "type": "input",
                    "block_id": "dates_block",
                    "element": {
                        "type": "plain_text_input", 
                        "action_id": "dates_input",
                        "placeholder": {"type": "plain_text", "text": "e.g., April 20th - 22nd"},
                        "initial_value": initial_data.get("dates", "")
                    },
                    "label": {"type": "plain_text", "text": "Dates"}
                }
            ]
        }
        return view

    @app.view("ridesheet_meta_modal")
    def handle_meta_submit(ack, body, client, view):
        """Processes the creation/edit form submission."""
        ack()
        meta = json.loads(view["private_metadata"])
        vals = view["state"]["values"]

        title = vals["title_block"]["title_input"]["value"]
        location = vals["location_block"]["location_input"]["value"]
        dates = vals["dates_block"]["dates_input"]["value"]

        channel_id = meta["channel_id"]

        if meta["mode"] == "create":
            state = {
                "metadata": {"title": title, "location": location, "dates": dates},
                "cars": {}
            }
            # Post initial message to get the timestamp identifier
            blocks = _build_ridesheet_blocks(state, channel_id, "")
            res = client.chat_postMessage(channel=channel_id, text=f"🚗 Ridesheet: {title}", blocks=blocks)
            
            ts = res["ts"]
            if channel_id not in ridesheets_state:
                ridesheets_state[channel_id] = {}
            ridesheets_state[channel_id][ts] = state

            # Update immediately so the buttons have the valid ts attached
            blocks = _build_ridesheet_blocks(state, channel_id, ts)
            client.chat_update(channel=channel_id, ts=ts, text=f"🚗 Ridesheet: {title}", blocks=blocks)

        elif meta["mode"] == "edit":
            ts = meta["message_ts"]
            state = ridesheets_state.get(channel_id, {}).get(ts)
            if state:
                state["metadata"].update({"title": title, "location": location, "dates": dates})
                blocks = _build_ridesheet_blocks(state, channel_id, ts)
                client.chat_update(channel=channel_id, ts=ts, text=f"🚗 Ridesheet: {title}", blocks=blocks)

    @app.action("ridesheet_join_driver")
    def action_join_driver(ack, body, client):
        """Opens the modal to add a new car."""
        ack()
        val = body["actions"][0]["value"]
        if not val or val == "new": return
        
        channel_id, message_ts = val.split("|")
        meta = {"channel_id": channel_id, "message_ts": message_ts}
        
        client.views_open(
            trigger_id=body["trigger_id"],
            view={
                "type": "modal",
                "callback_id": "ridesheet_driver_modal",
                "private_metadata": json.dumps(meta),
                "title": {"type": "plain_text", "text": "Add Your Car"},
                "submit": {"type": "plain_text", "text": "Add Car"},
                "blocks": [
                    {
                        "type": "input",
                        "block_id": "capacity_block",
                        "element": {
                            "type": "plain_text_input", 
                            "action_id": "capacity_input", 
                            "placeholder": {"type": "plain_text", "text": "e.g., 4 (Exclude yourself)"}
                        },
                        "label": {"type": "plain_text", "text": "Passenger Capacity"}
                    },
                    {
                        "type": "input",
                        "block_id": "departure_block",
                        "element": {
                            "type": "plain_text_input", 
                            "action_id": "departure_input", 
                            "placeholder": {"type": "plain_text", "text": "e.g., Friday 9:00 AM"}
                        },
                        "label": {"type": "plain_text", "text": "Departure Time"}
                    }
                ]
            }
        )

    @app.view("ridesheet_driver_modal")
    def handle_driver_submit(ack, body, client, view):
        """Processes the driver submission and adds row to state."""
        ack()
        meta = json.loads(view["private_metadata"])
        chan = meta["channel_id"]
        ts = meta["message_ts"]

        vals = view["state"]["values"]
        try:
            # Safely parse integer or default to 4
            cap = int(vals["capacity_block"]["capacity_input"]["value"].strip())
        except ValueError:
            cap = 4 
            
        dep = vals["departure_block"]["departure_input"]["value"]
        user_id = body["user"]["id"]

        state = ridesheets_state.get(chan, {}).get(ts)
        if state:
            state["cars"][user_id] = {
                "capacity": cap,
                "departure": dep,
                "passengers": []
            }
            blocks = _build_ridesheet_blocks(state, chan, ts)
            client.chat_update(channel=chan, ts=ts, text="🚗 Ridesheet updated", blocks=blocks)

    @app.action("ridesheet_join_passenger")
    def action_join_passenger(ack, body, client):
        """Appends (or removes) a user from a specific car's passenger list."""
        ack()
        val = body["actions"][0]["value"]
        chan, ts, driver_id = val.split("|")
        user_id = body["user"]["id"]

        state = ridesheets_state.get(chan, {}).get(ts)
        if not state or driver_id not in state["cars"]: return

        car = state["cars"][driver_id]

        if user_id == driver_id:
            client.chat_postEphemeral(channel=chan, user=user_id, text="You are the driver of this car! You don't need to join as a passenger. 🚘")
            return

        passengers = car["passengers"]
        if user_id in passengers:
            # Toggle off: Leave car
            passengers.remove(user_id)
        else:
            # Toggle on: Join car (enforce capacity)
            if len(passengers) >= car["capacity"]:
                client.chat_postEphemeral(channel=chan, user=user_id, text="Sorry, this car is full! 🚙 Please join another or add your own.")
                return
            passengers.append(user_id)

        blocks = _build_ridesheet_blocks(state, chan, ts)
        client.chat_update(channel=chan, ts=ts, text="🚗 Ridesheet updated", blocks=blocks)

    @app.action("ridesheet_make_group_chat")
    def action_make_group_chat(ack, body, client):
        """Creates a multiparty DM (MPIM) between the driver and their passengers."""
        ack()
        val = body["actions"][0]["value"]
        chan, ts, driver_id = val.split("|")
        user_id = body["user"]["id"]

        state = ridesheets_state.get(chan, {}).get(ts)
        if not state or driver_id not in state["cars"]: return

        car = state["cars"][driver_id]
        users_to_invite = [driver_id] + car["passengers"]

        # Ensure the person clicking the button is added to the chat, even if they're just an admin
        if user_id not in users_to_invite:
            users_to_invite.append(user_id)

        # distinct list
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
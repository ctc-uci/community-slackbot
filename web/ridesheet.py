"""Ridesheet SPA, JSON API, and refresh webhook."""
import random
from datetime import datetime
from pathlib import Path

from flask import Blueprint, jsonify, render_template_string, request

bp = Blueprint("ridesheet", __name__)

_SPA_HTML = Path(__file__).resolve().parent.joinpath("ridesheet_spa.html").read_text()


@bp.post("/ridesheet/refresh")
def ridesheet_refresh():
    data = request.get_json(silent=True) or {}
    channel_id = data.get("channel_id")
    message_ts = data.get("message_ts")
    if not channel_id or not message_ts:
        return jsonify({"error": "Missing channel_id or message_ts"}), 400
    from features.ridesheet import refresh_ridesheet_message

    refresh_ridesheet_message(channel_id, message_ts)
    return jsonify({"ok": True})


@bp.get("/<channel_id>/<message_ts>")
def ridesheet_page(channel_id, message_ts):
    from features.ridesheet import get_state

    state = get_state(channel_id, message_ts)
    if not state:
        return "Ridesheet not found.", 404
    return render_template_string(
        _SPA_HTML,
        state=state,
        channel_id=channel_id,
        message_ts=message_ts,
    )


def _action(channel_id, message_ts, fn):
    """Run fn(user_id, state, data), persist, refresh Slack, return JSON state."""
    from features.ridesheet import get_state, refresh_ridesheet_message, save_state

    data = request.get_json(silent=True) or {}
    user_id = data.get("user_id", "").strip()
    if not user_id:
        return jsonify({"error": "user_id required"}), 400
    state = get_state(channel_id, message_ts)
    if not state:
        return jsonify({"error": "ridesheet not found"}), 404
    err = fn(user_id, state, data)
    if err:
        return jsonify({"error": err}), 400
    save_state(channel_id, message_ts, state)
    refresh_ridesheet_message(channel_id, message_ts)
    return jsonify({"ok": True, "state": state})


@bp.post("/<channel_id>/<message_ts>/join/<driver_id>")
def ridesheet_join(channel_id, message_ts, driver_id):
    def fn(uid, state, data):
        car = state.get("cars", {}).get(driver_id)
        if not car:
            return "car not found"
        if uid == driver_id:
            return "you are the driver"
        phones = car.setdefault("passenger_phones", {})
        if uid in car["passengers"]:
            car["passengers"].remove(uid)
            phones.pop(uid, None)
        else:
            if len(car["passengers"]) >= car["capacity"]:
                return "car is full"
            for other_car in state.get("cars", {}).values():
                if uid in other_car.get("passengers", []):
                    other_car["passengers"].remove(uid)
                    other_car.get("passenger_phones", {}).pop(uid, None)
            car["passengers"].append(uid)
            if data.get("passenger_phone"):
                phones[uid] = data["passenger_phone"].strip()

    return _action(channel_id, message_ts, fn)


@bp.post("/<channel_id>/<message_ts>/remove-passenger/<driver_id>")
def ridesheet_remove_passenger(channel_id, message_ts, driver_id):
    def fn(uid, state, data):
        passenger_id = data.get("passenger_id", "").strip()
        if not passenger_id:
            return "passenger_id required"
        car = state.get("cars", {}).get(driver_id)
        if not car:
            return "car not found"
        if passenger_id not in car.get("passengers", []):
            return "passenger not found"
        car["passengers"].remove(passenger_id)
        car.get("passenger_phones", {}).pop(passenger_id, None)

    return _action(channel_id, message_ts, fn)


@bp.post("/<channel_id>/<message_ts>/add-car")
def ridesheet_add_car(channel_id, message_ts):
    def fn(uid, state, data):
        dep_str = "TBD"
        raw = data.get("departure", "").strip()
        if raw:
            try:
                ts = int(datetime.fromisoformat(raw).timestamp())
                dep_str = f"<!date^{ts}^{{date_short}} at {{time}}|{raw}>"
            except ValueError:
                dep_str = raw
        rename_from = data.get("rename_from", "").strip()
        if rename_from and rename_from != uid:
            old = state.get("cars", {}).pop(rename_from, {})
            existing = old.get("passengers", [])
        else:
            existing = state.get("cars", {}).get(uid, {}).get("passengers", [])
        state.setdefault("cars", {})[uid] = {
            "capacity": int(data.get("capacity", 4)),
            "phone": data.get("phone", "").strip(),
            "departure": dep_str,
            "return_time": data.get("return_time", "").strip(),
            "passengers": existing,
            "passenger_phones": state.get("cars", {}).get(uid, {}).get("passenger_phones", {}),
            "description": data.get("description", "").strip(),
            "direction": data.get("direction", "both"),
        }

    return _action(channel_id, message_ts, fn)


@bp.post("/<channel_id>/<message_ts>/remove-car")
def ridesheet_remove_car(channel_id, message_ts):
    def fn(uid, state, _):
        if uid not in state.get("cars", {}):
            return "no car to remove"
        del state["cars"][uid]

    return _action(channel_id, message_ts, fn)


@bp.post("/<channel_id>/<message_ts>/join-pool")
def ridesheet_join_pool(channel_id, message_ts):
    def fn(uid, state, _):
        if state.get("metadata", {}).get("mode") != "random":
            return "not random mode"
        cars = state.get("cars", {})
        assigned = next((d for d, c in cars.items() if uid in c.get("passengers", [])), None)
        if assigned:
            cars[assigned]["passengers"].remove(uid)
            return
        if uid in cars:
            return "you are a driver"
        avail = [d for d, c in cars.items() if len(c.get("passengers", [])) < c.get("capacity", 4)]
        if not avail:
            return "all cars are full"
        cars[random.choice(avail)]["passengers"].append(uid)

    return _action(channel_id, message_ts, fn)


@bp.post("/<channel_id>/<message_ts>/edit-meta")
def ridesheet_edit_meta(channel_id, message_ts):
    def fn(uid, state, data):
        m = state["metadata"]
        for key in ("title", "location", "start_date", "end_date"):
            if data.get(key):
                m[key] = data[key]
        for key in ("start_time", "end_time"):
            if key in data:
                m[key] = data[key]
        if data.get("start_date") and data.get("end_date"):
            m["dates"] = f"{data['start_date']} to {data['end_date']}"

    return _action(channel_id, message_ts, fn)

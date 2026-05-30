"""
Flask web server running alongside the Slack bot.
- Gmail OAuth: GET /gmail/oauth, /gmail/oauth/callback
- Ridesheet website: GET /<channel_id>/<message_ts>  (SPA)
- Ridesheet JSON API: POST /<channel_id>/<message_ts>/...
- Ridesheet refresh webhook: POST /ridesheet/refresh
"""
import os
import random
from datetime import datetime
from flask import Flask, jsonify, request, redirect, render_template_string

flask_app = Flask(__name__)
flask_app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-key")


# ---------------------------------------------------------------------------
# Gmail OAuth
# ---------------------------------------------------------------------------

@flask_app.get("/gmail/oauth")
def gmail_oauth():
    from features.gmail import _gmail_oauth_callback_base, get_gmail_oauth_authorization_url, GMAIL_OAUTH_CALLBACK_PATH
    base = _gmail_oauth_callback_base()
    auth_url, err = get_gmail_oauth_authorization_url()
    if err:
        redirect_uri = (base + GMAIL_OAUTH_CALLBACK_PATH) if base else "(not set)"
        return f"Gmail OAuth not available: {err}. redirect_uri: {redirect_uri}", 400
    if not auth_url:
        return "Gmail already authorized."
    return redirect(auth_url)


@flask_app.get("/gmail/oauth/debug")
def gmail_oauth_debug():
    from features.gmail import _gmail_oauth_callback_base, GMAIL_OAUTH_CALLBACK_PATH
    base = _gmail_oauth_callback_base()
    redirect_uri = (base + GMAIL_OAUTH_CALLBACK_PATH) if base else "(not set)"
    return f"redirect_uri: {redirect_uri}. Add this exact URL in Google Cloud Console."


@flask_app.get("/gmail/oauth/callback")
def gmail_oauth_callback():
    from features.gmail import _gmail_oauth_callback_base, complete_gmail_oauth
    base = _gmail_oauth_callback_base()
    if not base:
        return "GMAIL_REDIRECT_URI or RAILWAY_PUBLIC_DOMAIN not set.", 500
    qs = request.query_string.decode()
    full_url = base + request.path + ("?" + qs if qs else "")
    ok, err = complete_gmail_oauth(full_url)
    if ok:
        return "Gmail authorized. Token saved. You can close this tab."
    return f"Authorization failed: {err}", 500


# ---------------------------------------------------------------------------
# Ridesheet refresh webhook
# ---------------------------------------------------------------------------

@flask_app.post("/ridesheet/refresh")
def ridesheet_refresh():
    data = request.get_json(silent=True) or {}
    channel_id = data.get("channel_id")
    message_ts = data.get("message_ts")
    if not channel_id or not message_ts:
        return jsonify({"error": "Missing channel_id or message_ts"}), 400
    from features.ridesheet import refresh_ridesheet_message
    refresh_ridesheet_message(channel_id, message_ts)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Ridesheet SPA page
# ---------------------------------------------------------------------------

_SPA_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Ridesheet</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: Arial, sans-serif; background: #f0f0f0; font-size: 14px; }

    /* Sheet container */
    .sheet-wrap {
      background: white;
      border: 1px solid #bbb;
      margin: 16px auto;
      max-width: 1100px;
      box-shadow: 0 2px 8px rgba(0,0,0,0.12);
    }

    /* Purple header */
    .sheet-title {
      background: #7B68AE;
      color: white;
      text-align: center;
      padding: 14px 20px;
      font-size: 22px;
      font-weight: bold;
      letter-spacing: 0.3px;
      cursor: pointer;
    }
    .sheet-title:hover { background: #6e5da0; }
    .sheet-info-cell { cursor: pointer; }
    .sheet-info-cell:hover { background: #f5f3ff; }

    /* Info row */
    .sheet-info {
      display: flex;
      border-bottom: 1px solid #bbb;
    }
    .sheet-info-cell {
      flex: 1;
      padding: 9px 16px;
      border-right: 1px solid #bbb;
      font-size: 13px;
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .sheet-info-cell:last-child { border-right: none; }
    .sheet-info-cell a { color: #4a4aff; text-decoration: none; }
    .sheet-info-cell a:hover { text-decoration: underline; }

    /* Rides subheader */
    .sheet-rides-header {
      background: #7B68AE;
      color: white;
      text-align: center;
      padding: 7px 16px;
      font-weight: bold;
      font-size: 14px;
    }

    /* Spreadsheet table */
    .sheet-table-wrap { overflow-x: auto; }
    .sheet-table {
      width: 100%;
      border-collapse: collapse;
      min-width: 700px;
    }
    .sheet-table th {
      border: 1px solid #888;
      padding: 7px 10px;
      font-weight: bold;
      font-size: 13px;
      background: white;
      white-space: nowrap;
    }
    .sheet-table td {
      border: 1px solid #ccc;
      padding: 0;
      height: 34px;
      vertical-align: middle;
      font-size: 13px;
      position: relative;
    }
    .sheet-table td .cell-inner {
      padding: 4px 8px;
      min-height: 34px;
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .cell-occupied { cursor: pointer; }
    .cell-occupied:hover { background: #ffeaea; }
    .cell-notes { cursor: text; }
    .cell-notes:hover { background: #eef2fb; }
    .cell-mine { background: #e8f0fe; }
    .cell-crossed {
      background-color: #f0f0f0;
      background-image: repeating-linear-gradient(
        -45deg, transparent, transparent 4px, #d8d8d8 4px, #d8d8d8 5px
      );
    }
    .cell-driver { font-weight: bold; }
    .cell-driver-mine { background: #e8f0fe; }
    .join-btn {
      color: #1a73e8;
      background: none;
      border: none;
      cursor: pointer;
      font-size: 12px;
      padding: 0;
      opacity: 0;
      white-space: nowrap;
    }
    .sheet-table td:hover .join-btn { opacity: 1; }
    .leave-btn {
      color: #c00;
      background: none;
      border: none;
      cursor: pointer;
      font-size: 11px;
      padding: 0;
      margin-left: 4px;
    }
    .remove-btn {
      color: #c00;
      background: none;
      border: none;
      cursor: pointer;
      font-size: 11px;
      padding: 0 3px;
      margin-left: 4px;
    }
    .full-badge {
      font-size: 10px;
      color: #999;
      font-style: italic;
    }
    .dir-note {
      font-size: 11px;
      color: #b35a00;
    }
    .rand-badge {
      font-size: 11px;
      color: #666;
      font-style: italic;
    }

    /* Forms section */
    .forms-section {
      max-width: 560px;
      margin: 20px auto;
      padding: 0 16px 24px;
    }
    .form-card {
      background: white;
      border: 1px solid #ccc;
      border-radius: 4px;
      margin-bottom: 14px;
    }
    .form-card-header {
      background: #f4f4f4;
      border-bottom: 1px solid #ccc;
      padding: 8px 14px;
      font-weight: bold;
      font-size: 13px;
    }
    .form-card-body { padding: 12px 14px; }
    .form-row { margin-bottom: 10px; }
    .form-row label { display: block; font-size: 12px; color: #555; margin-bottom: 3px; }
    .form-row input, .form-row select, .form-row textarea {
      width: 100%;
      border: 1px solid #bbb;
      border-radius: 3px;
      padding: 5px 8px;
      font-size: 13px;
      font-family: Arial, sans-serif;
    }
    .form-row textarea { resize: vertical; }
    .btn-save {
      background: #7B68AE;
      color: white;
      border: none;
      border-radius: 3px;
      padding: 6px 16px;
      cursor: pointer;
      font-size: 13px;
    }
    .btn-save:hover { background: #6a58a0; }
    .btn-outline {
      background: white;
      color: #555;
      border: 1px solid #bbb;
      border-radius: 3px;
      padding: 6px 16px;
      cursor: pointer;
      font-size: 13px;
    }
    .btn-outline:hover { background: #f0f0f0; }
    .btn-danger {
      background: #dc3545;
      color: white;
      border: none;
      border-radius: 3px;
      padding: 5px 12px;
      cursor: pointer;
      font-size: 12px;
    }
    .btn-danger:hover { background: #b02a37; }
    .pool-section { text-align: center; padding: 12px; }
    .btn-pool {
      background: #f0a800;
      color: white;
      border: none;
      border-radius: 3px;
      padding: 8px 20px;
      cursor: pointer;
      font-size: 14px;
    }
    .btn-pool:hover { background: #d49400; }
    .random-banner {
      background: #fff3cd;
      border: 1px solid #ffc107;
      border-radius: 4px;
      padding: 10px 14px;
      margin-bottom: 14px;
      font-size: 13px;
    }

    /* Car modal */
    .modal-backdrop {
      display: none;
      position: fixed;
      inset: 0;
      background: rgba(0,0,0,0.35);
      z-index: 100;
      align-items: center;
      justify-content: center;
    }
    .modal-backdrop.open { display: flex; }
    .modal-box {
      background: white;
      border-radius: 6px;
      box-shadow: 0 4px 24px rgba(0,0,0,0.2);
      width: 340px;
      max-width: 95vw;
      padding: 20px;
    }
    .modal-box h3 { font-size: 15px; margin-bottom: 14px; }
    .modal-actions { display: flex; gap: 8px; margin-top: 14px; align-items: center; }
    .modal-actions .spacer { flex: 1; }
  </style>
</head>
<body>

<!-- Sheet -->
<div class="sheet-wrap">
  <div class="sheet-title" id="title-display" onclick="makeTitleEditable()" title="Click to edit"></div>

  <div class="sheet-info">
    <div class="sheet-info-cell" id="date-cell" onclick="makeDateEditable()" title="Click to edit"></div>
    <div class="sheet-info-cell" id="loc-cell" onclick="makeLocEditable()" title="Click to edit"></div>
  </div>

  <div class="sheet-rides-header">Rides (include your phone #)</div>

  <div class="sheet-table-wrap">
    <table class="sheet-table">
      <thead id="cars-thead">
        <tr>
          <th>Driver</th>
          <th>Departure Time</th>
          <th>Passenger 1</th>
          <th>Passenger 2</th>
          <th>Passenger 3</th>
          <th>Passenger 4</th>
          <th>Passenger 5</th>
          <th>Return Time</th>
        </tr>
      </thead>
      <tbody id="cars-tbody"></tbody>
    </table>
  </div>
</div>

<!-- Car modal -->
<div id="car-modal" class="modal-backdrop" onclick="if(event.target===this)closeCarModal()">
  <div class="modal-box">
    <h3 id="modal-title">Add My Car</h3>
    <div class="form-row">
      <label>Your Name</label>
      <input id="modal-name" placeholder="Name" autocomplete="off">
    </div>
    <div class="form-row">
      <label>Phone # <small style="color:#999">(optional)</small></label>
      <input id="modal-phone" placeholder="e.g. 555-1234" type="tel">
    </div>
    <div class="form-row">
      <label>Departure Time</label>
      <input type="time" id="modal-dep">
    </div>
    <div class="form-row">
      <label>Return Time <small style="color:#999">(optional)</small></label>
      <input type="time" id="modal-return">
    </div>
    <div class="form-row">
      <label>Passenger Capacity (excluding you)</label>
      <select id="modal-cap">
        <option value="1">1 seat</option><option value="2">2 seats</option>
        <option value="3">3 seats</option><option value="4" selected>4 seats</option>
        <option value="5">5 seats</option><option value="6">6 seats</option>
        <option value="7">7 seats</option><option value="8">8 seats</option>
        <option value="9">9 seats</option>
      </select>
    </div>
    <div class="form-row">
      <label>Direction</label>
      <select id="modal-dir">
        <option value="both">Both Ways</option>
        <option value="there">Driving THERE only</option>
        <option value="return">Returning ONLY</option>
      </select>
    </div>
    <div class="form-row">
      <label>Notes (optional)</label>
      <textarea id="modal-desc" rows="2" style="width:100%;border:1px solid #bbb;border-radius:3px;padding:5px 8px;font-size:13px;font-family:Arial,sans-serif;resize:vertical"></textarea>
    </div>
    <div class="modal-actions">
      <button class="btn-save" onclick="submitCar()">Save</button>
      <button class="btn-outline" onclick="closeCarModal()">Cancel</button>
      <div class="spacer"></div>
      <button id="modal-remove" class="btn-danger" style="display:none" onclick="armRemoveCar(this)">Remove Car</button>
    </div>
  </div>
</div>

<!-- Passenger modal -->
<div id="pass-modal" class="modal-backdrop" onclick="if(event.target===this)closePassengerModal()">
  <div class="modal-box">
    <h3>Join Car</h3>
    <div class="form-row">
      <label>Your Name</label>
      <input id="pass-name" placeholder="Name" autocomplete="off"
             onkeydown="if(event.key==='Enter') submitPassenger()">
    </div>
    <div class="form-row">
      <label>Phone # <small style="color:#999">(optional)</small></label>
      <input id="pass-phone" placeholder="e.g. 555-1234" type="tel"
             onkeydown="if(event.key==='Enter') submitPassenger()">
    </div>
    <div class="modal-actions">
      <button class="btn-save" onclick="submitPassenger()">Join</button>
      <button class="btn-outline" onclick="closePassengerModal()">Cancel</button>
    </div>
  </div>
</div>

<!-- Forms -->
<div class="forms-section">
  <div id="random-banner" class="random-banner" style="display:none">
    &#127922; <strong>Blind Random Mode</strong> — Passengers are randomly assigned to cars and won't see who they're riding with.
  </div>
  <div id="pool-section"></div>
</div>

<script>
const BASE = window.location.pathname.replace(/\\/$/, '');
let state = {{ state | tojson }};


// ── Helpers ───────────────────────────────────────────────────────────────────

function fmtDep(dep) {
  if (!dep || dep === 'TBD') return 'TBD';
  const m = dep.match(/<!date\\^(\\d+)\\^/);
  if (m) {
    return new Date(parseInt(m[1]) * 1000).toLocaleString('en-US',
      { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
  }
  const t = dep.match(/^(\\d{1,2}):(\\d{2})$/);
  if (t) {
    const h = parseInt(t[1]), min = t[2];
    return `${h % 12 || 12}:${min} ${h >= 12 ? 'PM' : 'AM'}`;
  }
  return dep;
}

function depToTime(dep) {
  if (!dep) return '';
  const m = dep.match(/<!date\\^(\\d+)\\^/);
  if (m) {
    const d = new Date(parseInt(m[1]) * 1000);
    return `${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`;
  }
  if (/^\\d{1,2}:\\d{2}$/.test(dep)) return dep.padStart(5, '0');
  return '';
}

function fmtDate(d) {
  if (!d) return 'TBD';
  const dt = new Date(d + 'T12:00:00');
  return dt.toLocaleDateString('en-US', { weekday: 'long', month: 'numeric', day: 'numeric', year: '2-digit' });
}

function esc(s) {
  return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;')
                        .replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

function isUrl(s) { return /^https?:\\/\\//i.test(s); }

// ── API ───────────────────────────────────────────────────────────────────────

async function apiAs(userId, path, extra = {}) {
  try {
    const r = await fetch(BASE + path, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ user_id: userId, ...extra })
    });
    const j = await r.json();
    if (j.error) { alert(j.error); return j; }
    if (j.state) { state = j.state; render(); }
    return j;
  } catch(e) { console.error(e); }
}

// ── Car modal ─────────────────────────────────────────────────────────────────

let _carModalDriverId = null;

function openCarModal(driverId) {
  _carModalDriverId = driverId || null;
  const car = driverId ? (state.cars || {})[driverId] : null;
  document.getElementById('modal-title').textContent = car ? 'Edit Car' : 'Add My Car';
  document.getElementById('modal-name').value    = driverId || '';
  document.getElementById('modal-name').readOnly = false;
  document.getElementById('modal-name').style.background = '';
  document.getElementById('modal-phone').value   = car ? (car.phone || '') : '';
  document.getElementById('modal-dep').value     = car ? depToTime(car.departure) : '';
  document.getElementById('modal-return').value  = car ? (car.return_time || '') : '';
  document.getElementById('modal-cap').value     = car ? String(car.capacity) : '4';
  document.getElementById('modal-dir').value     = car ? (car.direction || 'both') : 'both';
  document.getElementById('modal-desc').value    = car ? (car.description || '') : '';
  document.getElementById('modal-remove').style.display = car ? '' : 'none';
  document.getElementById('car-modal').classList.add('open');
  if (!car) document.getElementById('modal-name').focus();
  else document.getElementById('modal-dep').focus();
}

function closeCarModal() {
  document.getElementById('car-modal').classList.remove('open');
  const btn = document.getElementById('modal-remove');
  btn.dataset.armed = '';
  btn.textContent = 'Remove Car';
  btn.style.background = '';
}

async function submitCar() {
  const name = document.getElementById('modal-name').value.trim();
  if (!name) { alert('Please enter your name.'); return; }
  const dep  = document.getElementById('modal-dep').value;
  if (!dep)  { alert('Please set a departure time.'); return; }
  const extra = {
    capacity:    parseInt(document.getElementById('modal-cap').value),
    phone:       document.getElementById('modal-phone').value.trim(),
    departure:   dep,
    return_time: document.getElementById('modal-return').value,
    direction:   document.getElementById('modal-dir').value,
    description: document.getElementById('modal-desc').value,
  };
  if (_carModalDriverId && _carModalDriverId !== name) extra.rename_from = _carModalDriverId;
  await apiAs(name, '/add-car', extra);
  closeCarModal();
}

function armRemoveCar(btn) {
  if (btn.dataset.armed) {
    removeCarModal();
  } else {
    btn.dataset.armed = '1';
    btn.textContent = 'Confirm Remove';
    btn.style.background = '#a0000d';
    setTimeout(() => {
      if (btn.dataset.armed) {
        btn.dataset.armed = '';
        btn.textContent = 'Remove Car';
        btn.style.background = '';
      }
    }, 3000);
  }
}

async function removeCarModal() {
  const dId = _carModalDriverId;
  if (!dId) return;
  await apiAs(dId, '/remove-car');
  closeCarModal();
}

// ── Passenger modal ───────────────────────────────────────────────────────────

let _passModalDriverId = null;

function openPassengerModal(driverId) {
  _passModalDriverId = driverId;
  document.getElementById('pass-name').value  = '';
  document.getElementById('pass-phone').value = '';
  document.getElementById('pass-modal').classList.add('open');
  setTimeout(() => document.getElementById('pass-name').focus(), 50);
}

function closePassengerModal() {
  document.getElementById('pass-modal').classList.remove('open');
}

async function submitPassenger() {
  const name = document.getElementById('pass-name').value.trim();
  if (!name) { alert('Please enter your name.'); return; }
  const phone = document.getElementById('pass-phone').value.trim();
  const path = _passModalDriverId === '__pool__' ? '/join-pool' : `/join/${_passModalDriverId}`;
  await apiAs(name, path, { passenger_phone: phone });
  closePassengerModal();
}

function editNotes(dId, cell) {
  const cur = (state.cars || {})[dId]?.description || '';
  cell.onclick = null;
  cell.innerHTML = `<div class="cell-inner">
    <input id="inline-notes-${esc(dId)}" value="${esc(cur)}" placeholder="Add a note…"
      style="border:none;border-bottom:1px solid #bbb;outline:none;font-size:12px;flex:1;font-family:Arial,sans-serif;width:90%"
      onkeydown="if(event.key==='Enter')saveNotes('${esc(dId)}');if(event.key==='Escape')render();">
    <button onclick="saveNotes('${esc(dId)}')" style="background:#7B68AE;color:white;border:none;border-radius:3px;padding:1px 6px;cursor:pointer;font-size:12px;margin-left:4px">&#10003;</button>
    <button onclick="render()" style="background:none;border:1px solid #ccc;border-radius:3px;padding:1px 6px;cursor:pointer;font-size:12px">&#10005;</button>
  </div>`;
  const inp = document.getElementById(`inline-notes-${dId}`);
  inp.focus(); inp.select();
}

async function saveNotes(dId) {
  const inp = document.getElementById(`inline-notes-${dId}`);
  if (!inp) return;
  const car = (state.cars || {})[dId] || {};
  await apiAs(dId, '/add-car', {
    capacity:    car.capacity,
    phone:       car.phone || '',
    departure:   car.departure,
    return_time: car.return_time || '',
    direction:   car.direction || 'both',
    description: inp.value.trim(),
  });
}

async function removePassenger(driverId, passengerName) {
  if (!confirm(`Remove ${passengerName} from this car?`)) return;
  await apiAs('admin', `/remove-passenger/${encodeURIComponent(driverId)}`, { passenger_id: passengerName });
}

// ── Render ────────────────────────────────────────────────────────────────────

function render() {
  const meta  = state.metadata || {};
  const cars  = state.cars     || {};
  const isRnd = meta.mode === 'random';

  const titleEl = document.getElementById('title-display');
  titleEl.textContent = meta.title || 'Ridesheet';
  titleEl.onclick = makeTitleEditable;

  const sd = meta.start_date;
  let dateStr = sd ? fmtDate(sd) : 'TBD';
  if (meta.start_time) dateStr += '  ·  ' + fmtDep(meta.start_time);
  if (meta.end_time)   dateStr += ' – ' + fmtDep(meta.end_time);
  const dateCell = document.getElementById('date-cell');
  dateCell.innerHTML = `&#128197; ${esc(dateStr)}`;
  dateCell.onclick = makeDateEditable;

  const loc = meta.location || 'TBD';
  const locCell = document.getElementById('loc-cell');
  const locContent = isUrl(loc)
    ? `<a href="${esc(loc)}" target="_blank" onclick="event.stopPropagation()">${esc(loc)}</a>`
    : esc(loc);
  locCell.innerHTML = `&#128205; ${locContent}`;
  locCell.onclick = makeLocEditable;

  document.getElementById('random-banner').style.display = isRnd ? '' : 'none';
  renderCars(cars, isRnd);
  renderPool(isRnd);
}

function renderCars(cars, isRnd) {
  const thead = document.getElementById('cars-thead');
  const tbody = document.getElementById('cars-tbody');
  const entries = Object.entries(cars);

  const maxPass = entries.length
    ? Math.max(4, ...entries.map(([_, c]) => c.capacity || 0))
    : 4;

  const hasReturn = entries.some(([_, c]) => c.return_time);
  let hdr = '<tr><th>Driver</th><th>Departure Time</th>';
  for (let i = 1; i <= maxPass; i++) hdr += `<th>Passenger ${i}</th>`;
  if (hasReturn) hdr += '<th>Return Time</th>';
  hdr += '<th>Notes</th></tr>';
  thead.innerHTML = hdr;

  const EMPTY_ROWS = 6;
  const totalRows = Math.max(entries.length + EMPTY_ROWS, 12);
  let rows = '';

  entries.forEach(([dId, car]) => {
    const passengers = car.passengers || [];
    const capacity   = car.capacity || 4;
    const dirNote    = car.direction === 'there'  ? '(there only)' :
                       car.direction === 'return' ? '(return only)' : '';
    const driverPhone = car.phone ? `<span style="color:#999;font-size:11px;font-weight:normal"> ${esc(car.phone)}</span>` : '';
    const driverCell = `<td onclick="openCarModal('${esc(dId)}')" style="cursor:pointer">
      <div class="cell-inner cell-driver">
        ${esc(dId)}${driverPhone}
        ${dirNote ? `<span class="dir-note">${dirNote}</span>` : ''}
        <button class="join-btn" title="Edit car">edit</button>
      </div></td>`;

    const depCell = `<td><div class="cell-inner">${esc(fmtDep(car.departure))}</div></td>`;

    let passCells = '';
    for (let i = 0; i < maxPass; i++) {
      const p = passengers[i];
      if (p) {
        const pPhone = (car.passenger_phones || {})[p];
        const pPhoneHtml = pPhone ? `<span style="color:#999;font-size:11px"> ${esc(pPhone)}</span>` : '';
        passCells += `<td class="cell-occupied" onclick="removePassenger('${esc(dId)}','${esc(p)}')">
          <div class="cell-inner">${esc(p)}${pPhoneHtml}<span class="join-btn">remove</span></div></td>`;
      } else if (i >= capacity) {
        passCells += `<td class="cell-crossed"><div class="cell-inner"></div></td>`;
      } else if (!isRnd) {
        passCells += `<td onclick="openPassengerModal('${esc(dId)}')" style="cursor:pointer">
          <div class="cell-inner"><button class="join-btn">+ Join</button></div></td>`;
      } else {
        passCells += `<td><div class="cell-inner"></div></td>`;
      }
    }

    const returnCell = hasReturn ? `<td><div class="cell-inner">${esc(fmtDep(car.return_time || ''))}</div></td>` : '';
    const notesCell = `<td class="cell-notes" onclick="editNotes('${esc(dId)}', this)"><div class="cell-inner" style="color:#555;font-size:12px">${esc(car.description || '')}<span class="join-btn" style="margin-left:4px">edit</span></div></td>`;
    rows += `<tr>${driverCell}${depCell}${passCells}${returnCell}${notesCell}</tr>`;
  });

  const emptyPassCols = '<td><div class="cell-inner"></div></td>'.repeat(maxPass + (hasReturn ? 1 : 0) + 1);
  const driverEmptyCell = `<td onclick="openCarModal()" style="cursor:pointer">
    <div class="cell-inner"><button class="join-btn">+ Add Car</button></div></td>`;
  for (let i = entries.length; i < totalRows; i++) {
    rows += `<tr>${driverEmptyCell}<td><div class="cell-inner"></div></td>${emptyPassCols}</tr>`;
  }

  tbody.innerHTML = rows;
}

function renderPool(isRnd) {
  const el = document.getElementById('pool-section');
  if (!isRnd) { el.innerHTML = ''; return; }
  el.innerHTML = `<div class="form-card"><div class="pool-section">
    <button class="btn-pool" onclick="openPassengerModal('__pool__')">&#127922; Join Pool</button>
  </div></div>`;
}

async function saveMeta(data) {
  await apiAs('admin', '/edit-meta', data);
}

function makeTitleEditable() {
  const cur = (state.metadata || {}).title || '';
  const el = document.getElementById('title-display');
  el.onclick = null;
  el.innerHTML = `<input id="inline-title" value="${esc(cur)}"
    style="background:transparent;border:none;border-bottom:2px solid rgba(255,255,255,0.6);color:white;font-size:22px;font-weight:bold;text-align:center;width:70%;outline:none;font-family:Arial,sans-serif"
    onkeydown="if(event.key==='Enter')saveInlineTitle();if(event.key==='Escape')render();">
  <span style="margin-left:10px">
    <button onclick="saveInlineTitle()" style="background:rgba(255,255,255,0.25);color:white;border:none;border-radius:3px;padding:2px 8px;cursor:pointer;font-size:14px">&#10003;</button>
    <button onclick="render()" style="background:rgba(255,255,255,0.1);color:white;border:none;border-radius:3px;padding:2px 8px;cursor:pointer;font-size:14px">&#10005;</button>
  </span>`;
  const inp = document.getElementById('inline-title');
  inp.focus(); inp.select();
}

async function saveInlineTitle() {
  await saveMeta({ title: document.getElementById('inline-title').value.trim() || (state.metadata||{}).title });
}

function makeDateEditable() {
  const meta = state.metadata || {};
  const cell = document.getElementById('date-cell');
  cell.onclick = null;
  cell.innerHTML = `&#128197;
    <input type="date" id="inline-start" value="${meta.start_date||''}" style="border:1px solid #bbb;border-radius:3px;padding:2px 6px;font-size:13px">
    <input type="time" id="inline-start-time" value="${meta.start_time||''}" style="border:1px solid #bbb;border-radius:3px;padding:2px 6px;font-size:13px;margin-left:4px">
    <span style="margin:0 2px">–</span>
    <input type="time" id="inline-end-time" value="${meta.end_time||''}" style="border:1px solid #bbb;border-radius:3px;padding:2px 6px;font-size:13px">
    <button onclick="saveDates()" style="background:#7B68AE;color:white;border:none;border-radius:3px;padding:2px 8px;cursor:pointer;font-size:12px;margin-left:6px">&#10003;</button>
    <button onclick="render()" style="background:none;border:1px solid #ccc;border-radius:3px;padding:2px 8px;cursor:pointer;font-size:12px">&#10005;</button>`;
  document.getElementById('inline-start').focus();
}

async function saveDates() {
  await saveMeta({
    start_date:  document.getElementById('inline-start').value,
    end_date:    document.getElementById('inline-start').value,
    start_time:  document.getElementById('inline-start-time').value || null,
    end_time:    document.getElementById('inline-end-time').value   || null,
  });
}

function makeLocEditable() {
  const cur = (state.metadata || {}).location || '';
  const cell = document.getElementById('loc-cell');
  cell.onclick = null;
  cell.innerHTML = `&#128205;
    <input id="inline-loc" value="${esc(cur)}" placeholder="Location or URL"
      style="border:none;border-bottom:1px solid #bbb;outline:none;font-size:13px;flex:1;font-family:Arial,sans-serif;min-width:160px"
      onkeydown="if(event.key==='Enter')saveInlineLoc();if(event.key==='Escape')render();">
    <button onclick="saveInlineLoc()" style="background:#7B68AE;color:white;border:none;border-radius:3px;padding:2px 8px;cursor:pointer;font-size:12px;margin-left:6px">&#10003;</button>
    <button onclick="render()" style="background:none;border:1px solid #ccc;border-radius:3px;padding:2px 8px;cursor:pointer;font-size:12px">&#10005;</button>`;
  const inp = document.getElementById('inline-loc');
  inp.focus(); inp.select();
}

async function saveInlineLoc() {
  await saveMeta({ location: document.getElementById('inline-loc').value.trim() });
}

render();
</script>
</body>
</html>"""


@flask_app.get("/<channel_id>/<message_ts>")
def ridesheet_page(channel_id, message_ts):
    from features.ridesheet import get_state
    state = get_state(channel_id, message_ts)
    if not state:
        return "Ridesheet not found.", 404
    return render_template_string(_SPA_HTML, state=state,
                                  channel_id=channel_id, message_ts=message_ts)


# ---------------------------------------------------------------------------
# Ridesheet JSON API
# ---------------------------------------------------------------------------

def _action(channel_id, message_ts, fn):
    """Run fn(user_id, state, data), persist, refresh Slack, return JSON state."""
    from features.ridesheet import get_state, save_state, refresh_ridesheet_message
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


@flask_app.post("/<channel_id>/<message_ts>/join/<driver_id>")
def ridesheet_join(channel_id, message_ts, driver_id):
    def fn(uid, state, data):
        car = state.get("cars", {}).get(driver_id)
        if not car:          return "car not found"
        if uid == driver_id: return "you are the driver"
        phones = car.setdefault("passenger_phones", {})
        if uid in car["passengers"]:
            car["passengers"].remove(uid)
            phones.pop(uid, None)
        else:
            if len(car["passengers"]) >= car["capacity"]: return "car is full"
            for other_car in state.get("cars", {}).values():
                if uid in other_car.get("passengers", []):
                    other_car["passengers"].remove(uid)
                    other_car.get("passenger_phones", {}).pop(uid, None)
            car["passengers"].append(uid)
            if data.get("passenger_phone"):
                phones[uid] = data["passenger_phone"].strip()
    return _action(channel_id, message_ts, fn)


@flask_app.post("/<channel_id>/<message_ts>/remove-passenger/<driver_id>")
def ridesheet_remove_passenger(channel_id, message_ts, driver_id):
    def fn(uid, state, data):
        passenger_id = data.get("passenger_id", "").strip()
        if not passenger_id: return "passenger_id required"
        car = state.get("cars", {}).get(driver_id)
        if not car: return "car not found"
        if passenger_id not in car.get("passengers", []):
            return "passenger not found"
        car["passengers"].remove(passenger_id)
        car.get("passenger_phones", {}).pop(passenger_id, None)
    return _action(channel_id, message_ts, fn)


@flask_app.post("/<channel_id>/<message_ts>/add-car")
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
            "capacity":       int(data.get("capacity", 4)),
            "phone":          data.get("phone", "").strip(),
            "departure":      dep_str,
            "return_time":    data.get("return_time", "").strip(),
            "passengers":     existing,
            "passenger_phones": state.get("cars", {}).get(uid, {}).get("passenger_phones", {}),
            "description":    data.get("description", "").strip(),
            "direction":      data.get("direction", "both"),
        }
    return _action(channel_id, message_ts, fn)


@flask_app.post("/<channel_id>/<message_ts>/remove-car")
def ridesheet_remove_car(channel_id, message_ts):
    def fn(uid, state, _):
        if uid not in state.get("cars", {}): return "no car to remove"
        del state["cars"][uid]
    return _action(channel_id, message_ts, fn)


@flask_app.post("/<channel_id>/<message_ts>/join-pool")
def ridesheet_join_pool(channel_id, message_ts):
    def fn(uid, state, _):
        if state.get("metadata", {}).get("mode") != "random": return "not random mode"
        cars = state.get("cars", {})
        assigned = next((d for d, c in cars.items() if uid in c.get("passengers", [])), None)
        if assigned:
            cars[assigned]["passengers"].remove(uid)
            return
        if uid in cars: return "you are a driver"
        avail = [d for d, c in cars.items() if len(c.get("passengers", [])) < c.get("capacity", 4)]
        if not avail: return "all cars are full"
        cars[random.choice(avail)]["passengers"].append(uid)
    return _action(channel_id, message_ts, fn)


@flask_app.post("/<channel_id>/<message_ts>/edit-meta")
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


# ---------------------------------------------------------------------------

def run_web_server(port: int | None = None):
    port = port or int(os.environ.get("PORT", "3000"))
    print(f"[Web] Flask server listening on port {port}")
    flask_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

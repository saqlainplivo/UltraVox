"""
complex_agent.py

A full-featured Ultravox agent with:
  - Function / tool calling (check availability, book appointment, send confirmation)
  - SQLite database for call logs + appointment records
  - Per-call metrics (duration, turn count, tools used, transcript)
  - Flask webhook server for Plivo calls (inbound + outbound)
  - Web dashboard with live call status and transcript via SSE
  - REST endpoints to query logged data

Install deps:
  pip install flask requests python-dotenv plivo

Usage:
  python complex_agent.py
  Open  http://localhost:5000              -> Dashboard
  GET   http://localhost:5000/logs         -> All call logs
  GET   http://localhost:5000/appointments -> All booked appointments
  GET   http://localhost:5000/metrics/summary -> Aggregate metrics
"""

import os
import json
import queue
import sqlite3
import threading
import time
import requests
from datetime import datetime
from flask import Flask, request, Response, jsonify, render_template
from dotenv import load_dotenv
import plivo

load_dotenv()

ULTRAVOX_API_KEY = os.getenv("ULTRAVOX_API_KEY")
ULTRAVOX_API_URL = "https://api.ultravox.ai/api/calls"
DB_PATH = "agent_data.db"

PLIVO_AUTH_ID = os.getenv("PLIVO_AUTH_ID")
PLIVO_AUTH_TOKEN = os.getenv("PLIVO_AUTH_TOKEN")
PLIVO_PHONE_NUMBER = os.getenv("PLIVO_PHONE_NUMBER")

plivo_client = plivo.RestClient(PLIVO_AUTH_ID, PLIVO_AUTH_TOKEN)

# Runtime-configurable ngrok base URL
ngrok_base_url = os.getenv("NGROK_BASE_URL", "")

app = Flask(__name__)
active_sessions = {}  # call_uuid -> {call_id, caller, started_at, direction, call_type}


# =============================================================================
# SSE INFRASTRUCTURE
# =============================================================================
def format_sse(data, event=None):
    msg = ""
    if event:
        msg += f"event: {event}\n"
    msg += f"data: {json.dumps(data)}\n\n"
    return msg


class MessageAnnouncer:
    def __init__(self):
        self.listeners = []

    def listen(self):
        q = queue.Queue(maxsize=50)
        self.listeners.append(q)
        return q

    def announce(self, data, event=None):
        msg = format_sse(data, event)
        dead = []
        for i, q in enumerate(self.listeners):
            try:
                q.put_nowait(msg)
            except queue.Full:
                dead.append(i)
        for i in reversed(dead):
            del self.listeners[i]


announcer = MessageAnnouncer()


# =============================================================================
# DATABASE SETUP
# =============================================================================
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS call_logs (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                call_id      TEXT,
                caller       TEXT,
                started_at   TEXT,
                ended_at     TEXT,
                duration_sec INTEGER,
                turn_count   INTEGER,
                tools_called TEXT,
                transcript   TEXT,
                status       TEXT,
                direction    TEXT DEFAULT 'inbound',
                call_type    TEXT DEFAULT 'agent'
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS appointments (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                call_id        TEXT,
                patient_name   TEXT,
                phone          TEXT,
                date_time      TEXT,
                reason         TEXT,
                confirmed      INTEGER DEFAULT 0,
                created_at     TEXT
            )
        """)
        # Add columns if upgrading from older schema
        try:
            conn.execute("ALTER TABLE call_logs ADD COLUMN direction TEXT DEFAULT 'inbound'")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE call_logs ADD COLUMN call_type TEXT DEFAULT 'agent'")
        except sqlite3.OperationalError:
            pass
    print("[DB] Initialized -> agent_data.db")


def log_call(call_id, caller, started_at, ended_at, duration, turns, tools, transcript, status,
             direction="inbound", call_type="agent"):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO call_logs
            (call_id, caller, started_at, ended_at, duration_sec, turn_count, tools_called, transcript, status,
             direction, call_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (call_id, caller, started_at, ended_at, duration, turns,
              json.dumps(tools), transcript, status, direction, call_type))
    print(f"[DB] Call logged -> {call_id}  ({duration}s, {turns} turns)")


def save_appointment(call_id, name, phone, dt, reason):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO appointments (call_id, patient_name, phone, date_time, reason, confirmed, created_at)
            VALUES (?, ?, ?, ?, ?, 1, ?)
        """, (call_id, name, phone, dt, reason, datetime.utcnow().isoformat()))
    print(f"[DB] Appointment saved -> {name} on {dt}")


# =============================================================================
# TOOL DEFINITIONS (sent to Ultravox so the agent can call them)
# =============================================================================

AVAILABLE_SLOTS = {
    "2026-03-05 10:00": True,
    "2026-03-05 14:00": True,
    "2026-03-06 09:00": False,
    "2026-03-06 15:00": True,
}

def get_tool_definitions(base_url):
    """Build tool definitions using the given base URL (must be https for Ultravox)."""
    return [
        {
            "temporaryTool": {
                "modelToolName": "check_availability",
                "description":   "Check whether a given date/time slot is available for booking.",
                "dynamicParameters": [
                    {
                        "name":     "date_time",
                        "location": "PARAMETER_LOCATION_BODY",
                        "schema":   {"type": "string", "description": "Date and time in format YYYY-MM-DD HH:MM"},
                        "required": True,
                    }
                ],
                "http": {
                    "baseUrlPattern": f"{base_url}/tools/check-availability",
                    "httpMethod":     "POST",
                },
            }
        },
        {
            "temporaryTool": {
                "modelToolName": "book_appointment",
                "description":   "Book an appointment for a patient.",
                "dynamicParameters": [
                    {"name": "patient_name", "location": "PARAMETER_LOCATION_BODY",
                     "schema": {"type": "string"}, "required": True},
                    {"name": "phone", "location": "PARAMETER_LOCATION_BODY",
                     "schema": {"type": "string"}, "required": True},
                    {"name": "date_time", "location": "PARAMETER_LOCATION_BODY",
                     "schema": {"type": "string"}, "required": True},
                    {"name": "reason", "location": "PARAMETER_LOCATION_BODY",
                     "schema": {"type": "string"}, "required": True},
                ],
                "http": {
                    "baseUrlPattern": f"{base_url}/tools/book-appointment",
                    "httpMethod":     "POST",
                },
            }
        },
        {
            "temporaryTool": {
                "modelToolName": "send_confirmation",
                "description":   "Send an SMS confirmation to the patient after booking.",
                "dynamicParameters": [
                    {"name": "phone",   "location": "PARAMETER_LOCATION_BODY",
                     "schema": {"type": "string"}, "required": True},
                    {"name": "message", "location": "PARAMETER_LOCATION_BODY",
                     "schema": {"type": "string"}, "required": True},
                ],
                "http": {
                    "baseUrlPattern": f"{base_url}/tools/send-confirmation",
                    "httpMethod":     "POST",
                },
            }
        },
    ]

SYSTEM_PROMPT = """
You are Maya, an intelligent receptionist at Sunrise Dental Clinic.

Your workflow for each call:
1. Greet the caller and collect their name and phone number.
2. Ask what they need (appointment, info, emergency).
3. For appointments:
   a. Ask their preferred date and time.
   b. Call check_availability to confirm the slot.
   c. If available, call book_appointment to reserve it.
   d. Call send_confirmation to text them a summary.
4. For emergencies: advise them to visit immediately or call 911.
5. For general questions: answer from your knowledge (hours: Mon-Sat 9am-6pm, address: 123 Main St).

Always be warm, brief, and professional. Confirm all details before ending the call.
"""

RECEPTIONIST_PROMPT = """
You are Maya, a warm and professional receptionist at Sunrise Dental Clinic.
Your job is to:
  1. Greet the caller and ask how you can help.
  2. Handle appointment bookings (ask for name, preferred date/time, and reason for visit).
  3. Answer general questions about clinic hours (Mon-Sat, 9am-6pm) and location (123 Main St).
  4. If a caller has a dental emergency, tell them to come in immediately or call 911.
  5. Be concise -- this is a phone call, not a chat.

Always end by confirming the caller's name and what you've arranged for them.
"""


# =============================================================================
# TOOL ENDPOINTS (Ultravox calls these during the conversation)
# =============================================================================
@app.route("/tools/check-availability", methods=["POST"])
def tool_check_availability():
    data = request.json or {}
    date_time = data.get("date_time", "")
    available = AVAILABLE_SLOTS.get(date_time, True)
    return jsonify({
        "available": available,
        "date_time": date_time,
        "message":   f"Slot {'is available' if available else 'is already booked'}.",
    })


@app.route("/tools/book-appointment", methods=["POST"])
def tool_book_appointment():
    data    = request.json or {}
    name    = data.get("patient_name", "Unknown")
    phone   = data.get("phone", "")
    dt      = data.get("date_time", "")
    reason  = data.get("reason", "General checkup")
    call_id = request.headers.get("X-Ultravox-Call-Id", "unknown")

    AVAILABLE_SLOTS[dt] = False
    save_appointment(call_id, name, phone, dt, reason)

    return jsonify({
        "success":           True,
        "confirmation_code": f"APT-{abs(hash(name + dt)) % 9999:04d}",
        "message":           f"Appointment booked for {name} on {dt}.",
    })


@app.route("/tools/send-confirmation", methods=["POST"])
def tool_send_confirmation():
    data    = request.json or {}
    phone   = data.get("phone", "")
    message = data.get("message", "")

    print(f"[SMS] to {phone}: {message}")
    return jsonify({"sent": True, "phone": phone})


# =============================================================================
# ULTRAVOX CALL CREATION
# =============================================================================
def create_ultravox_call() -> dict:
    # Use ngrok URL for tool endpoints (Ultravox requires https)
    tools_base = ngrok_base_url if ngrok_base_url else "http://localhost:5000"
    payload = {
        "systemPrompt": SYSTEM_PROMPT,
        "voice":        "Sarah",
        "temperature":  0.5,
        "firstSpeaker": "FIRST_SPEAKER_AGENT",
        "medium": {
            "serverWebSocket": {
                "inputSampleRate":  8000,
                "outputSampleRate": 8000,
            }
        },
        "selectedTools": get_tool_definitions(tools_base),
    }
    headers = {
        "X-API-Key":    ULTRAVOX_API_KEY,
        "Content-Type": "application/json",
    }
    res = requests.post(ULTRAVOX_API_URL, json=payload, headers=headers)
    if not res.ok:
        print(f"[!] Ultravox API error {res.status_code}: {res.text}")
        res.raise_for_status()
    return res.json()


def create_ultravox_call_receptionist() -> dict:
    """Create an Ultravox call with the receptionist prompt and no tools."""
    payload = {
        "systemPrompt": RECEPTIONIST_PROMPT,
        "voice":        "Sarah",
        "temperature":  0.5,
        "firstSpeaker": "FIRST_SPEAKER_AGENT",
        "medium": {
            "serverWebSocket": {
                "inputSampleRate":  8000,
                "outputSampleRate": 8000,
            }
        },
    }
    headers = {
        "X-API-Key":    ULTRAVOX_API_KEY,
        "Content-Type": "application/json",
    }
    res = requests.post(ULTRAVOX_API_URL, json=payload, headers=headers)
    if not res.ok:
        print(f"[!] Ultravox API error {res.status_code}: {res.text}")
        res.raise_for_status()
    return res.json()


# =============================================================================
# LIVE TRANSCRIPT POLLING
# =============================================================================
def poll_transcript(call_id, call_uuid):
    """Background thread: polls Ultravox messages API and broadcasts new lines via SSE."""
    headers = {"X-API-Key": ULTRAVOX_API_KEY}
    msgs_url = f"https://api.ultravox.ai/api/calls/{call_id}/messages"
    seen_count = 0

    while call_uuid in active_sessions:
        try:
            res = requests.get(msgs_url, headers=headers)
            if res.ok:
                messages = res.json().get("results", [])
                new_messages = messages[seen_count:]
                for m in new_messages:
                    role = m.get("role", "")
                    text = m.get("text", "")
                    if role in ("agent", "user") and text:
                        announcer.announce({
                            "call_uuid": call_uuid,
                            "role": role,
                            "text": text,
                        }, event="transcript")
                seen_count = len(messages)
        except Exception as e:
            print(f"[!] Transcript poll error: {e}")
        time.sleep(2)

    print(f"[i] Transcript polling stopped for {call_uuid}")


# =============================================================================
# METRICS COLLECTOR (polls Ultravox call events post-call)
# =============================================================================
def collect_and_log_metrics(call_id, caller, started_at, direction="inbound", call_type="agent"):
    """Fetch call messages from Ultravox API and log metrics to DB."""
    try:
        headers  = {"X-API-Key": ULTRAVOX_API_KEY}
        msgs_url = f"https://api.ultravox.ai/api/calls/{call_id}/messages"
        res      = requests.get(msgs_url, headers=headers)
        messages = res.json().get("results", []) if res.ok else []

        transcript = []
        tools_used = []
        turn_count = 0

        for m in messages:
            role = m.get("role", "")
            text = m.get("text", "")
            if role in ("agent", "user") and text:
                transcript.append(f"{role}: {text}")
                if role == "user":
                    turn_count += 1
            if m.get("toolName"):
                tools_used.append(m["toolName"])

        ended_at   = datetime.utcnow().isoformat()
        started_dt = datetime.fromisoformat(started_at)
        duration   = int((datetime.utcnow() - started_dt).total_seconds())

        log_call(
            call_id    = call_id,
            caller     = caller,
            started_at = started_at,
            ended_at   = ended_at,
            duration   = duration,
            turns      = turn_count,
            tools      = list(set(tools_used)),
            transcript = "\n".join(transcript),
            status     = "completed",
            direction  = direction,
            call_type  = call_type,
        )
    except Exception as e:
        print(f"[!] Metrics collection failed: {e}")


# =============================================================================
# DASHBOARD & SSE ROUTES
# =============================================================================
@app.route("/")
def homepage():
    return render_template("home.html")


@app.route("/complex-agent")
def dashboard():
    return render_template("index.html")


@app.route("/receptionist")
def receptionist_page():
    return render_template("receptionist.html")


@app.route("/api/events")
def sse_stream():
    def stream():
        q = announcer.listen()
        # Send a heartbeat so the client knows the connection is alive
        yield format_sse({"type": "connected"}, event="status")
        while True:
            try:
                msg = q.get(timeout=30)
                yield msg
            except queue.Empty:
                # Send keepalive
                yield ": keepalive\n\n"

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/active-calls")
def api_active_calls():
    calls = []
    for uuid, session in active_sessions.items():
        calls.append({
            "call_uuid": uuid,
            "call_id": session.get("call_id", ""),
            "caller": session.get("caller", ""),
            "started_at": session.get("started_at", ""),
            "direction": session.get("direction", "inbound"),
            "call_type": session.get("call_type", "agent"),
        })
    return jsonify(calls)


@app.route("/api/ngrok-url", methods=["GET", "POST"])
def api_ngrok_url():
    global ngrok_base_url
    if request.method == "POST":
        data = request.json or {}
        url = data.get("url", "").rstrip("/")
        if url:
            ngrok_base_url = url
            print(f"[i] Ngrok URL updated -> {ngrok_base_url}")
            return jsonify({"success": True, "url": ngrok_base_url})
        return jsonify({"success": False, "error": "No URL provided"}), 400
    return jsonify({"url": ngrok_base_url})


# =============================================================================
# OUTBOUND CALL API
# =============================================================================
@app.route("/api/make-call", methods=["POST"])
def api_make_call():
    global ngrok_base_url
    data = request.json or {}
    phone_number = data.get("phone_number", "").strip()
    call_type = data.get("call_type", "agent")  # "agent" or "normal"
    provided_url = data.get("ngrok_url", "").rstrip("/")

    if provided_url:
        ngrok_base_url = provided_url

    if not ngrok_base_url:
        return jsonify({"success": False, "error": "Ngrok URL not configured"}), 400

    if not phone_number:
        return jsonify({"success": False, "error": "Phone number is required"}), 400

    if call_type == "agent":
        answer_url = f"{ngrok_base_url}/outbound-agent-answered"
    elif call_type == "receptionist":
        answer_url = f"{ngrok_base_url}/outbound-receptionist-answered"
    else:
        answer_url = f"{ngrok_base_url}/outbound-normal-answered"

    ring_url = f"{ngrok_base_url}/outbound-ringing"
    hangup_url = f"{ngrok_base_url}/call-ended"

    try:
        response = plivo_client.calls.create(
            from_=PLIVO_PHONE_NUMBER,
            to_=phone_number,
            answer_url=answer_url,
            answer_method="POST",
            hangup_url=hangup_url,
            hangup_method="POST",
            ring_url=ring_url,
            ring_method="POST",
        )

        call_uuid = response.request_uuid
        print(f"[phone] Outbound {call_type} call to {phone_number}  (Request UUID: {call_uuid})")

        announcer.announce({
            "call_uuid": call_uuid,
            "phone_number": phone_number,
            "call_type": call_type,
            "status": "initiated",
        }, event="call_status")

        return jsonify({
            "success": True,
            "request_uuid": call_uuid,
            "phone_number": phone_number,
            "call_type": call_type,
        })

    except Exception as e:
        print(f"[!] Outbound call failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/end-call", methods=["POST"])
def api_end_call():
    data = request.json or {}
    call_uuid = data.get("call_uuid", "")

    if not call_uuid:
        return jsonify({"success": False, "error": "call_uuid is required"}), 400

    try:
        plivo_client.calls.delete(call_uuid)
        print(f"[phone] Ended call {call_uuid}")

        announcer.announce({
            "call_uuid": call_uuid,
            "status": "ended",
        }, event="call_status")

        return jsonify({"success": True})
    except Exception as e:
        print(f"[!] End call failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# =============================================================================
# OUTBOUND PLIVO WEBHOOKS
# =============================================================================
@app.route("/outbound-ringing", methods=["GET", "POST"])
def outbound_ringing():
    call_uuid = request.values.get("CallUUID", "unknown")
    to_number = request.values.get("To", "unknown")
    print(f"[phone] Ringing -> {to_number}  (UUID: {call_uuid})")

    announcer.announce({
        "call_uuid": call_uuid,
        "phone_number": to_number,
        "status": "ringing",
    }, event="call_status")

    return Response("OK", status=200)


@app.route("/outbound-agent-answered", methods=["GET", "POST"])
def outbound_agent_answered():
    call_uuid = request.values.get("CallUUID", "unknown")
    to_number = request.values.get("To", "unknown")
    started = datetime.utcnow().isoformat()

    print(f"\n[phone] Agent call answered by {to_number}  (UUID: {call_uuid})")

    try:
        session = create_ultravox_call()
    except Exception as e:
        print(f"[!] Failed to create Ultravox session: {e}")
        # Return valid XML so Plivo doesn't drop the call silently
        err_xml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Speak>Sorry, we are experiencing technical difficulties. Please try again later.</Speak>
</Response>"""
        return Response(err_xml, mimetype="text/xml")

    join_url = session["joinUrl"]
    call_id = session["callId"]

    active_sessions[call_uuid] = {
        "call_id": call_id,
        "caller": to_number,
        "started_at": started,
        "direction": "outbound",
        "call_type": "agent",
    }
    print(f"[+] Ultravox session -> {call_id}")

    announcer.announce({
        "call_uuid": call_uuid,
        "call_id": call_id,
        "phone_number": to_number,
        "status": "connected",
        "call_type": "agent",
    }, event="call_status")

    # Start transcript polling in background
    t = threading.Thread(target=poll_transcript, args=(call_id, call_uuid), daemon=True)
    t.start()

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Stream
        keepCallAlive="true"
        bidirectional="true"
        contentType="audio/x-mulaw;rate=8000"
        streamTimeout="86400"
        audioTrack="inbound"
    >{join_url}</Stream>
</Response>"""
    return Response(xml, mimetype="text/xml")


@app.route("/outbound-receptionist-answered", methods=["GET", "POST"])
def outbound_receptionist_answered():
    call_uuid = request.values.get("CallUUID", "unknown")
    to_number = request.values.get("To", "unknown")
    started = datetime.utcnow().isoformat()

    print(f"\n[phone] Receptionist call answered by {to_number}  (UUID: {call_uuid})")

    try:
        session = create_ultravox_call_receptionist()
    except Exception as e:
        print(f"[!] Failed to create Ultravox session: {e}")
        err_xml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Speak>Sorry, we are experiencing technical difficulties. Please try again later.</Speak>
</Response>"""
        return Response(err_xml, mimetype="text/xml")

    join_url = session["joinUrl"]
    call_id = session["callId"]

    active_sessions[call_uuid] = {
        "call_id": call_id,
        "caller": to_number,
        "started_at": started,
        "direction": "outbound",
        "call_type": "receptionist",
    }
    print(f"[+] Ultravox session -> {call_id}")

    announcer.announce({
        "call_uuid": call_uuid,
        "call_id": call_id,
        "phone_number": to_number,
        "status": "connected",
        "call_type": "receptionist",
    }, event="call_status")

    # Start transcript polling in background
    t = threading.Thread(target=poll_transcript, args=(call_id, call_uuid), daemon=True)
    t.start()

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Stream
        keepCallAlive="true"
        bidirectional="true"
        contentType="audio/x-mulaw;rate=8000"
        streamTimeout="86400"
        audioTrack="inbound"
    >{join_url}</Stream>
</Response>"""
    return Response(xml, mimetype="text/xml")


@app.route("/outbound-normal-answered", methods=["GET", "POST"])
def outbound_normal_answered():
    call_uuid = request.values.get("CallUUID", "unknown")
    to_number = request.values.get("To", "unknown")
    started = datetime.utcnow().isoformat()

    print(f"\n[phone] Normal call answered by {to_number}  (UUID: {call_uuid})")

    active_sessions[call_uuid] = {
        "call_id": None,
        "caller": to_number,
        "started_at": started,
        "direction": "outbound",
        "call_type": "normal",
    }

    announcer.announce({
        "call_uuid": call_uuid,
        "phone_number": to_number,
        "status": "connected",
        "call_type": "normal",
    }, event="call_status")

    xml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Speak voice="Polly.Amy">
        Hello! This is a call from Sunrise Dental Clinic.
        We are calling to confirm your upcoming appointment.
        If you have any questions, please call us back at your convenience.
        Thank you and have a great day!
    </Speak>
</Response>"""
    return Response(xml, mimetype="text/xml")


# =============================================================================
# PLIVO WEBHOOKS (INBOUND)
# =============================================================================
@app.route("/incoming-call", methods=["GET", "POST"])
def incoming_call():
    caller    = request.values.get("From", "Unknown")
    call_uuid = request.values.get("CallUUID", "unknown")
    started   = datetime.utcnow().isoformat()

    print(f"\n[phone] Incoming call from {caller}  (UUID: {call_uuid})")
    session  = create_ultravox_call()
    join_url = session["joinUrl"]
    call_id  = session["callId"]

    active_sessions[call_uuid] = {
        "call_id":    call_id,
        "caller":     caller,
        "started_at": started,
        "direction":  "inbound",
        "call_type":  "agent",
    }
    print(f"[+] Ultravox session -> {call_id}")

    announcer.announce({
        "call_uuid": call_uuid,
        "call_id": call_id,
        "phone_number": caller,
        "status": "connected",
        "call_type": "agent",
        "direction": "inbound",
    }, event="call_status")

    # Start transcript polling
    t = threading.Thread(target=poll_transcript, args=(call_id, call_uuid), daemon=True)
    t.start()

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Stream
        keepCallAlive="true"
        bidirectional="true"
        contentType="audio/x-mulaw;rate=8000"
        streamTimeout="86400"
        audioTrack="inbound"
    >{join_url}</Stream>
</Response>"""
    return Response(xml, mimetype="text/xml")


@app.route("/call-ended", methods=["GET", "POST"])
def call_ended():
    call_uuid = request.values.get("CallUUID", "unknown")
    session   = active_sessions.pop(call_uuid, {})

    print(f"[phone] Call ended -> {call_uuid}")

    direction = session.get("direction", "inbound")
    call_type = session.get("call_type", "agent")

    announcer.announce({
        "call_uuid": call_uuid,
        "status": "ended",
    }, event="call_status")

    if session and session.get("call_id"):
        t = threading.Thread(
            target=collect_and_log_metrics,
            args=(session["call_id"], session["caller"], session["started_at"],
                  direction, call_type),
            daemon=True,
        )
        t.start()
    elif session:
        # Normal call (no Ultravox) — log basic info
        ended_at = datetime.utcnow().isoformat()
        started_dt = datetime.fromisoformat(session["started_at"])
        duration = int((datetime.utcnow() - started_dt).total_seconds())
        log_call(
            call_id=None, caller=session["caller"], started_at=session["started_at"],
            ended_at=ended_at, duration=duration, turns=0, tools=[],
            transcript="", status="completed", direction=direction, call_type=call_type,
        )

    return Response("OK", status=200)


# =============================================================================
# DATA QUERY ENDPOINTS
# =============================================================================
@app.route("/logs")
def get_logs():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM call_logs ORDER BY id DESC").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/appointments")
def get_appointments():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM appointments ORDER BY id DESC").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/metrics/summary")
def metrics_summary():
    with sqlite3.connect(DB_PATH) as conn:
        stats = conn.execute("""
            SELECT
                COUNT(*)            AS total_calls,
                AVG(duration_sec)   AS avg_duration_sec,
                SUM(turn_count)     AS total_turns,
                AVG(turn_count)     AS avg_turns_per_call
            FROM call_logs
        """).fetchone()
        apt_count = conn.execute("SELECT COUNT(*) FROM appointments").fetchone()[0]
    return jsonify({
        "total_calls":        stats[0],
        "avg_duration_sec":   round(stats[1] or 0, 1),
        "total_turns":        stats[2],
        "avg_turns_per_call": round(stats[3] or 0, 1),
        "total_appointments": apt_count,
    })


@app.route("/health")
def health():
    return jsonify({"status": "ok", "db": DB_PATH})


# =============================================================================
# MAIN
# =============================================================================
if __name__ == "__main__":
    init_db()
    print("\n[*] Complex Agent Server  ->  http://0.0.0.0:5000")
    print("[i] Homepage              ->  http://localhost:5000")
    print("[i] Complex Agent         ->  http://localhost:5000/complex-agent")
    print("[i] Receptionist          ->  http://localhost:5000/receptionist")
    print("[i] Plivo Answer URL      ->  http://<ngrok>/incoming-call")
    print("[i] Plivo Hangup URL      ->  http://<ngrok>/call-ended")
    print("[i] View logs             ->  GET /logs")
    print("[i] View appointments     ->  GET /appointments")
    print("[i] View metrics          ->  GET /metrics/summary\n")
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)

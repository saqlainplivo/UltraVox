"""
plivo_receptionist.py

A phone receptionist powered by Plivo (telephony) + Ultravox (AI voice).

Flow:
  Incoming call -> Plivo webhook -> Flask server ->
  Create Ultravox session -> Return Plivo XML with Stream URL ->
  Plivo bridges call audio <-> Ultravox WebSocket

Install deps:
  pip install flask requests plivo python-dotenv

Expose locally with:
  ngrok http 5000
  Then set your Plivo number's Answer URL to: https://<ngrok-url>/incoming-call

Usage:
  python plivo_receptionist.py
"""

import os
from typing import Optional, Dict, Any
import requests
import plivo
from flask import Flask, request, Response, render_template, jsonify
from dotenv import load_dotenv

load_dotenv()

ULTRAVOX_API_KEY = os.getenv("ULTRAVOX_API_KEY")
ULTRAVOX_API_BASE = "https://api.ultravox.ai/api"
ULTRAVOX_CALLS_URL = f"{ULTRAVOX_API_BASE}/calls"
ULTRAVOX_AGENTS_URL = f"{ULTRAVOX_API_BASE}/agents"
PLIVO_AUTH_ID = os.getenv("PLIVO_AUTH_ID")
PLIVO_AUTH_TOKEN = os.getenv("PLIVO_AUTH_TOKEN")
PLIVO_FROM_NUMBER = os.getenv("PLIVO_FROM_NUMBER") or os.getenv("PLIVO_PHONE_NUMBER")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL") or os.getenv("NGROK_BASE_URL") or os.getenv("WEBHOOK_BASE_URL")
DESTINATION_PHONE_NUMBER = os.getenv("DESTINATION_PHONE_NUMBER")
AGENT_NAME = os.getenv("AGENT_NAME", "sample-plivo-phone-calls-ts")
AGENT_VOICE = os.getenv("AGENT_VOICE", "Jessica")
AGENT_TEMPERATURE = float(os.getenv("AGENT_TEMPERATURE", "0.4"))

app = Flask(__name__)

# -- Receptionist persona ------------------------------------------------------
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


# -- 1. Create (or fetch) agent and start an Ultravox call ---------------------
def get_or_create_agent() -> Optional[Dict[str, Any]]:
    headers = {"X-API-Key": ULTRAVOX_API_KEY}
    try:
        res = requests.get(f"{ULTRAVOX_AGENTS_URL}?name={AGENT_NAME}", headers=headers, timeout=20)
        if res.ok:
            data = res.json()
            if isinstance(data, list) and data:
                return data[0]
            if isinstance(data, dict):
                results = data.get("results") or data.get("agents") or []
                if results:
                    return results[0]
    except Exception as exc:
        print(f"[!] Agent lookup failed: {exc}")

    payload = {
        "name": AGENT_NAME,
        "systemPrompt": "You're a friendly and fun gal. You like to chat casually.",
        "voice": AGENT_VOICE,
        "temperature": AGENT_TEMPERATURE,
    }
    try:
        res = requests.post(ULTRAVOX_AGENTS_URL, json=payload, headers=headers, timeout=20)
        if res.ok:
            return res.json()
        print(f"[!] Agent create failed {res.status_code}: {res.text}")
    except Exception as exc:
        print(f"[!] Agent create exception: {exc}")
    return None


def create_ultravox_call(first_speaker: str) -> dict:
    headers = {
        "X-API-Key": ULTRAVOX_API_KEY,
        "Content-Type": "application/json",
    }

    agent = get_or_create_agent()
    if agent:
        agent_id = agent.get("id") or agent.get("agentId")
        if agent_id:
            payload = {
                "agentId": agent_id,
                "firstSpeaker": first_speaker,
                "medium": {"plivo": {}},
            }
            res = requests.post(ULTRAVOX_CALLS_URL, json=payload, headers=headers, timeout=20)
            if res.ok:
                return res.json()
            print(f"[!] Ultravox agent call error {res.status_code}: {res.text}")

    payload = {
        "systemPrompt": RECEPTIONIST_PROMPT,
        "voice":        "Sarah",
        "temperature":  0.5,
        "firstSpeaker": first_speaker,
        "medium": {"plivo": {}},
    }
    res = requests.post(ULTRAVOX_CALLS_URL, json=payload, headers=headers, timeout=20)
    res.raise_for_status()
    return res.json()


# -- 1.5 Simple dialer UI -----------------------------------------------------
@app.route("/", methods=["GET"])
def dialer():
    return render_template("dial.html")


# -- 2. Webhook: Plivo hits this when a call comes in -------------------------
@app.route("/incoming-call", methods=["GET", "POST"])
def incoming_call():
    caller = request.values.get("From", "Unknown")
    print(f"[phone] Incoming call from {caller}")

    # Create a fresh Ultravox session for this call
    session = create_ultravox_call(first_speaker="FIRST_SPEAKER_AGENT")
    join_url = session.get("joinUrl")
    call_id = session.get("callId")
    if not join_url:
        print(f"[!] Missing joinUrl in Ultravox response: {session}")
        return Response("Ultravox error: missing joinUrl", status=502)
    print(f"[+] Ultravox session created -> {call_id}")
    print(f"[+] Ultravox joinUrl -> {join_url}")

    # Return Plivo XML: stream call audio to Ultravox
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Stream
        keepCallAlive="true"
        bidirectional="true"
        contentType="audio/x-l16;rate=16000"
        streamTimeout="86400"
    >{join_url}</Stream>
</Response>"""

    print("[+] Streaming call audio to Ultravox WebSocket")
    return Response(xml, mimetype="text/xml")


# -- 2.1 Inbound (Plivo Answer URL target in example) -------------------------
@app.route("/inbound", methods=["GET", "POST"])
def inbound():
    return incoming_call()


# -- 2.2 Connect (Plivo Answer URL target for outbound in example) -----------
@app.route("/connect", methods=["GET", "POST"])
def connect():
    return incoming_call()


# -- 2.5 Outbound call: trigger a Plivo outbound dial -------------------------
@app.route("/outbound-call", methods=["POST"])
def outbound_call():
    try:
        if request.is_json:
            data = request.get_json(silent=True) or {}
            to_number = data.get("to_number")
        else:
            to_number = request.form.get("to_number")
        if not to_number:
            to_number = DESTINATION_PHONE_NUMBER
        if not to_number:
            return jsonify({"ok": False, "error": "Missing to_number (or DESTINATION_PHONE_NUMBER)"}), 400
        if not (PLIVO_AUTH_ID and PLIVO_AUTH_TOKEN and PLIVO_FROM_NUMBER):
            return jsonify({"ok": False, "error": "Missing PLIVO_AUTH_ID/PLIVO_AUTH_TOKEN/PLIVO_FROM_NUMBER in .env"}), 500
        if not PUBLIC_BASE_URL:
            return jsonify({"ok": False, "error": "Missing PUBLIC_BASE_URL/WEBHOOK_BASE_URL in .env"}), 500

        base_url = PUBLIC_BASE_URL.rstrip("/")
        answer_url = f"{base_url}/connect"
        hangup_url = f"{base_url}/call-ended"

        client = plivo.RestClient(auth_id=PLIVO_AUTH_ID, auth_token=PLIVO_AUTH_TOKEN)
        call = client.calls.create(
            from_=PLIVO_FROM_NUMBER,
            to_=to_number,
            answer_url=answer_url,
            answer_method="POST",
            hangup_url=hangup_url,
            hangup_method="POST",
        )
        request_uuid = None
        if isinstance(call, dict):
            request_uuid = call.get("request_uuid")
        else:
            try:
                request_uuid = call["request_uuid"]
            except Exception:
                request_uuid = getattr(call, "request_uuid", None)
        return jsonify({"ok": True, "call_uuid": request_uuid})
    except plivo.exceptions.PlivoRestError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Server error: {exc}"}), 500


# -- 2.6 Outbound alias to match example --------------------------------------
@app.route("/outbound", methods=["POST"])
def outbound_alias():
    return outbound_call()


# -- 3. Webhook: Plivo hits this when a call ends -----------------------------
@app.route("/call-ended", methods=["GET", "POST"])
def call_ended():
    call_uuid = request.values.get("CallUUID", "unknown")
    duration  = request.values.get("Duration", "0")
    status    = request.values.get("CallStatus", "unknown")
    caller    = request.values.get("From", "unknown")

    print(f"\n[log] Call Summary")
    print(f"      UUID    : {call_uuid}")
    print(f"      From    : {caller}")
    print(f"      Duration: {duration}s")
    print(f"      Status  : {status}")
    return Response("OK", status=200)


# -- 4. Health check -----------------------------------------------------------
@app.route("/health")
def health():
    return {"status": "ok"}


# -- 5. Start server -----------------------------------------------------------
if __name__ == "__main__":
    print("\n[*] Plivo Receptionist Server  ->  http://0.0.0.0:5000")
    if PUBLIC_BASE_URL:
        print(f"[i] Public Base URL          -> {PUBLIC_BASE_URL.rstrip('/')}")
    print("[i] Set your Plivo Answer URL  -> http://<your-ngrok-url>/inbound")
    print("[i] Set your Plivo Hangup URL  -> http://<your-ngrok-url>/call-ended\n")
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)

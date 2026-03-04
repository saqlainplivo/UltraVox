# Ultravox Voice Agent Scripts

Three progressively complex voice agent scripts built on the [Ultravox](https://ultravox.ai) API.

## Scripts

### 1. `simple_agent.py` — Local Voice Agent

Runs entirely on your laptop. Captures microphone input, streams it to Ultravox, and plays the agent's audio response through your speaker. Prints a live transcript in the terminal.

- No server required
- Uses `sounddevice` for mic/speaker I/O
- WebSocket streaming to Ultravox

```bash
python simple_agent.py
```

### 2. `plivo_receptionist.py` — Phone Receptionist

Flask server that receives Plivo webhooks when a phone call comes in. Creates a fresh Ultravox session per call and bridges the audio via Plivo's `<Stream>` element. Simulates a dental clinic receptionist named Maya.

- Plivo handles telephony, Ultravox handles the AI conversation
- Expose with ngrok for a public webhook URL
- Swap the `RECEPTIONIST_PROMPT` to change the persona

```bash
ngrok http 5000          # in one terminal
python plivo_receptionist.py  # in another
```

Then set your Plivo number's:
- **Answer URL** to `https://<ngrok-url>/incoming-call`
- **Hangup URL** to `https://<ngrok-url>/call-ended`

### 3. `complex_agent.py` — Full-Stack Agent

Everything in Script 2, plus:

- **Tool/function calling** — the agent can check slot availability, book appointments, and send SMS confirmations mid-conversation
- **SQLite logging** — every call's transcript, duration, turn count, and tools used are recorded
- **REST endpoints** for querying data after calls

```bash
python complex_agent.py
```

| Endpoint | Description |
|---|---|
| `GET /logs` | All call logs |
| `GET /appointments` | All booked appointments |
| `GET /metrics/summary` | Aggregate stats (avg duration, turns, etc.) |
| `GET /health` | Server health check |

Tool endpoints (called by Ultravox during a conversation):

| Endpoint | What it does |
|---|---|
| `POST /tools/check-availability` | Checks if a date/time slot is open |
| `POST /tools/book-appointment` | Books a slot and saves to SQLite |
| `POST /tools/send-confirmation` | Logs an SMS (integrate Twilio/Plivo for real sends) |

## Setup

```bash
cd ultravox-scripts
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and add your Ultravox API key:

```
ULTRAVOX_API_KEY=your_key_here
```

## Requirements

- Python 3.9+
- An [Ultravox](https://ultravox.ai) API key
- For Scripts 2 & 3: a [Plivo](https://www.plivo.com) account with a phone number and [ngrok](https://ngrok.com) (or another tunnel)
- For Script 1: a working microphone and speaker

## File Structure

```
ultravox-scripts/
├── simple_agent.py          # Local mic/speaker voice agent
├── plivo_receptionist.py    # Phone receptionist via Plivo
├── complex_agent.py         # Full agent with tools, DB, and metrics
├── requirements.txt
├── .env.example
└── README.md
```

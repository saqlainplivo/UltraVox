"""
simple_agent.py

A local voice agent using Ultravox.
Captures mic input -> streams to Ultravox -> plays response audio.

Install deps:
  pip install requests sounddevice numpy websockets python-dotenv

Usage:
  python simple_agent.py
"""

import os
import json
import asyncio
import numpy as np
import sounddevice as sd
import websockets
import requests
from dotenv import load_dotenv

load_dotenv()

ULTRAVOX_API_KEY = os.getenv("ULTRAVOX_API_KEY")
ULTRAVOX_API_URL = "https://api.ultravox.ai/api/calls"

SAMPLE_RATE  = 16000   # Hz (Ultravox expects 16 kHz PCM)
CHANNELS     = 1
CHUNK_FRAMES = 1600    # 100 ms of audio per chunk

SYSTEM_PROMPT = """
You are a helpful AI assistant. Keep your responses concise and conversational.
"""


# -- 1. Create an Ultravox call session ----------------------------------------
def create_call() -> str:
    payload = {
        "systemPrompt": SYSTEM_PROMPT,
        "voice":        "Mark2",
        "temperature":  0.7,
        "firstSpeaker": "FIRST_SPEAKER_USER",
        "medium": {
            "serverWebSocket": {
                "inputSampleRate":  SAMPLE_RATE,
                "outputSampleRate": SAMPLE_RATE,
            }
        },
    }
    headers = {
        "X-API-Key":    ULTRAVOX_API_KEY,
        "Content-Type": "application/json",
    }
    res = requests.post(ULTRAVOX_API_URL, json=payload, headers=headers)
    res.raise_for_status()
    data = res.json()
    print(f"[+] Call created  ->  {data['callId']}")
    return data["joinUrl"]


# -- 2. Stream mic audio -> WebSocket -----------------------------------------
async def send_audio(ws, stop_event):
    loop = asyncio.get_event_loop()
    q = asyncio.Queue()

    def mic_callback(indata, frames, time_info, status):
        loop.call_soon_threadsafe(q.put_nowait, indata.copy())

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=CHANNELS,
                        dtype="int16", blocksize=CHUNK_FRAMES,
                        callback=mic_callback):
        print("[mic] Listening... speak now. Ctrl+C to quit.\n")
        while not stop_event.is_set():
            chunk = await q.get()
            await ws.send(chunk.tobytes())


# -- 3. Receive agent audio -> speaker ----------------------------------------
async def receive_audio(ws, stop_event):
    stream = sd.OutputStream(samplerate=SAMPLE_RATE, channels=CHANNELS, dtype="int16")
    stream.start()

    async for message in ws:
        if isinstance(message, bytes):
            # Raw PCM audio from Ultravox
            audio = np.frombuffer(message, dtype="int16")
            stream.write(audio)
        else:
            # JSON control messages (transcript, status, etc.)
            try:
                evt = json.loads(message)
                kind = evt.get("type", "")
                if kind == "transcript":
                    role = evt.get("role", "")
                    text = evt.get("text", "")
                    tag = "Agent" if role == "agent" else "You"
                    print(f"{tag}: {text}")
                elif kind == "state" and evt.get("state") == "ended":
                    print("[+] Conversation ended.")
                    stop_event.set()
            except json.JSONDecodeError:
                pass

    stream.stop()
    stream.close()


# -- 4. Main ------------------------------------------------------------------
async def main():
    join_url = create_call()
    stop_event = asyncio.Event()

    async with websockets.connect(join_url) as ws:
        await asyncio.gather(
            send_audio(ws, stop_event),
            receive_audio(ws, stop_event),
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[+] Session ended by user.")

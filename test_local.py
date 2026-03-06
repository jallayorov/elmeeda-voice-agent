"""
Local voice test for Elmeeda Voice Agent.
Connects directly to PersonaPlex WebSocket, bypassing Twilio.
Uses your Mac's microphone and speakers.

Usage: python3 test_local.py
"""
import asyncio
import json
import sys
import numpy as np

try:
    import sounddevice as sd
except ImportError:
    print("Installing sounddevice...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "sounddevice", "-q"])
    import sounddevice as sd

try:
    import websockets
except ImportError:
    print("Installing websockets...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "websockets", "-q"])
    import websockets

VOICE_AGENT_URL = "ws://34.172.163.253:8080/ws/twilio"
SAMPLE_RATE_IN = 8000   # Twilio sends 8kHz
SAMPLE_RATE_OUT = 8000  # Twilio expects 8kHz
CHUNK_MS = 20
CHUNK_SAMPLES = SAMPLE_RATE_IN * CHUNK_MS // 1000  # 160 samples

import audioop
import base64
import uuid

stream_sid = str(uuid.uuid4())

async def test_voice():
    print("🎤 Connecting to Elmeeda Voice Agent...")
    print(f"   URL: {VOICE_AGENT_URL}")
    
    async with websockets.connect(VOICE_AGENT_URL) as ws:
        print("✅ Connected!")
        
        # Send Twilio-style start event
        start_event = {
            "event": "start",
            "start": {
                "streamSid": stream_sid,
                "callSid": "test-local-" + stream_sid[:8],
                "customParameters": {
                    "unit_number": "8056"
                }
            }
        }
        await ws.send(json.dumps(start_event))
        print("📞 Sent start event (unit 8056)")
        print("🎙️  Speak into your microphone! Press Ctrl+C to stop.\n")

        # Audio output buffer
        output_queue = asyncio.Queue()

        def audio_callback_out(outdata, frames, time_info, status):
            """Play received audio."""
            try:
                data = output_queue.get_nowait()
                if len(data) < len(outdata):
                    outdata[:len(data)] = data
                    outdata[len(data):] = b'\x00' * (len(outdata) - len(data))
                else:
                    outdata[:] = data[:len(outdata)]
            except:
                outdata[:] = b'\x00' * len(outdata)

        # Start output stream
        output_stream = sd.RawOutputStream(
            samplerate=SAMPLE_RATE_OUT,
            channels=1,
            dtype='int16',
            blocksize=CHUNK_SAMPLES,
            callback=audio_callback_out,
        )
        output_stream.start()

        async def send_audio():
            """Capture mic audio and send as Twilio media events."""
            def audio_callback_in(indata, frames, time_info, status):
                if status:
                    pass
                # Convert to mulaw for Twilio format
                pcm_bytes = indata.tobytes()
                mulaw_bytes = audioop.lin2ulaw(pcm_bytes, 2)
                b64 = base64.b64encode(mulaw_bytes).decode()
                
                media_event = {
                    "event": "media",
                    "media": {
                        "payload": b64,
                        "track": "inbound",
                        "chunk": "1",
                        "timestamp": "0"
                    },
                    "streamSid": stream_sid
                }
                try:
                    asyncio.get_event_loop().call_soon_threadsafe(
                        send_queue.put_nowait, json.dumps(media_event)
                    )
                except:
                    pass

            send_queue = asyncio.Queue()
            
            input_stream = sd.RawInputStream(
                samplerate=SAMPLE_RATE_IN,
                channels=1,
                dtype='int16',
                blocksize=CHUNK_SAMPLES,
                callback=audio_callback_in,
            )
            input_stream.start()
            
            try:
                while True:
                    msg = await send_queue.get()
                    await ws.send(msg)
            finally:
                input_stream.stop()

        async def receive_audio():
            """Receive Twilio-style media events and play them."""
            async for message in ws:
                try:
                    data = json.loads(message)
                    if data.get("event") == "media":
                        payload = data["media"]["payload"]
                        mulaw_bytes = base64.b64decode(payload)
                        pcm_bytes = audioop.ulaw2lin(mulaw_bytes, 2)
                        output_queue.put_nowait(pcm_bytes)
                    elif data.get("event") == "mark":
                        pass  # timing marks
                    else:
                        print(f"   📨 Event: {data.get('event', 'unknown')}")
                except json.JSONDecodeError:
                    # Binary message from PersonaPlex directly
                    if len(message) > 1:
                        kind = message[0]
                        if kind == 0x02:  # text token
                            text = message[1:].decode('utf-8', errors='replace')
                            print(f"   🤖 {text}", end='', flush=True)

        try:
            await asyncio.gather(send_audio(), receive_audio())
        except KeyboardInterrupt:
            print("\n\n👋 Disconnecting...")
            stop_event = {"event": "stop", "streamSid": stream_sid}
            await ws.send(json.dumps(stop_event))

        output_stream.stop()

if __name__ == "__main__":
    print("=" * 50)
    print("  Elmeeda Voice Agent — Local Test")
    print("=" * 50)
    try:
        asyncio.run(test_voice())
    except KeyboardInterrupt:
        print("\n👋 Done!")

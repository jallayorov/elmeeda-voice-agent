# Elmeeda Voice Agent

Voice-based fleet warranty assistant powered by NVIDIA PersonaPlex (Moshi) and integrated with Twilio Media Streams. Callers speak to an AI agent that can look up warranty coverage, check claim status, evaluate repair coverage, and schedule callbacks with warranty specialists.

## Architecture

```
 Caller (PSTN)
      |
  [Twilio]
      | Twilio Media Streams (WebSocket, mulaw 8kHz base64)
      v
 +--------------------------+
 |  FastAPI (app.py :8080)  |
 |  /ws/twilio endpoint     |
 +--------------------------+
      |                  |
      v                  v
 +----------------+  +-------------------+
 | TwilioBridge   |  | ElmeedaClient     |
 | Audio pipeline |  | REST API client   |
 +----------------+  +-------------------+
      |
      v
 +------------------+
 | PersonaPlex      |
 | (moshi.server)   |
 | localhost:8998   |
 +------------------+
```

## PersonaPlex WebSocket Protocol

The PersonaPlex server exposes a WebSocket at `/api/chat`. All binary messages use **kind-byte framing** — the first byte indicates the message type:

| Kind Byte | Meaning | Payload |
|-----------|---------|---------|
| `0x00` | Handshake | Empty or server info |
| `0x01` | Audio | Opus-encoded audio |
| `0x02` | Text | UTF-8 text token |

**Query parameters** on the WebSocket URL:
- `text_prompt` — system prompt text for the AI persona
- `voice_prompt` — voice preset filename (e.g. `NATF2.pt` from the `voices/` directory)

**Sending audio to PersonaPlex:** `b'\x01' + opus_payload`
**Receiving audio from PersonaPlex:** first byte is `0x01`, remainder is Opus audio
**Receiving text tokens:** first byte is `0x02`, remainder is UTF-8 text

The Opus codec uses `sphn.OpusStreamWriter` (append_pcm -> read_bytes) and `sphn.OpusStreamReader` (append_bytes -> read_pcm) at 24 kHz sample rate.

## Audio Pipeline

**Inbound (caller -> AI):**
1. Twilio sends mulaw 8 kHz base64 via WebSocket
2. Decode base64 -> mulaw to PCM16 (audioop.ulaw2lin)
3. Resample 8 kHz -> 24 kHz (audioop.ratecv stateful)
4. int16 -> float32, buffer into 480-sample (20 ms) Opus frames
5. Encode via OpusStreamWriter, prepend kind byte `0x01`, send to PersonaPlex

**Outbound (AI -> caller):**
1. PersonaPlex sends `0x01` + Opus bytes
2. Decode via OpusStreamReader -> float32 PCM 24 kHz
3. float32 -> int16, resample 24 kHz -> 8 kHz (audioop.ratecv stateful)
4. int16 -> mulaw (audioop.lin2ulaw)
5. Buffer into 160-byte (20 ms) frames, base64 encode, send as Twilio media event

## Twilio Custom Parameters for Elmeeda Lookups

When configuring the Twilio `<Stream>`, pass custom parameters to trigger automatic Elmeeda API lookups before the call starts:

```xml
<Response>
  <Connect>
    <Stream url="wss://YOUR_CLOUD_RUN_URL/ws/twilio">
      <Parameter name="unit_number" value="12345" />
      <Parameter name="claim_id" value="CLM-2024-001" />
      <Parameter name="repair_code" value="DPF-REGEN" />
      <Parameter name="symptoms" value="regen not completing" />
      <Parameter name="callback_phone" value="+15551234567" />
      <Parameter name="callback_time" value="2pm CT" />
    </Stream>
  </Connect>
</Response>
```

Available parameters:
- `unit_number` — triggers warranty status lookup
- `claim_id` — triggers claim status lookup
- `repair_code` + `unit_number` — triggers coverage evaluation
- `symptoms` — included with coverage evaluation
- `callback_phone` / `callback_time` — added to prompt context for callback scheduling

Results are appended to the system prompt as structured context before connecting to PersonaPlex.

## Files

| File | Purpose |
|---|---|
| `app.py` | FastAPI server, PersonaPlex subprocess lifecycle, health endpoints, WebSocket routing |
| `twilio_bridge.py` | Bidirectional audio bridge with kind-byte protocol framing |
| `elmeeda_client.py` | Async HTTP client for Elmeeda API with token caching and auto-refresh |
| `persona_config.py` | System prompt builder, context formatters, voice prompt defaults |
| `Dockerfile` | Cloud Run GPU container (CUDA 12.4, NVIDIA L4, PersonaPlex from source) |
| `deploy.sh` | One-command deploy to Google Cloud Run |
| `requirements.txt` | Python dependencies |

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `PORT` | `8080` | HTTP server port |
| `HOST` | `0.0.0.0` | HTTP server bind address |
| `LOG_LEVEL` | `INFO` | Python log level |
| `PERSONA_HOST` | `127.0.0.1` | PersonaPlex internal host |
| `PERSONA_PORT` | `8998` | PersonaPlex internal port |
| `MOSHI_CMD` | auto-generated | Override PersonaPlex startup command |
| `VOICE_PROMPT` | `NATF2.pt` | PersonaPlex voice preset filename |
| `ELMEEDA_API_URL` | `https://api.elmeeda.com` | Elmeeda API base URL |
| `ELMEEDA_USERNAME` | *(required)* | Elmeeda API username |
| `ELMEEDA_PASSWORD` | *(required)* | Elmeeda API password |

## Cloud Run Deploy

```bash
gcloud auth login
gcloud config set project hazel-hall-487120-v3
bash deploy.sh
```

Uses `gcloud run deploy --source .` with NVIDIA L4 GPU, 4 vCPUs, 16 GiB RAM.

## Known Caveats

- **Single concurrent call** — one PersonaPlex instance per container (Cloud Run scales horizontally)
- **Handshake timing** — if PersonaPlex doesn't respond to the handshake within 10s, the bridge proceeds anyway (some server versions skip the handshake response)
- **No DTMF handling** — DTMF digits are logged but not acted upon
- **Resampling quality** — audioop.ratecv is adequate for voice but not audiophile-grade; the stateful converter maintains continuity across chunks
- **Elmeeda API failures** — lookup errors are logged and skipped; the call proceeds with whatever context was successfully retrieved
- **PersonaPlex installed from source** — the Dockerfile clones the NVIDIA PersonaPlex repo; do not `pip install moshi` from PyPI as it is a different package
- **Text tokens** — PersonaPlex text tokens (kind byte 0x02) are logged but not forwarded to Twilio (voice-only output)

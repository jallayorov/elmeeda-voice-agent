"""Twilio Media Streams <-> PersonaPlex bidirectional audio bridge.

PersonaPlex WebSocket protocol (kind-byte framing):
    0x00 = handshake
    0x01 = audio (Opus payload)
    0x02 = text token (UTF-8)

Audio pipeline (inbound — caller voice to AI):
    Twilio mulaw 8 kHz base64
    -> decode base64 -> audioop ulaw2lin -> int16 PCM 8 kHz
    -> audioop.ratecv 8 kHz -> 24 kHz (stateful)
    -> float32 -> sphn.OpusStreamWriter.append_pcm + read_bytes
    -> prepend kind byte 0x01 -> PersonaPlex WS

Audio pipeline (outbound — AI voice to caller):
    PersonaPlex binary (kind byte 0x01 + Opus payload)
    -> sphn.OpusStreamReader.append_bytes + read_pcm -> float32 24 kHz
    -> int16 PCM -> audioop.ratecv 24 kHz -> 8 kHz (stateful)
    -> audioop lin2ulaw -> mulaw -> base64 -> Twilio media event (160 byte frames)
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any, Optional

import numpy as np
import sphn
import websockets
from fastapi import WebSocket

try:
    import audioop
except ImportError:
    import audioop_lts as audioop  # type: ignore[no-redef]

from elmeeda_client import ElmeedaClient
from persona_config import (
    DEFAULT_VOICE_PROMPT,
    TWILIO_PARAM_KEYS,
    build_system_prompt,
    format_claim_context,
    format_coverage_context,
    format_warranty_context,
)

logger = logging.getLogger(__name__)

# ---- Constants ----
TWILIO_SAMPLE_RATE = 8000
PERSONA_SAMPLE_RATE = 24000
OPUS_FRAME_SAMPLES = 480  # 20 ms at 24 kHz
TWILIO_FRAME_BYTES = 160  # 20 ms at 8 kHz mulaw (1 byte/sample)

# PersonaPlex kind bytes
KIND_HANDSHAKE = 0x00
KIND_AUDIO = 0x01
KIND_TEXT = 0x02


class TwilioBridge:
    """Bridges a single Twilio Media Stream call to a PersonaPlex session."""

    def __init__(
        self,
        twilio_ws: WebSocket,
        persona_ws_url: str,
        elmeeda_client: Optional[ElmeedaClient] = None,
        voice_prompt: str = DEFAULT_VOICE_PROMPT,
    ):
        self.twilio_ws = twilio_ws
        self.persona_ws_url = persona_ws_url
        self.elmeeda_client = elmeeda_client
        self.voice_prompt = voice_prompt

        self.stream_sid: str | None = None
        self.call_params: dict[str, str] = {}
        self.text_prompt: str = ""

        # Opus codec objects
        self._opus_writer = sphn.OpusStreamWriter(PERSONA_SAMPLE_RATE)
        self._opus_reader = sphn.OpusStreamReader(PERSONA_SAMPLE_RATE)

        # Inbound PCM buffer for accumulating 20 ms Opus frames
        self._pcm_buffer = np.array([], dtype=np.float32)

        # audioop.ratecv state machines (None initially)
        self._upsample_state: Any = None  # 8k -> 24k
        self._downsample_state: Any = None  # 24k -> 8k

        # Outbound mulaw remainder buffer for 160-byte framing
        self._mulaw_remainder = b""

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(self):
        """Wait for Twilio start event, do Elmeeda lookups, then bridge audio."""
        # Phase 1: wait for Twilio 'start' event to get stream_sid and custom params
        if not await self._wait_for_start():
            logger.error("Never received Twilio start event — aborting")
            return

        # Phase 2: perform Elmeeda lookups from custom params
        await self._do_elmeeda_lookups()

        # Phase 3: connect to PersonaPlex and bridge audio
        ws_url = self._build_persona_url()
        logger.info("Opening PersonaPlex connection: %s", ws_url)
        try:
            async with websockets.connect(
                ws_url, open_timeout=10.0, max_size=2**20
            ) as persona_ws:
                logger.info("PersonaPlex connected, awaiting handshake")
                await self._handle_handshake(persona_ws)

                inbound = asyncio.create_task(
                    self._twilio_to_persona(persona_ws), name="twilio->persona"
                )
                outbound = asyncio.create_task(
                    self._persona_to_twilio(persona_ws), name="persona->twilio"
                )
                done, pending = await asyncio.wait(
                    [inbound, outbound], return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                for task in done:
                    if task.exception():
                        logger.error(
                            "Bridge task %s failed: %s",
                            task.get_name(),
                            task.exception(),
                        )
        except websockets.exceptions.WebSocketException:
            logger.exception("Failed to connect to PersonaPlex")
        except Exception:
            logger.exception("Unexpected error in bridge run loop")

    # ------------------------------------------------------------------
    # Phase 1: Wait for Twilio start event
    # ------------------------------------------------------------------

    async def _wait_for_start(self) -> bool:
        """Read Twilio messages until we get a 'start' event. Returns False on timeout/close."""
        try:
            async for raw_msg in self.twilio_ws.iter_text():
                try:
                    msg = json.loads(raw_msg)
                except json.JSONDecodeError:
                    logger.warning("Ignoring non-JSON Twilio message")
                    continue

                event = msg.get("event")
                if event == "start":
                    start_data = msg.get("start", {})
                    self.stream_sid = start_data.get("streamSid")
                    if not self.stream_sid:
                        logger.error("Twilio start event missing streamSid")
                        return False

                    # Extract custom parameters
                    custom = start_data.get("customParameters", {})
                    for key in TWILIO_PARAM_KEYS:
                        val = custom.get(key, "")
                        if val:
                            self.call_params[key] = val

                    logger.info(
                        "Twilio stream started sid=%s params=%s",
                        self.stream_sid,
                        list(self.call_params.keys()),
                    )
                    return True

                elif event == "connected":
                    logger.info("Twilio connected event received")

                elif event == "stop":
                    logger.info("Twilio stop received before start — aborting")
                    return False

                else:
                    logger.debug("Pre-start Twilio event: %s", event)
        except Exception:
            logger.exception("Error waiting for Twilio start event")
        return False

    # ------------------------------------------------------------------
    # Phase 2: Elmeeda lookups
    # ------------------------------------------------------------------

    async def _do_elmeeda_lookups(self):
        """Call Elmeeda API based on custom params and build text_prompt."""
        context_lines: list[str] = []

        if not self.elmeeda_client:
            self.text_prompt = build_system_prompt()
            return

        unit = self.call_params.get("unit_number")
        claim_id = self.call_params.get("claim_id")
        repair_code = self.call_params.get("repair_code")
        symptoms = self.call_params.get("symptoms", "")

        tasks: list[tuple[str, Any]] = []

        if unit:
            tasks.append(("warranty", self.elmeeda_client.get_warranty_status(unit)))
        if claim_id:
            tasks.append(("claim", self.elmeeda_client.get_claim_status(claim_id)))
        if unit and repair_code:
            tasks.append(
                (
                    "coverage",
                    self.elmeeda_client.evaluate_repair_coverage(
                        unit, repair_code, symptoms
                    ),
                )
            )

        for label, coro in tasks:
            try:
                result = await coro
                if label == "warranty":
                    context_lines.append(format_warranty_context(result))
                elif label == "claim":
                    context_lines.append(format_claim_context(result))
                elif label == "coverage":
                    context_lines.append(format_coverage_context(result))
            except Exception:
                logger.exception("Elmeeda lookup '%s' failed", label)

        # Add caller context if available
        cb_phone = self.call_params.get("callback_phone")
        cb_time = self.call_params.get("callback_time")
        if cb_phone:
            context_lines.append(f"Caller callback phone: {cb_phone}")
        if cb_time:
            context_lines.append(f"Preferred callback time: {cb_time}")

        self.text_prompt = build_system_prompt(context_lines if context_lines else None)
        logger.info(
            "Built text_prompt with %d context lines (%d chars)",
            len(context_lines),
            len(self.text_prompt),
        )

    # ------------------------------------------------------------------
    # URL and handshake
    # ------------------------------------------------------------------

    def _build_persona_url(self) -> str:
        """Build PersonaPlex WS URL with query params."""
        from urllib.parse import quote, urlencode

        params = {
            "text_prompt": self.text_prompt,
            "voice_prompt": self.voice_prompt,
        }
        sep = "&" if "?" in self.persona_ws_url else "?"
        return self.persona_ws_url + sep + urlencode(params, quote_via=quote)

    async def _handle_handshake(self, persona_ws: websockets.WebSocketClientProtocol):
        """Send handshake and wait for server handshake response."""
        # Send handshake kind byte
        await persona_ws.send(bytes([KIND_HANDSHAKE]))
        logger.info("Sent handshake to PersonaPlex")

        # Wait for handshake response (with timeout)
        try:
            response = await asyncio.wait_for(persona_ws.recv(), timeout=10.0)
            if isinstance(response, bytes) and len(response) >= 1:
                kind = response[0]
                if kind == KIND_HANDSHAKE:
                    logger.info("PersonaPlex handshake complete")
                else:
                    logger.warning(
                        "Expected handshake response, got kind=%d — proceeding", kind
                    )
            else:
                logger.warning("Unexpected handshake response type — proceeding")
        except asyncio.TimeoutError:
            logger.warning("Handshake response timeout — proceeding anyway")

    # ------------------------------------------------------------------
    # Resampling helpers (audioop.ratecv state machines)
    # ------------------------------------------------------------------

    def _upsample_8k_to_24k(self, pcm16_bytes: bytes) -> bytes:
        """Resample int16 PCM from 8 kHz to 24 kHz using audioop.ratecv."""
        result, self._upsample_state = audioop.ratecv(
            pcm16_bytes, 2, 1, TWILIO_SAMPLE_RATE, PERSONA_SAMPLE_RATE, self._upsample_state
        )
        return result

    def _downsample_24k_to_8k(self, pcm16_bytes: bytes) -> bytes:
        """Resample int16 PCM from 24 kHz to 8 kHz using audioop.ratecv."""
        result, self._downsample_state = audioop.ratecv(
            pcm16_bytes, 2, 1, PERSONA_SAMPLE_RATE, TWILIO_SAMPLE_RATE, self._downsample_state
        )
        return result

    # ------------------------------------------------------------------
    # Twilio -> PersonaPlex
    # ------------------------------------------------------------------

    async def _twilio_to_persona(
        self, persona_ws: websockets.WebSocketClientProtocol
    ):
        """Forward Twilio audio to PersonaPlex with correct kind-byte framing."""
        async for raw_msg in self.twilio_ws.iter_text():
            try:
                msg = json.loads(raw_msg)
            except json.JSONDecodeError:
                logger.warning("Ignoring non-JSON Twilio message")
                continue

            event = msg.get("event")

            if event == "media":
                payload = msg.get("media", {}).get("payload")
                if not payload:
                    logger.debug("Empty media payload — skipping")
                    continue

                mulaw_bytes = base64.b64decode(payload)

                # mulaw -> int16 PCM 8 kHz
                pcm16_8k = audioop.ulaw2lin(mulaw_bytes, 2)

                # Resample 8 kHz -> 24 kHz (int16)
                pcm16_24k = self._upsample_8k_to_24k(pcm16_8k)

                # int16 -> float32 [-1, 1]
                pcm_f32 = (
                    np.frombuffer(pcm16_24k, dtype=np.int16).astype(np.float32)
                    / 32768.0
                )

                # Buffer and encode to Opus in valid frame sizes
                self._pcm_buffer = np.concatenate([self._pcm_buffer, pcm_f32])
                while self._pcm_buffer.shape[0] >= OPUS_FRAME_SAMPLES:
                    frame = self._pcm_buffer[:OPUS_FRAME_SAMPLES]
                    self._pcm_buffer = self._pcm_buffer[OPUS_FRAME_SAMPLES:]

                    # Feed PCM to writer — append_pcm returns encoded Opus bytes directly
                    opus_bytes = self._opus_writer.append_pcm(frame)
                    if opus_bytes:
                        # Prepend kind byte 0x01 for audio
                        await persona_ws.send(bytes([KIND_AUDIO]) + opus_bytes)

            elif event == "stop":
                logger.info("Twilio stream stopped sid=%s", self.stream_sid)
                break

            elif event == "mark":
                logger.debug("Twilio mark event: %s", msg.get("mark", {}).get("name"))

            elif event == "dtmf":
                logger.info("DTMF received: %s", msg.get("dtmf", {}).get("digit"))

            else:
                logger.debug("Unhandled Twilio event: %s", event)

    # ------------------------------------------------------------------
    # PersonaPlex -> Twilio
    # ------------------------------------------------------------------

    async def _persona_to_twilio(
        self, persona_ws: websockets.WebSocketClientProtocol
    ):
        """Forward PersonaPlex audio to Twilio with kind-byte parsing and 20ms framing."""
        async for raw_data in persona_ws:
            if isinstance(raw_data, str):
                logger.debug("PersonaPlex text message (str): %.200s", raw_data)
                continue

            if len(raw_data) < 1:
                continue

            kind = raw_data[0]
            payload = raw_data[1:]

            if kind == KIND_HANDSHAKE:
                logger.debug("Late handshake message from PersonaPlex — ignoring")
                continue

            elif kind == KIND_TEXT:
                text = payload.decode("utf-8", errors="replace")
                logger.debug("PersonaPlex text token: %s", text)
                continue

            elif kind == KIND_AUDIO:
                if not payload:
                    continue

                # Decode Opus -> float32 PCM 24 kHz (append_bytes returns ndarray directly)
                pcm_24k = self._opus_reader.append_bytes(payload)
                if pcm_24k.shape[0] == 0:
                    continue

                # float32 -> int16 PCM
                pcm16_24k = (
                    np.clip(pcm_24k * 32768.0, -32768, 32767).astype(np.int16).tobytes()
                )

                # Resample 24 kHz -> 8 kHz (int16)
                pcm16_8k = self._downsample_24k_to_8k(pcm16_24k)

                # int16 PCM -> mulaw
                mulaw_bytes = audioop.lin2ulaw(pcm16_8k, 2)

                # Buffer and send in 160-byte (20 ms) frames
                self._mulaw_remainder += mulaw_bytes
                while len(self._mulaw_remainder) >= TWILIO_FRAME_BYTES:
                    frame = self._mulaw_remainder[:TWILIO_FRAME_BYTES]
                    self._mulaw_remainder = self._mulaw_remainder[TWILIO_FRAME_BYTES:]

                    if not self.stream_sid:
                        logger.warning("No stream_sid — cannot send audio to Twilio")
                        continue

                    b64_payload = base64.b64encode(frame).decode("ascii")
                    await self.twilio_ws.send_json(
                        {
                            "event": "media",
                            "streamSid": self.stream_sid,
                            "media": {"payload": b64_payload},
                        }
                    )

            else:
                logger.warning("Unknown PersonaPlex kind byte: 0x%02x", kind)

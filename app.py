"""Elmeeda Voice Agent — FastAPI application.

Starts the PersonaPlex (moshi) server as an internal subprocess, then serves:
  GET  /         — service info
  GET  /healthz  — liveness (PersonaPlex process alive)
  GET  /readyz   — readiness (PersonaPlex accepting connections)
  WS   /ws/twilio — Twilio Media Streams websocket endpoint
"""

import asyncio
import logging
import os
import subprocess
import sys
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from elmeeda_client import ElmeedaClient
from persona_config import DEFAULT_VOICE_PROMPT
from twilio_bridge import TwilioBridge

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8080"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

PERSONA_HOST = os.getenv("PERSONA_HOST", "127.0.0.1")
PERSONA_PORT = int(os.getenv("PERSONA_PORT", "8998"))
PERSONA_WS_URL = f"ws://{PERSONA_HOST}:{PERSONA_PORT}/api/chat"

MOSHI_CMD = os.getenv(
    "MOSHI_CMD",
    f"python -m moshi.server --host {PERSONA_HOST} --port {PERSONA_PORT}",
)

ELMEEDA_API_URL = os.getenv("ELMEEDA_API_URL", "https://api.elmeeda.com")
ELMEEDA_USERNAME = os.getenv("ELMEEDA_USERNAME", "")
ELMEEDA_PASSWORD = os.getenv("ELMEEDA_PASSWORD", "")

logger = logging.getLogger("elmeeda_voice_agent")

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

_persona_process: subprocess.Popen | None = None
_ready: bool = False
_elmeeda: ElmeedaClient | None = None


# ---------------------------------------------------------------------------
# PersonaPlex subprocess management
# ---------------------------------------------------------------------------


async def _wait_for_persona_port() -> bool:
    """Poll until the PersonaPlex TCP port is accepting connections."""
    for attempt in range(120):  # up to 2 min for model loading
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(PERSONA_HOST, PERSONA_PORT),
                timeout=2.0,
            )
            writer.close()
            await writer.wait_closed()
            logger.info("PersonaPlex ready after %d attempts", attempt + 1)
            return True
        except (ConnectionRefusedError, asyncio.TimeoutError, OSError):
            pass
        await asyncio.sleep(1.0)
    return False


async def _start_persona_server():
    global _persona_process, _ready
    logger.info("Starting PersonaPlex: %s", MOSHI_CMD)
    _persona_process = subprocess.Popen(
        MOSHI_CMD.split(),
        stdout=sys.stdout,
        stderr=sys.stderr,
    )
    if await _wait_for_persona_port():
        _ready = True
    else:
        logger.error("PersonaPlex did not become ready within 120 s")


async def _stop_persona_server():
    global _persona_process, _ready
    _ready = False
    if _persona_process is None:
        return
    logger.info("Stopping PersonaPlex (PID %d)", _persona_process.pid)
    _persona_process.terminate()
    try:
        _persona_process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        _persona_process.kill()
        _persona_process.wait()
    _persona_process = None


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _elmeeda
    await _start_persona_server()
    if ELMEEDA_USERNAME:
        _elmeeda = ElmeedaClient(ELMEEDA_API_URL, ELMEEDA_USERNAME, ELMEEDA_PASSWORD)
        logger.info("Elmeeda API client initialised (%s)", ELMEEDA_API_URL)
    yield
    if _elmeeda:
        await _elmeeda.close()
    await _stop_persona_server()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Elmeeda Voice Agent", lifespan=lifespan)


@app.get("/")
async def root():
    return {"service": "Elmeeda Voice Agent", "status": "running"}


@app.get("/healthz")
async def healthz():
    alive = _persona_process is not None and _persona_process.poll() is None
    if not alive:
        return JSONResponse({"healthy": False}, status_code=503)
    return {"healthy": True}


@app.get("/readyz")
async def readyz():
    if not _ready:
        return JSONResponse({"ready": False}, status_code=503)
    return {"ready": True}


@app.websocket("/ws/twilio")
async def twilio_websocket(ws: WebSocket):
    await ws.accept()
    logger.info("Twilio websocket connected from %s", ws.client)
    bridge = TwilioBridge(
        twilio_ws=ws,
        persona_ws_url=PERSONA_WS_URL,
        elmeeda_client=_elmeeda,
        voice_prompt=DEFAULT_VOICE_PROMPT,
    )
    try:
        await bridge.run()
    except WebSocketDisconnect:
        logger.info("Twilio websocket disconnected")
    except Exception:
        logger.exception("Unhandled bridge error")
    finally:
        logger.info("Twilio session ended")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level=LOG_LEVEL,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")

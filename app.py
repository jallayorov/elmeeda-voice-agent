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
from fastapi.responses import HTMLResponse, JSONResponse

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

TEST_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Elmeeda Voice Test</title>
<style>
body{font-family:system-ui;max-width:700px;margin:40px auto;padding:0 20px;background:#1a1a2e;color:#e0e0e0}
h1{color:#00d4ff}button{padding:10px 24px;font-size:16px;border:none;border-radius:6px;cursor:pointer;margin:4px}
#start{background:#00d4ff;color:#000}#stop{background:#ff4757;color:#fff}
#status{padding:8px 12px;border-radius:4px;margin:12px 0;font-weight:bold}
.connected{background:#2ed573;color:#000}.disconnected{background:#ff4757}
.connecting{background:#ffa502;color:#000}
#transcript{background:#16213e;border:1px solid #333;border-radius:6px;padding:16px;min-height:200px;
  max-height:400px;overflow-y:auto;white-space:pre-wrap;font-size:14px;line-height:1.5}
</style></head><body>
<h1>&#x1f399; Elmeeda Voice Agent Test</h1>
<div><button id="start" onclick="startCall()">Start Call</button>
<button id="stop" onclick="stopCall()" disabled>Stop Call</button></div>
<div id="status" class="disconnected">Disconnected</div>
<h3>PersonaPlex Transcript</h3><div id="transcript"></div>
<script>
let ws,audioCtx,micStream,scriptNode,streamSid='test-'+Date.now();
const MULAW_ENCODE=new Int8Array(65536);
(function(){for(let i=0;i<65536;i++){let s=i>32767?i-65536:i;let sign=0;
if(s<0){sign=0x80;s=-s}if(s>32635)s=32635;s+=0x84;
let exp=0,m=s;while(m>0x3F){m>>=1;exp++}
let mantissa=(s>>(exp+3))&0x0F;MULAW_ENCODE[i&0xFFFF]=~(sign|((exp&7)<<4)|mantissa)&0xFF}})();

const MULAW_DECODE=new Float32Array(256);
(function(){for(let i=0;i<256;i++){let u=~i&0xFF;let sign=(u&0x80)?-1:1;
let exp=(u>>4)&7;let man=u&0x0F;
let sample=sign*((man*2+33)*(1<<(exp+2))-33);MULAW_DECODE[i]=sample/32768.0}})();

function setStatus(text,cls){const el=document.getElementById('status');el.textContent=text;el.className=cls}
function log(t){const el=document.getElementById('transcript');el.textContent+=t;el.scrollTop=el.scrollHeight}

async function startCall(){
  const proto=location.protocol==='https:'?'wss:':'ws:';
  ws=new WebSocket(proto+'//'+location.host+'/ws/twilio');
  ws.onopen=()=>{
    setStatus('Connected','connected');
    ws.send(JSON.stringify({event:'connected',protocol:'Call',version:'1.0'}));
    ws.send(JSON.stringify({event:'start',start:{streamSid:streamSid,
      accountSid:'TEST',callSid:'TEST',customParameters:{}}}));
    startMic();
  };
  ws.onmessage=(e)=>{
    try{const m=JSON.parse(e.data);
      if(m.event==='media'&&m.media&&m.media.payload){playMulaw(m.media.payload)}
    }catch(err){
      // might be text token or other
      log(e.data);
    }
  };
  ws.onclose=()=>{setStatus('Disconnected','disconnected');stopMic()};
  ws.onerror=()=>setStatus('Error','disconnected');
  setStatus('Connecting...','connecting');
  document.getElementById('start').disabled=true;
  document.getElementById('stop').disabled=false;
}

function stopCall(){
  if(ws){ws.send(JSON.stringify({event:'stop',stop:{}}));ws.close()}
  stopMic();
  document.getElementById('start').disabled=false;
  document.getElementById('stop').disabled=true;
}

async function startMic(){
  audioCtx=new AudioContext({sampleRate:8000});
  micStream=await navigator.mediaDevices.getUserMedia({audio:{sampleRate:{ideal:8000},
    channelCount:1,echoCancellation:true,noiseSuppression:true}});
  const src=audioCtx.createMediaStreamSource(micStream);
  scriptNode=audioCtx.createScriptProcessor(512,1,1);
  scriptNode.onaudioprocess=(e)=>{
    if(!ws||ws.readyState!==1)return;
    const inp=e.inputBuffer.getChannelData(0);
    const mulaw=new Uint8Array(inp.length);
    for(let i=0;i<inp.length;i++){
      const s=Math.max(-1,Math.min(1,inp[i]));
      const s16=Math.floor(s*32767)&0xFFFF;
      mulaw[i]=MULAW_ENCODE[s16]&0xFF;
    }
    // Send in 160-byte chunks (20ms at 8kHz)
    for(let off=0;off<mulaw.length;off+=160){
      const chunk=mulaw.slice(off,Math.min(off+160,mulaw.length));
      const b64=btoa(String.fromCharCode(...chunk));
      ws.send(JSON.stringify({event:'media',streamSid:streamSid,
        media:{payload:b64,track:'inbound',timestamp:Date.now().toString()}}));
    }
  };
  src.connect(scriptNode);scriptNode.connect(audioCtx.destination);
}

function stopMic(){
  if(scriptNode){scriptNode.disconnect();scriptNode=null}
  if(micStream){micStream.getTracks().forEach(t=>t.stop());micStream=null}
  if(audioCtx){audioCtx.close();audioCtx=null}
}

let playCtx,playNextTime=0;
function playMulaw(b64){
  if(!playCtx)playCtx=new AudioContext({sampleRate:8000});
  const raw=atob(b64);const mulaw=new Uint8Array(raw.length);
  for(let i=0;i<raw.length;i++)mulaw[i]=raw.charCodeAt(i);
  const pcm=new Float32Array(mulaw.length);
  for(let i=0;i<mulaw.length;i++)pcm[i]=MULAW_DECODE[mulaw[i]];
  const buf=playCtx.createBuffer(1,pcm.length,8000);
  buf.getChannelData(0).set(pcm);
  const src=playCtx.createBufferSource();src.buffer=buf;
  src.connect(playCtx.destination);
  const now=playCtx.currentTime;
  if(playNextTime<now)playNextTime=now;
  src.start(playNextTime);playNextTime+=buf.duration;
}
</script></body></html>"""

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


@app.get("/test", response_class=HTMLResponse)
async def test_ui():
    """Browser-based voice test UI that speaks Twilio Media Streams protocol."""
    return HTMLResponse(content=TEST_HTML)


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

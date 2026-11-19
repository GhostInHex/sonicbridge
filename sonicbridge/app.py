"""Local dashboard API and audio worker."""
from base64 import b64decode, b64encode
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Event, Lock, Thread
from time import time
from typing import Literal

import numpy as np
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field

from .link import Audio, Cancelled, Message, Receiver, Simulation, send_message
from .modem import MAX_FILE, RATE


class Devices(BaseModel):
    input_device: int | None = Field(default=None, ge=0)
    output_device: int | None = Field(default=None, ge=0)


class Send(Devices):
    transport: Literal["simulation", "acoustic"]
    mode: Literal["adaptive", "balanced", "robust"] = "adaptive"
    name: str = Field(default="", max_length=96)
    data: str = Field(max_length=2732)
    noise: float = Field(default=0.03, ge=0, le=0.75)
    drop_ack: int = Field(default=0, ge=0, le=2)


class Station:
    def __init__(self):
        self.lock = Lock()
        self.cancel = Event()
        self.thread = None
        self.events = deque(maxlen=100)
        self.inbox = deque(maxlen=20)
        self.status = "idle"
        self.error = ""
        self.profile = None
        self.completed = self.total = 0
        self.quality = self.goodput = None
        self.wave = []
        self.spectra = deque(maxlen=72)
        self.counts = self.fresh_counts()
        self.event_id = 0

    @staticmethod
    def fresh_counts():
        return dict.fromkeys(("tx", "rx", "ack", "retry", "timeout", "crc"), 0)

    def report(self, kind, message):
        with self.lock:
            self.event_id += 1
            self.events.append({"id": self.event_id, "kind": kind, "message": message, "at": time()})
            if kind in self.counts:
                self.counts[kind] += 1

    def deliver(self, message):
        with self.lock:
            self.inbox.append({
                "name": message.name,
                "size": len(message.data),
                "data": b64encode(message.data).decode(),
                "text": None if message.name else message.data.decode(),
            })
        self.report("done", f"Received {message.name or 'message'} · {len(message.data)} bytes")

    def observe(self, block):
        window = block[-min(960, len(block)):]
        fft = np.abs(np.fft.rfft(window * np.hanning(len(window)), n=960))[:97]
        level = np.clip((20 * np.log10(np.maximum(fft / 120, 1e-7)) + 74) / 74, 0, 1)
        with self.lock:
            self.wave = np.round(block[-960:][::4], 4).tolist()
            self.spectra.append(np.round(level, 2).tolist())

    def progress(self, profile, completed, total, quality=None):
        with self.lock:
            self.profile, self.completed, self.total = profile, completed, total
            if quality is not None:
                self.quality = quality

    def snapshot(self):
        with self.lock:
            return {
                "status": self.status, "error": self.error, "profile": self.profile,
                "completed": self.completed, "total": self.total, "quality": self.quality,
                "goodput": self.goodput, "wave": self.wave, "spectra": list(self.spectra),
                "events": list(self.events), "inbox": list(self.inbox), "counts": self.counts.copy(),
            }

    def start(self, status, job):
        with self.lock:
            if self.thread and self.thread.is_alive():
                raise HTTPException(409, "An audio operation is already running")
            self.cancel = Event()
            self.status, self.error = status, ""
            self.profile = self.quality = self.goodput = None
            self.completed = self.total = 0
            self.events.clear()
            self.wave = []
            self.spectra.clear()
            self.counts = self.fresh_counts()
            self.thread = Thread(target=self.run, args=(job,), daemon=True)
            self.thread.start()

    def run(self, job):
        status, error = "complete", ""
        try:
            job()
        except Cancelled:
            status = "idle"
            self.report("info", "Operation stopped")
        except Exception as exc:
            status, error = "failed", str(exc)
            self.report("error", error)
        with self.lock:
            self.status, self.error = status, error

    def audio(self, devices):
        return Audio(self.cancel, self.report, self.observe, devices.input_device, devices.output_device)

    def send(self, options, message):
        if options.transport == "simulation":
            receiver = Receiver(self.report, self.deliver, options.drop_ack)
            transport = Simulation(receiver, self.cancel, self.observe, self.report, options.noise)
            self.report("info", "Simulated peer ready · no audio device in use")
        else:
            transport = self.audio(options)
        goodput = send_message(message, options.mode, transport.exchange, self.cancel, self.report, self.progress)
        if isinstance(transport, Simulation):
            # Report encoded airtime rather than accelerated simulation wall time.
            goodput = len(message.data) * 8 / (transport.samples / RATE)
        with self.lock:
            self.goodput = goodput
        self.report("done", "All packets acknowledged")


station = Station()


@asynccontextmanager
async def lifespan(_app):
    yield
    station.cancel.set()
    if station.thread:
        station.thread.join(timeout=2)


app = FastAPI(title="SonicBridge", lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1"])


@app.middleware("http")
async def local_browser_only(request: Request, call_next):
    if request.method == "POST":
        origin = request.headers.get("origin")
        if origin and origin != f"{request.url.scheme}://{request.headers.get('host')}":
            return JSONResponse({"detail": "Use the local SonicBridge dashboard"}, status_code=403)
    return await call_next(request)


@app.get("/api/state")
def state():
    return station.snapshot()


@app.get("/api/devices")
def devices():
    try:
        import sounddevice as sd

        return {"devices": [
            {"id": i, "name": device["name"], "input": device["max_input_channels"] > 0,
             "output": device["max_output_channels"] > 0}
            for i, device in enumerate(sd.query_devices())
        ], "error": ""}
    except (ImportError, OSError) as exc:
        return {"devices": [], "error": str(exc)}


@app.post("/api/send", status_code=202)
def send(options: Send):
    try:
        message = Message(options.name, b64decode(options.data, validate=True))
        Message.unpack(message.pack())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    station.start("sending", lambda: station.send(options, message))
    return {"status": "sending"}


@app.post("/api/listen", status_code=202)
def listen(options: Devices):
    def job():
        station.report("info", "Listening for acoustic packets")
        station.audio(options).receive(Receiver(station.report, station.deliver))
    station.start("listening", job)
    return {"status": "listening"}


@app.post("/api/stop")
def stop():
    station.cancel.set()
    return {"status": "stopping"}


dist = Path(__file__).resolve().parents[1] / "web" / "dist"
if dist.is_dir():
    app.mount("/", StaticFiles(directory=dist, html=True), name="dashboard")

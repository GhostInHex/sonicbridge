"""Local dashboard API and audio worker."""
from base64 import b64decode, b64encode
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from os import environ
from secrets import randbits
from time import monotonic
from threading import Event, Lock, Thread
from time import time
from typing import Literal

import numpy as np
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field

from .compare import compare
from .link import Audio, Cancelled, Message, Receiver, Simulation, check_cancel, send_payload
from .seal import MAGIC, seal
from .storage import Store
from .modem import RATE, Kind, Packet, Profile
from .postcards import POSTCARD_NAME, POSTCARDS, phone_page
from .recordings import decode_sample, sample_wav


class Devices(BaseModel):
    input_device: int | None = Field(default=None, ge=0)
    output_device: int | None = Field(default=None, ge=0)


class Listen(Devices):
    passphrase: str = Field(default="", max_length=256)


class Channel(Devices):
    transport: Literal["simulation", "acoustic"]
    mode: Literal["adaptive", "balanced", "robust", "fast"] = "adaptive"
    noise: float = Field(default=0.03, ge=0, le=0.75)
    drop_ack: int = Field(default=0, ge=0, le=2)


class Send(Channel):
    name: str = Field(default="", max_length=96)
    data: str = Field(max_length=2732)
    passphrase: str = Field(default="", max_length=256)


class Compare(BaseModel):
    scenario: Literal["quiet", "interference"]


DEMO_PHRASE = "sonicbridge stage demo"
DEMO_MESSAGE = Message("", (
    "This message traveled through simulated sound. SonicBridge recovered from two lost replies, "
    "resumed an interrupted transfer, and verified the encrypted payload before displaying it."
).encode())



class Station:
    def __init__(self, store):
        self.store = store
        self.lock = Lock()
        self.cancel = Event()
        self.thread = None
        self.events = deque(maxlen=100)
        self.inbox = deque((self.inbox_item(Message(name, data), encrypted)
                            for name, data, encrypted in store.messages()), maxlen=20)
        self.probe_result = None
        self.comparison = []
        self.status = "idle"
        self.transport = None
        self.latest_delivery = None
        self.last_signal = None
        self.recording_result = None
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
            if kind == "rx" and self.spectra:
                self.last_signal = {
                    "wave": self.wave, "spectra": list(self.spectra), "source": self.transport,
                }

    @staticmethod
    def inbox_item(message, encrypted):
        return {
            "name": message.name,
            "size": len(message.data),
            "data": b64encode(message.data).decode(),
            "text": None if message.name else message.data.decode(),
            "encrypted": bool(encrypted),
        }

    def deliver(self, message, encrypted):
        with self.lock:
            self.latest_delivery = self.inbox_item(message, encrypted)
            self.inbox.append(self.latest_delivery)
        label = " · authenticated" if encrypted else ""
        self.report("done", f"Received {message.name or 'message'} · {len(message.data)} bytes{label}")

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
            pending = self.store.pending()
            checkpoint = None if not pending else {
                "size": pending[1], "encrypted": pending[2].startswith(MAGIC),
                "transport": pending[3]["transport"], "demo": pending[3].get("demo", False),
            }
            return {
                "checkpoint": checkpoint, "probe": self.probe_result, "comparison": self.comparison.copy(),
                "status": self.status, "error": self.error, "profile": self.profile,
                "transport": self.transport, "latest_delivery": self.latest_delivery,
                "last_signal": self.last_signal, "recording": self.recording_result,
                "postcards": POSTCARDS, "postcard_name": POSTCARD_NAME,
                "completed": self.completed, "total": self.total, "quality": self.quality,
                "goodput": self.goodput, "wave": self.wave, "spectra": list(self.spectra),
                "events": list(self.events), "inbox": list(self.inbox), "counts": self.counts.copy(),
            }

    def start(self, status, job, *, transport, clear_probe=False, clear_comparison=False, require_empty=False):
        with self.lock:
            if self.thread and self.thread.is_alive():
                raise HTTPException(409, "An audio operation is already running")
            if require_empty and self.store.pending():
                raise HTTPException(409, "Resume or discard the saved transfer first")
            self.cancel = Event()
            self.status, self.error = status, ""
            self.transport = transport
            self.latest_delivery = None
            if transport == "recording":
                self.recording_result = None
            if clear_probe:
                self.probe_result = None
            if clear_comparison:
                self.comparison = []
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

    def channel(self, options, passphrase="", *, resumed=False, probe=False):
        if options.transport == "acoustic":
            return self.audio(options)
        receiver = Receiver(self.report, self.deliver, 0 if resumed or probe else options.drop_ack,
                            passphrase=passphrase, store=self.store)
        self.report("info", "Simulated peer ready · no audio device in use")
        return Simulation(receiver, self.cancel, self.observe, self.report, options.noise)

    def new_send(self, options, message, passphrase, *, demo=False):
        transfer = randbits(64)
        payload = seal(message.pack(), passphrase, transfer)
        checkpoint_options = options.model_dump() | {"demo": demo}
        self.store.save_pending(transfer, len(message.data), payload, checkpoint_options)
        self.send(options, transfer, len(message.data), payload, passphrase, pause=demo)

    def send(self, options, transfer, size, payload, passphrase, *, resumed=False, pause=False):
        channel = self.channel(options, passphrase, resumed=resumed)

        def progress(profile, completed, total, quality=None):
            self.progress(profile, completed, total, quality)
            if pause and completed == 1:
                self.report("resume", "Simulated interruption · first packet saved. Resume to send the rest.")
                raise Cancelled()

        if payload.startswith(MAGIC):
            self.report("info", "Encrypted envelope · AES-GCM")
        goodput = send_payload(payload, size, transfer, options.mode, channel.exchange,
                               self.cancel, self.report, progress, resume=resumed)
        self.store.clear_pending()
        if isinstance(channel, Simulation):
            goodput = size * 8 / channel.seconds
        with self.lock:
            self.goodput = None if resumed else goodput
        self.report("done", "All packets acknowledged")

    def probe(self, options):
        channel = self.channel(options, probe=True)
        probe = Packet(Kind.PING, Profile.BALANCED, randbits(64), 0, 1, b"")
        start = monotonic()
        self.report("tx", "Sending a round-trip link probe")
        reply = channel.exchange(probe)
        check_cancel(self.cancel)
        quality = reply.payload[0] if reply and reply.kind is Kind.ACK else None
        duration = channel.seconds if isinstance(channel, Simulation) else monotonic() - start
        with self.lock:
            self.probe_result = {
                "transport": options.transport, "reachable": quality is not None,
                "seconds": round(duration, 2), "quality": quality,
                "recommendation": None if quality is None else "robust" if quality < 16 else "balanced",
            }
        self.report("ack" if quality is not None else "timeout",
                    "Round-trip probe acknowledged" if quality is not None else "No probe reply heard")

    def compare(self, scenario):
        def publish(row):
            with self.lock:
                self.comparison.append(row)
        compare(scenario, self.cancel, self.observe, self.report, publish)

    def decode_recording(self):
        result = decode_sample(self.cancel, self.observe, self.report)
        with self.lock:
            self.recording_result = result


state_dir = Path(environ.get("SONICBRIDGE_STATE_DIR", Path.home() / ".local" / "share" / "sonicbridge"))
station = Station(Store(state_dir / "station.sqlite3"))


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


@app.get("/api/postcards/phone")
def postcard_phone():
    return HTMLResponse(phone_page(), headers={
        "Content-Disposition": 'attachment; filename="sonicbridge-postcards.html"',
        "Cache-Control": "no-store",
    })


@app.get("/api/recording/sample")
def recording_sample():
    return Response(sample_wav(), media_type="audio/wav", headers={
        "Content-Disposition": 'attachment; filename="sonicbridge-sample.wav"',
    })


@app.post("/api/recording/decode", status_code=202)
def recording_decode():
    station.start("decoding", station.decode_recording, transport="recording")
    return {"status": "decoding"}


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
    if options.passphrase and len(options.passphrase) < 12:
        raise HTTPException(422, "Use a shared passphrase of at least 12 characters")
    channel = Channel.model_validate(options.model_dump())
    station.start("sending", lambda: station.new_send(channel, message, options.passphrase),
                  transport=channel.transport, require_empty=True)
    return {"status": "sending"}


@app.post("/api/listen", status_code=202)
def listen(options: Listen):
    def job():
        station.report("info", "Listening for acoustic packets")
        station.audio(options).receive(Receiver(station.report, station.deliver,
                                                passphrase=options.passphrase, store=station.store))
    station.start("listening", job, transport="acoustic")
    return {"status": "listening"}


@app.post("/api/probe", status_code=202)
def probe(options: Channel):
    station.start("probing", lambda: station.probe(options), transport=options.transport, clear_probe=True)
    return {"status": "probing"}


@app.post("/api/compare", status_code=202)
def comparison(options: Compare):
    station.start("comparing", lambda: station.compare(options.scenario),
                  transport="simulation", clear_comparison=True)
    return {"status": "comparing"}


@app.post("/api/demo", status_code=202)
def demo():
    options = Channel(transport="simulation", drop_ack=2)
    station.start("sending", lambda: station.new_send(options, DEMO_MESSAGE, DEMO_PHRASE, demo=True),
                  transport="simulation", require_empty=True)
    return {"status": "sending"}


@app.post("/api/resume", status_code=202)
def resume(options: Listen):
    checkpoint = station.store.pending()
    if checkpoint is None:
        raise HTTPException(404, "No saved transfer")
    transfer, size, payload, saved = checkpoint
    channel = Channel.model_validate(saved)
    if channel.transport == "acoustic":
        channel.input_device = options.input_device
        channel.output_device = options.output_device
    passphrase = DEMO_PHRASE if saved.get("demo") else options.passphrase
    if channel.transport == "simulation" and payload.startswith(MAGIC) and not passphrase:
        raise HTTPException(422, "Enter the original shared passphrase to resume the simulation")
    def job():
        if station.store.pending() != checkpoint:
            raise RuntimeError("The saved transfer changed. Reload and try again.")
        station.send(channel, transfer, size, payload, passphrase, resumed=True)
    station.start("sending", job, transport=channel.transport)
    return {"status": "sending"}


@app.post("/api/discard")
def discard():
    with station.lock:
        if station.thread and station.thread.is_alive():
            raise HTTPException(409, "Stop the current operation first")
        station.store.clear_pending()
    return {"status": "discarded"}


@app.post("/api/stop")
def stop():
    station.cancel.set()
    return {"status": "stopping"}


dist = Path(__file__).resolve().parents[1] / "web" / "dist"
if dist.is_dir():
    app.mount("/", StaticFiles(directory=dist, html=True), name="dashboard")

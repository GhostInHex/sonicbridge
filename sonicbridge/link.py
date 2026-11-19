"""Stop-and-wait transfer, reassembly, and audio transports."""
from collections import OrderedDict
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import PurePath
from secrets import randbits
from time import monotonic, sleep

import numpy as np

from .modem import BITMAP_BYTES, CHUNK, MAX_FILE, MAX_NAME, PREFIX_SAMPLES, RATE, Decoder, Kind, Packet, Profile, encode
from .seal import MAGIC, LockedMessage, seal, unseal


@dataclass(frozen=True)
class Message:
    name: str
    data: bytes

    def pack(self):
        name = self.name.encode()
        if len(name) > MAX_NAME or not 1 <= len(self.data) <= MAX_FILE:
            raise ValueError("Use 1–2048 data bytes and a filename of at most 96 UTF-8 bytes")
        return bytes((len(name),)) + name + self.data

    @classmethod
    def unpack(cls, data):
        if not data:
            raise ValueError("Empty message envelope")
        length = data[0]
        if length > MAX_NAME or not 1 <= len(data) - 1 - length <= MAX_FILE:
            raise ValueError("Invalid message length")
        name = data[1:1 + length].decode()
        if name and (PurePath(name).name != name or '\\' in name or any(ord(c) < 32 for c in name) or name in ('.', '..')):
            raise ValueError("Invalid filename")
        payload = data[1 + length:]
        if not name:
            payload.decode()
        return cls(name, payload)


@dataclass
class Assembly:
    total: int
    seen: float
    chunks: dict[int, bytes] = field(default_factory=dict)
    completed: bool = False


class Receiver:
    def __init__(self, report, deliver, drop_ack=0, *, passphrase="", store=None):
        self.report = report
        self.deliver = deliver
        self.drop_ack = drop_ack
        self.passphrase = passphrase
        self.store = store
        self.transfers = OrderedDict()

    def receive(self, packet, quality):
        kind = packet.kind
        reply_profile = Profile.FAST if packet.profile is Profile.FAST else Profile.BALANCED
        def reply(reply_kind, payload=b"", profile=Profile.BALANCED):
            return Packet(reply_kind, profile, packet.transfer, packet.index, packet.total, payload)

        if kind is Kind.PING:
            self.report("info", "Link probe received")
            return reply(Kind.ACK, bytes((round(quality),)))
        if kind not in (Kind.DATA, Kind.QUERY):
            return None

        delivered = None
        with self.store.connection() if self.store else nullcontext() as db:
            now = monotonic()
            if self.store:
                stored = self.store.load(db, packet.transfer)
                state = Assembly(stored[0], now, stored[2], stored[1]) if stored else None
            else:
                while self.transfers and now - next(iter(self.transfers.values())).seen > 1800:
                    self.transfers.popitem(last=False)
                state = self.transfers.get(packet.transfer)
            if state is None:
                state = Assembly(packet.total, now)
            if state.total != packet.total:
                return None
            if kind is Kind.QUERY:
                bits = sum(1 << index for index in state.chunks)
                if len(state.chunks) == state.total and not state.completed:
                    # The final ACK requires successful assembly and authentication.
                    bits &= ~(1 << (state.total - 1))
                return reply(Kind.HAVE, bits.to_bytes(BITMAP_BYTES, "big"))
            previous = state.chunks.get(packet.index)
            if previous is not None and previous != packet.payload:
                return None
            state.chunks[packet.index] = packet.payload
            if self.store:
                self.store.save_chunk(db, packet)
            else:
                state.seen = now
                self.transfers[packet.transfer] = state
                self.transfers.move_to_end(packet.transfer)
                if len(self.transfers) > 64:
                    self.transfers.popitem(last=False)
            if previous is None:
                self.report("rx", f"Packet {packet.index + 1}/{packet.total} · CRC verified")
            else:
                self.report("duplicate", f"Duplicate packet {packet.index + 1} · delivery suppressed")
            if len(state.chunks) == state.total and not state.completed:
                envelope = b"".join(state.chunks[i] for i in range(state.total))
                encrypted = envelope.startswith(MAGIC)
                try:
                    message = Message.unpack(unseal(envelope, self.passphrase, packet.transfer))
                except LockedMessage as exc:
                    self.report("error", str(exc))
                    return reply(Kind.LOCKED)
                except ValueError:
                    self.report("error", "Invalid message envelope")
                    return None
                if self.store:
                    self.store.finish(db, packet, message, encrypted)
                state.completed = True
                delivered = message, encrypted
        if delivered:
            self.deliver(*delivered)
        if self.drop_ack:
            self.drop_ack -= 1
            self.report("info", "Demo channel dropped the acknowledgment")
            return None
        return reply(Kind.ACK, bytes((round(quality),)), reply_profile)


class Cancelled(Exception):
    pass


def check_cancel(cancel):
    if cancel.is_set():
        raise Cancelled()


ACK_WAIT = 6.5
ACK_TURNAROUND = 0.45


def send_message(message, mode, exchange, cancel, report, progress):
    return send_payload(message.pack(), len(message.data), randbits(64), mode,
                        exchange, cancel, report, progress)


def send_payload(data, size, transfer, mode, exchange, cancel, report, progress, *, resume=False):
    chunks = [data[i:i + CHUNK] for i in range(0, len(data), CHUNK)]
    received = set()
    if resume:
        query = Packet(Kind.QUERY, Profile.BALANCED, transfer, 0, len(chunks), b"")
        for _ in range(3):
            check_cancel(cancel)
            reply = exchange(query)
            if reply and reply.kind is Kind.HAVE:
                bits = int.from_bytes(reply.payload, "big")
                received = {i for i in range(len(chunks)) if bits & (1 << i)}
                report("resume", f"Peer already has {len(received)}/{len(chunks)} packets")
                break
        else:
            raise RuntimeError("No resume reply. Start listening on the peer and try again.")
    profile = Profile.BALANCED if mode == "adaptive" else Profile[mode.upper()]
    misses = clean = 0
    start = monotonic()
    for index, chunk in enumerate(chunks):
        if index in received:
            progress(profile.name.lower(), len(received), len(chunks))
            continue
        for attempt in range(1, 6):
            check_cancel(cancel)
            packet = Packet(Kind.DATA, profile, transfer, index, len(chunks), chunk)
            progress(profile.name.lower(), len(received), len(chunks))
            report("tx", f"Packet {index + 1}/{len(chunks)} · {profile.name.title()} · attempt {attempt}")
            ack = exchange(packet)
            check_cancel(cancel)
            if ack is not None and ack.kind is Kind.LOCKED:
                raise RuntimeError("Peer could not unlock this message. Correct the receiving passphrase and resume.")
            if ack is not None and ack.kind is Kind.ACK:
                quality = ack.payload[0]
                misses = 0
                clean = clean + 1 if quality >= 16 else 0
                report("ack", f"ACK {index + 1} · tone separation {quality} dB")
                if mode == "adaptive" and profile is Profile.BALANCED and quality < 9:
                    profile = Profile.ROBUST
                    report("profile", "Robust enabled · weak tone separation")
                elif mode == "adaptive" and profile is Profile.ROBUST and clean >= 5:
                    profile = Profile.BALANCED
                    clean = 0
                    report("profile", "Balanced enabled · five strong acknowledgments")
                received.add(index)
                progress(packet.profile.name.lower(), len(received), len(chunks), quality)
                break
            clean = 0
            misses += 1
            if ack is None:
                report("timeout", f"ACK timeout for packet {index + 1}")
            else:
                report("nack", f"NACK received for packet {index + 1}")
            if attempt == 5:
                raise RuntimeError(f"Packet {index + 1} exhausted five attempts")
            report("retry", f"Retrying packet {index + 1}")
            if mode == "adaptive" and misses >= 2 and profile is Profile.BALANCED:
                profile = Profile.ROBUST
                report("profile", "Robust enabled · two consecutive delivery failures")
    return size * 8 / max(monotonic() - start, 1e-3)


class Audio:
    def __init__(self, cancel, report, observe, input_device, output_device):
        import sounddevice as sd

        self.sd = sd
        self.cancel = cancel
        self.report = report
        self.observe = observe
        self.input_device = input_device
        self.output_device = output_device

    def play(self, packet):
        samples = np.concatenate((np.zeros(RATE // 4, dtype=np.float32), encode(packet),
                                  np.zeros(RATE // 20, dtype=np.float32)))
        with self.sd.OutputStream(device=self.output_device, samplerate=RATE, channels=1, dtype="float32") as stream:
            for offset in range(0, len(samples), RATE // 10):
                check_cancel(self.cancel)
                block = samples[offset:offset + RATE // 10]
                self.observe(block)
                stream.write(block[:, None])
            # Drain the last queued block before opening the receive stream.
            stream.stop()

    def listen(self, accept, timeout=None, return_rejections=False):
        decoder = Decoder()
        deadline = None if timeout is None else monotonic() + timeout
        with self.sd.InputStream(device=self.input_device, samplerate=RATE, channels=1, dtype="float32") as stream:
            while deadline is None or monotonic() < deadline:
                check_cancel(self.cancel)
                block, overflow = stream.read(RATE // 10)
                block = block[:, 0]
                if overflow:
                    self.report("error", "Microphone input overflow · resynchronizing")
                    decoder = Decoder()
                self.observe(block)
                before = decoder.crc_errors
                packets = decoder.feed(block)
                for _ in range(decoder.crc_errors - before):
                    self.report("crc", "Packet rejected · CRC mismatch")
                for packet, quality in packets:
                    if accept(packet):
                        return packet, quality
                if return_rejections and decoder.rejections:
                    return decoder.rejections[0], 0
        return None

    def exchange(self, packet):
        self.play(packet)
        result = self.listen(
            lambda ack: ack.kind in (Kind.ACK, Kind.NACK, Kind.HAVE, Kind.LOCKED) and (ack.transfer, ack.index, ack.total)
            == (packet.transfer, packet.index, packet.total),
            timeout=ACK_WAIT,
        )
        return result[0] if result else None

    def receive(self, receiver):
        while True:
            packet, quality = self.listen(lambda packet: packet.kind in (Kind.DATA, Kind.PING, Kind.QUERY), return_rejections=True)
            ack = packet if packet.kind is Kind.NACK else receiver.receive(packet, quality)
            if ack is not None:
                self.report(ack.kind.name.lower(), f"Sending {ack.kind.name} {packet.index + 1} as sound")
                if self.cancel.wait(ACK_TURNAROUND):
                    raise Cancelled()
                self.play(ack)


class Simulation:
    def __init__(self, receiver, cancel, observe, report, noise, *, echo=0, burst_ms=0, paced=True):
        self.receiver = receiver
        self.cancel = cancel
        self.observe = observe
        self.report = report
        self.noise = noise
        self.echo = echo
        self.burst_ms = burst_ms
        self.paced = paced
        self.seconds = 0.
        self.random = np.random.default_rng(41)

    def transmit(self, packet):
        lead = 3149
        signal = np.concatenate((np.zeros(lead), encode(packet), np.zeros(1200)))
        delay = RATE * 12 // 1000
        if self.echo:
            signal[delay:] += self.echo * signal[:-delay].copy()
        if self.burst_ms and packet.kind is Kind.DATA:
            start = lead + PREFIX_SAMPLES + RATE * 60 // 1000
            length = min(RATE * self.burst_ms // 1000, max(0, len(signal) - start))
            t = np.arange(length) / RATE
            signal[start:start + length] += .8 * np.sin(2 * np.pi * 2200 * t)
        signal += self.random.normal(0, self.noise, len(signal))
        self.seconds += len(signal) / RATE
        decoder = Decoder()
        for block in np.array_split(signal, 6):
            check_cancel(self.cancel)
            self.observe(block[-min(4800, len(block)):])
            decoded = decoder.feed(block)
            if self.paced:
                sleep(0.055)
            if decoded:
                return decoded[0]
            if decoder.rejections:
                self.report("crc", "Simulated packet rejected · CRC mismatch")
                return decoder.rejections[0], 0
        return None

    def exchange(self, packet):
        data = self.transmit(packet)
        if data is None:
            self.seconds += ACK_WAIT
            return None
        packet, quality = data
        ack = packet if packet.kind is Kind.NACK else self.receiver.receive(packet, quality)
        if ack is None:
            self.seconds += ACK_WAIT
            return None
        self.seconds += ACK_TURNAROUND
        decoded = self.transmit(ack)
        if not decoded:
            self.seconds += max(0, ACK_WAIT - ACK_TURNAROUND - len(encode(ack)) / RATE)
        return decoded[0] if decoded else None

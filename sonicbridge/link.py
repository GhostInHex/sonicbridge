"""Stop-and-wait transfer, reassembly, and audio transports."""
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import PurePath
from secrets import randbits
from time import monotonic, sleep

import numpy as np

from .modem import CHUNK, MAX_FILE, MAX_NAME, RATE, Decoder, Kind, Packet, Profile, encode


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
    def __init__(self, report, deliver, drop_ack=0):
        self.report = report
        self.deliver = deliver
        self.drop_ack = drop_ack
        self.transfers = OrderedDict()

    def receive(self, packet, quality):
        if packet.kind is not Kind.DATA:
            return None
        now = monotonic()
        while self.transfers and now - next(iter(self.transfers.values())).seen > 1800:
            self.transfers.popitem(last=False)
        state = self.transfers.get(packet.transfer)
        if state is None:
            if len(self.transfers) >= 64:
                self.transfers.popitem(last=False)
            state = self.transfers[packet.transfer] = Assembly(packet.total, now)
        if state.total != packet.total:
            return None
        state.seen = now
        self.transfers.move_to_end(packet.transfer)
        previous = state.chunks.get(packet.index)
        if previous is not None and previous != packet.payload:
            return None
        state.chunks[packet.index] = packet.payload
        if previous is None:
            self.report("rx", f"Packet {packet.index + 1}/{packet.total} · CRC verified")
        else:
            self.report("duplicate", f"Duplicate packet {packet.index + 1} · delivery suppressed")
        if len(state.chunks) == state.total and not state.completed:
            try:
                message = Message.unpack(b"".join(state.chunks[i] for i in range(state.total)))
            except ValueError:
                self.report("error", "Invalid message envelope")
                return None
            self.deliver(message)
            state.completed = True
        if self.drop_ack:
            self.drop_ack -= 1
            self.report("info", "Demo channel dropped the acknowledgment")
            return None
        return Packet(Kind.ACK, Profile.BALANCED, packet.transfer, packet.index, packet.total, bytes((round(quality),)))


class Cancelled(Exception):
    pass


def check_cancel(cancel):
    if cancel.is_set():
        raise Cancelled()


ACK_WAIT = 5.5
ACK_TURNAROUND = 0.45


def send_message(message, mode, exchange, cancel, report, progress):
    data = message.pack()
    chunks = [data[i:i + CHUNK] for i in range(0, len(data), CHUNK)]
    transfer = randbits(64)
    profile = Profile.ROBUST if mode == "robust" else Profile.BALANCED
    misses = clean = 0
    start = monotonic()
    for index, chunk in enumerate(chunks):
        for attempt in range(1, 6):
            check_cancel(cancel)
            packet = Packet(Kind.DATA, profile, transfer, index, len(chunks), chunk)
            progress(profile.name.lower(), index, len(chunks))
            report("tx", f"Packet {index + 1}/{len(chunks)} · {profile.name.title()} · attempt {attempt}")
            ack = exchange(packet)
            check_cancel(cancel)
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
                progress(packet.profile.name.lower(), index + 1, len(chunks), quality)
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
    return len(message.data) * 8 / max(monotonic() - start, 1e-3)


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
            lambda ack: ack.kind in (Kind.ACK, Kind.NACK) and (ack.transfer, ack.index, ack.total)
            == (packet.transfer, packet.index, packet.total),
            timeout=ACK_WAIT,
        )
        return result[0] if result else None

    def receive(self, receiver):
        while True:
            packet, quality = self.listen(lambda packet: packet.kind is Kind.DATA, return_rejections=True)
            ack = packet if packet.kind is Kind.NACK else receiver.receive(packet, quality)
            if ack is not None:
                self.report("ack" if ack.kind is Kind.ACK else "nack", f"Sending {ack.kind.name} {packet.index + 1} as sound")
                if self.cancel.wait(ACK_TURNAROUND):
                    raise Cancelled()
                self.play(ack)


class Simulation:
    def __init__(self, receiver, cancel, observe, report, noise):
        self.receiver = receiver
        self.cancel = cancel
        self.observe = observe
        self.report = report
        self.noise = noise
        self.samples = 0
        self.random = np.random.default_rng(41)

    def transmit(self, packet):
        signal = np.concatenate((np.zeros(3149), encode(packet), np.zeros(1200)))
        signal += self.random.normal(0, self.noise, len(signal))
        self.samples += len(signal)
        decoder = Decoder()
        for block in np.array_split(signal, 6):
            check_cancel(self.cancel)
            self.observe(block[-min(4800, len(block)):])
            decoded = decoder.feed(block)
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
            return None
        packet, quality = data
        ack = packet if packet.kind is Kind.NACK else self.receiver.receive(packet, quality)
        if ack is None:
            return None
        decoded = self.transmit(ack)
        return decoded[0] if decoded else None

"""BFSK framing and streaming decoding, independent of the audio device."""
from binascii import crc_hqx, crc32
from dataclasses import dataclass
from enum import IntEnum
from functools import lru_cache
import struct

import numpy as np

RATE = 48_000
FREQUENCIES = (1200, 2200)
SYNC = np.unpackbits(np.frombuffer(b"\x55" * 4 + b"\xd3\x91", dtype=np.uint8))
HEADER = struct.Struct("!BBQBBH")
PREFIX_SAMPLES = (len(SYNC) + HEADER.size * 8) * 960
CHUNK = 64
MAX_FILE = 2048
MAX_NAME = 96
MAX_CHUNKS = (MAX_FILE + MAX_NAME + 1 + 49 + CHUNK - 1) // CHUNK
BITMAP_BYTES = (MAX_CHUNKS + 7) // 8


class Profile(IntEnum):
    BALANCED = 0
    ROBUST = 1
    FAST = 2

    @property
    def samples(self):
        return (960, 1920, 480)[self]


class Kind(IntEnum):
    DATA = 1
    ACK = 2
    NACK = 3
    PING = 4
    QUERY = 5
    HAVE = 6
    LOCKED = 7


WIRE_FLAGS = {
    (Kind.DATA, Profile.BALANCED): 2, (Kind.DATA, Profile.ROBUST): 3,
    (Kind.ACK, Profile.BALANCED): 4, (Kind.NACK, Profile.BALANCED): 6,
    (Kind.DATA, Profile.FAST): 7, (Kind.ACK, Profile.FAST): 8,
    (Kind.PING, Profile.BALANCED): 9, (Kind.QUERY, Profile.BALANCED): 10,
    (Kind.HAVE, Profile.BALANCED): 11, (Kind.LOCKED, Profile.BALANCED): 12,
}
PACKET_TYPES = {flag: pair for pair, flag in WIRE_FLAGS.items()}


@dataclass(frozen=True)
class Packet:
    kind: Kind
    profile: Profile
    transfer: int
    index: int
    total: int
    payload: bytes

    def header(self):
        fields = struct.pack("!BBQBB", WIRE_FLAGS[self.kind, self.profile], len(self.payload),
                             self.transfer, self.index, self.total)
        return fields + struct.pack("!H", crc_hqx(fields, 0xFFFF))

    def body(self):
        return self.payload + struct.pack("!I", crc32(self.header() + self.payload))


def hamming_word(nibble):
    a, b, c, d = [(nibble >> shift) & 1 for shift in (3, 2, 1, 0)]
    return [a ^ b ^ d, a ^ c ^ d, a, b ^ c ^ d, b, c, d]


HAMMING = np.array([hamming_word(n) for n in range(16)], dtype=np.uint8)


def to_bits(data):
    return np.unpackbits(np.frombuffer(data, dtype=np.uint8))


def body_bits(data, profile):
    if profile is not Profile.ROBUST:
        return to_bits(data)
    nibbles = to_bits(data).reshape(-1, 4) @ np.array([8, 4, 2, 1])
    # Transmit each bit position across all codewords before the next.
    return HAMMING[nibbles].T.reshape(-1)


def decode_body(bits, profile):
    if profile is not Profile.ROBUST:
        return np.packbits(bits).tobytes()
    words = bits.reshape(7, -1).T
    distances = np.count_nonzero(words[:, None] != HAMMING, axis=2)
    nibbles = distances.argmin(axis=1).astype(np.uint8)
    return ((nibbles[::2] << 4) | nibbles[1::2]).tobytes()


@lru_cache
def tones(width):
    t = np.arange(width) / RATE
    return (0.25 * np.sin(2 * np.pi * np.array(FREQUENCIES)[:, None] * t)).astype(np.float32)


def encode(packet):
    prefix = np.concatenate((SYNC, to_bits(packet.header())))
    return np.concatenate((
        tones(Profile.BALANCED.samples)[prefix].ravel(),
        tones(packet.profile.samples)[body_bits(packet.body(), packet.profile)].ravel(),
    ))


def tone_energy(samples, starts, width):
    t = np.arange(len(samples)) / RATE
    energies = []
    for frequency in FREQUENCIES:
        mixed = samples * np.exp(-2j * np.pi * frequency * t)
        integral = np.concatenate(([0j], mixed.cumsum()))
        energies.append(np.abs(integral[starts + width] - integral[starts]) ** 2)
    return np.array(energies)


def decisions(energy):
    return (energy[1] > energy[0]).astype(np.uint8)


class Decoder:
    """Find a CRC-checked header, then wait for exactly its advertised body."""
    def __init__(self):
        self.samples = np.empty(0, dtype=np.float32)
        self.pending = None
        self.crc_errors = 0
        self.rejections = []

    def feed(self, samples):
        self.samples = np.concatenate((self.samples, samples))
        packets = []
        self.rejections.clear()
        while True:
            if self.pending is None and not self.find_header():
                break
            kind, profile, length, header, transfer, index, total = self.pending
            prefix = PREFIX_SAMPLES
            factor = 14 if profile is Profile.ROBUST else 8
            count = (length + 4) * factor
            end = prefix + count * profile.samples
            # The sync grid can place the estimated boundary slightly early or late.
            if len(self.samples) < end + 480:
                break
            window = profile.samples * 3 // 4
            starts = prefix + (profile.samples - window) // 2 + np.arange(count) * profile.samples
            energy = tone_energy(self.samples[:end], starts, window)
            raw = decode_body(decisions(energy), profile)
            content, checksum = raw[:-4], raw[-4:]
            if crc32(header + content) == int.from_bytes(checksum, "big"):
                ratio = (energy.max(axis=0) + 1e-12) / (energy.min(axis=0) + 1e-12)
                quality = float(np.clip(np.median(10 * np.log10(ratio)), 0, 60))
                packets.append((Packet(kind, profile, transfer, index, total, content), quality))
                self.samples = self.samples[end:]
            else:
                self.crc_errors += 1
                if kind is Kind.DATA:
                    self.rejections.append(Packet(Kind.NACK, Profile.BALANCED, transfer, index, total, b""))
                self.samples = self.samples[Profile.BALANCED.samples:]
            self.pending = None
        return packets

    def find_header(self):
        width = Profile.BALANCED.samples
        symbols = len(SYNC) + HEADER.size * 8
        step = 48
        candidates = np.arange(0, len(self.samples) - symbols * width + 1, step)
        if not len(candidates):
            return False
        grid = np.arange(0, candidates[-1] + (symbols - 1) * width + 1, step)
        energy = tone_energy(self.samples, grid, width)
        indices = candidates[:, None] // step + np.arange(len(SYNC)) * (width // step)
        bits = decisions(energy)
        matches = bits[indices] == SYNC
        valid = np.flatnonzero((matches[:, :32].sum(axis=1) >= 30) & matches[:, 32:].all(axis=1))
        # The correctly centered preamble maximizes coherent energy in its expected tones.
        # A normalized ratio can favor a weak off-center window and misalign Fast symbols.
        preamble_energy = energy[1] - energy[0]
        scores = preamble_energy[indices[valid]] @ (2 * SYNC.astype(float) - 1)
        for candidate in valid[np.argsort(-scores)]:
            start = candidates[candidate]
            offsets = start // step + np.arange(len(SYNC), symbols) * (width // step)
            header = np.packbits(bits[offsets]).tobytes()
            flags, length, transfer, index, total, checksum = HEADER.unpack(header)
            if flags not in PACKET_TYPES or crc_hqx(header[:-2], 0xFFFF) != checksum:
                continue
            kind, profile = PACKET_TYPES[flags]
            if not 0 <= index < total <= MAX_CHUNKS:
                continue
            valid_length = {
                Kind.ACK: length == 1, Kind.DATA: 1 <= length <= CHUNK,
                Kind.HAVE: length == BITMAP_BYTES, Kind.NACK: length == 0,
                Kind.PING: length == 0, Kind.QUERY: length == 0, Kind.LOCKED: length == 0,
            }
            if not valid_length[kind]:
                continue
            self.samples = self.samples[start:]
            self.pending = kind, profile, length, header, transfer, index, total
            return True
        self.samples = self.samples[candidates[-1] + step:]
        return False

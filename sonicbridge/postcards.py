"""Build a standalone phone player for single-frame SonicBridge postcards."""

from base64 import b64encode
from json import dumps
from pathlib import Path
from secrets import randbits, token_hex

from .link import Message
from .modem import RATE, Kind, Packet, Profile, packet_bits, tones

POSTCARD_NAME = "sb-card"
POSTCARDS = (
    {"id": "P01", "title": "Pacific Coast", "scene": "coast"},
    {"id": "P02", "title": "Desert Dusk", "scene": "dunes"},
    {"id": "P03", "title": "Alpine Dawn", "scene": "alpine"},
)
PLAYS_PER_CARD = 12


def phone_page():
    profile = Profile.BALANCED
    cards = []
    for card in POSTCARDS:
        payload = Message(POSTCARD_NAME, card["id"].encode()).pack()
        frames = []
        for _ in range(PLAYS_PER_CARD):
            packet = Packet(Kind.DATA, profile, randbits(64), 0, 1, payload)
            prefix, body = packet_bits(packet)
            frames.append("".join(map(str, prefix)) + "".join(map(str, body)))
        cards.append({**card, "frames": frames})
    player = {
        "id": token_hex(8),
        "cards": cards,
        "sampleRate": RATE,
        "symbolSamples": profile.samples,
        "tones": b64encode(tones(profile.samples).astype("<f4").tobytes()).decode(),
    }
    template = Path(__file__).with_name("postcard-phone.html").read_text(encoding="utf-8")
    artwork = Path(__file__).with_name("postcard-art.css").read_text(encoding="utf-8")
    return template.replace("/* POSTCARD_ART */", artwork).replace("/* POSTCARD_DATA */", dumps(player))

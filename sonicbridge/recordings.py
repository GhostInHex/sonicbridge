"""A repeatable WAV sample and its streaming decode path."""

from functools import lru_cache
from io import BytesIO
import wave

import numpy as np

from .link import Message, Receiver, check_cancel
from .modem import RATE, Decoder, Kind, Packet, Profile, encode


@lru_cache(maxsize=1)
def sample_wav():
    message = Message("", b"Hello from a saved WAV.")
    packet = Packet(Kind.DATA, Profile.BALANCED, 1, 0, 1, message.pack())
    signal = np.concatenate((np.zeros(RATE // 4), encode(packet), np.zeros(RATE // 20)))
    pcm = np.round(signal * 32767).astype("<i2")

    output = BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
        wav.writeframes(pcm.tobytes())
    return output.getvalue()


def decode_sample(cancel, observe, report):
    decoder = Decoder()
    messages = []
    receiver = Receiver(report, lambda message, _encrypted: messages.append(message))
    packets = 0

    with wave.open(BytesIO(sample_wav()), "rb") as wav:
        seconds = wav.getnframes() / RATE
        while block := wav.readframes(RATE // 10):
            check_cancel(cancel)
            samples = np.frombuffer(block, dtype="<i2").astype(np.float32) / 32768
            observe(samples)
            previous_errors = decoder.crc_errors
            decoded = decoder.feed(samples)
            for _ in range(decoder.crc_errors - previous_errors):
                report("crc", "Recorded packet rejected · CRC mismatch")
            for packet, quality in decoded:
                packets += 1
                receiver.receive(packet, quality)

    if not messages:
        raise RuntimeError("No complete message decoded from the sample WAV")
    message = messages[0]
    return {
        "text": message.data.decode(),
        "size": len(message.data),
        "packets": packets,
        "crc_failures": decoder.crc_errors,
        "seconds": seconds,
    }

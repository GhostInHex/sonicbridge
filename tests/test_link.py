import unittest
from threading import Event

import numpy as np

from sonicbridge.link import Message, Receiver, Simulation, send_message
from sonicbridge.modem import Decoder, Kind, Packet, Profile, RATE, encode, tones


def streamed(samples):
    decoder = Decoder()
    decoded = []
    rejections = []
    for offset in range(0, len(samples), RATE // 10):
        decoded.extend(decoder.feed(samples[offset:offset + RATE // 10]))
        rejections.extend(decoder.rejections)
    return decoded, decoder.crc_errors, rejections


class AcousticLinkTests(unittest.TestCase):
    def test_streaming_packets_at_arbitrary_offset_with_noise(self):
        random = np.random.default_rng(11)
        packets = [Packet(Kind.DATA, profile, 41, number, 2, bytes(range(64)))
                   for number, profile in enumerate((Profile.BALANCED, Profile.ROBUST))]
        waveform = np.concatenate([np.zeros(3149), encode(packets[0]), np.zeros(2083),
                                   encode(packets[1]), np.zeros(1600)])
        waveform += random.normal(0, 0.10, len(waveform))
        decoded, errors, _ = streamed(waveform)
        self.assertEqual([packet for packet, _ in decoded], packets)
        self.assertEqual(errors, 0)

    def test_crc_rejects_bad_packet_and_recovers_next(self):
        packet = Packet(Kind.DATA, Profile.BALANCED, 41, 0, 1, b"hello")
        signal = encode(packet)
        width = packet.profile.samples
        bad = signal.copy()
        zero, one = tones(width)
        bad[-width:] = one if np.array_equal(bad[-width:], zero) else zero
        packets, errors, rejections = streamed(np.r_[bad, np.zeros(2100), signal, np.zeros(1500)])
        self.assertEqual([item for item, _ in packets], [packet])
        self.assertEqual(errors, 1)
        nack = Packet(Kind.NACK, Profile.BALANCED, packet.transfer, packet.index, packet.total, b"")
        self.assertEqual(rejections, [nack])
        replies, _, _ = streamed(np.r_[encode(nack), np.zeros(1500)])
        self.assertEqual([item for item, _ in replies], [nack])

    def test_robust_corrects_damaged_symbol(self):
        packet = Packet(Kind.DATA, Profile.ROBUST, 41, 0, 1, b"hello")
        signal = encode(packet)
        width = packet.profile.samples
        zero, one = tones(width)
        signal[-width:] = one if np.array_equal(signal[-width:], zero) else zero
        decoded, _, _ = streamed(np.r_[signal, np.zeros(1500)])
        self.assertEqual([item for item, _ in decoded], [packet])

    def test_retries_and_file_reassembly_use_real_codec(self):
        cancel = Event()
        events = []
        inbox = []
        report = lambda kind, text: events.append((kind, text))
        message = Message("example.bin", bytes(range(200)))
        receiver = Receiver(report, lambda message, _: inbox.append(message), drop_ack=2)
        transport = Simulation(receiver, cancel, lambda _: None, report, noise=.03)
        send_message(message, "adaptive", transport.exchange, cancel, report, lambda *_: None)
        self.assertEqual(inbox, [message])
        self.assertEqual(sum(kind == "duplicate" for kind, _ in events), 2)
        self.assertEqual(sum(kind == "timeout" for kind, _ in events), 2)
        self.assertTrue(any(kind == "profile" and "Robust" in detail for kind, detail in events))

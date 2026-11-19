from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest

from sonicbridge.compare import compare
from sonicbridge.link import Cancelled, Message, Receiver, Simulation, send_payload
from sonicbridge.modem import CHUNK, Kind, Packet, Profile, encode
from sonicbridge.seal import LockedMessage, seal, unseal
from sonicbridge.storage import Store


def ignore(*_):
    pass


class ExtendedLinkTests(unittest.TestCase):
    def test_probe_crosses_channel_without_delivering_a_message(self):
        inbox = []
        channel = Simulation(Receiver(ignore, inbox.append), Event(), ignore, ignore, .03, paced=False)
        result = channel.exchange(Packet(Kind.PING, Profile.BALANCED, 41, 0, 1, b""))
        self.assertEqual(result.kind, Kind.ACK)
        self.assertGreater(result.payload[0], 0)
        self.assertEqual(inbox, [])

    def test_fast_frames_and_replies_are_shorter(self):
        channel = Simulation(Receiver(ignore, ignore), Event(), ignore, ignore, .03, paced=False)
        fast = Packet(Kind.DATA, Profile.FAST, 42, 0, 1, Message("", b"hello through sound").pack())
        balanced = Packet(Kind.DATA, Profile.BALANCED, 42, 0, 1, fast.payload)
        self.assertLess(len(encode(fast)), len(encode(balanced)))
        reply = channel.exchange(fast)
        self.assertEqual((reply.kind, reply.profile), (Kind.ACK, Profile.FAST))

    def test_ciphertext_rejects_wrong_passphrase_tamper_and_wrong_transfer(self):
        phrase = "a sufficiently long passphrase"
        payload = Message("name.txt", b"confidential payload").pack()
        ciphertext = seal(payload, phrase, 55)
        self.assertNotIn(b"name.txt", ciphertext)
        self.assertNotIn(b"confidential payload", ciphertext)
        self.assertNotEqual(ciphertext, seal(payload, phrase, 55))
        self.assertEqual(unseal(ciphertext, phrase, 55), payload)
        for candidate, key, transfer in (
            (ciphertext, "wrong passphrase", 55),
            (ciphertext[:-1] + bytes((ciphertext[-1] ^ 1,)), phrase, 55),
            (ciphertext, phrase, 56),
        ):
            with self.assertRaises(LockedMessage):
                unseal(candidate, key, transfer)

    def test_encrypted_resume_after_restart_only_sends_missing_chunks(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "station.sqlite3"
            store = Store(path)
            message = Message("demo.bin", bytes(range(135)))
            phrase = "an example shared passphrase"
            transfer = 123
            wire = seal(message.pack(), phrase, transfer)
            store.save_pending(transfer, len(message.data), wire, {"transport": "simulation"})
            cancel = Event()
            events, inbox = [], []
            report = lambda kind, text: events.append((kind, text))
            receiver = Receiver(report, lambda *result: inbox.append(result), passphrase=phrase, store=store)
            channel = Simulation(receiver, cancel, ignore, report, .03, paced=False)

            def pause(_profile, complete, _total, _quality=None):
                if complete == 1:
                    cancel.set()

            with self.assertRaises(Cancelled):
                send_payload(wire, len(message.data), transfer, "balanced", channel.exchange, cancel, report, pause)
            self.assertEqual(inbox, [])
            self.assertEqual(store.pending()[:3], (transfer, len(message.data), wire))

            reopened = Store(path)
            receiver = Receiver(report, lambda *result: inbox.append(result), passphrase=phrase, store=reopened)
            cancel.clear()
            events.clear()
            channel = Simulation(receiver, cancel, ignore, report, .03, paced=False)
            send_payload(wire, len(message.data), transfer, "fast", channel.exchange, cancel, report, ignore, resume=True)
            self.assertEqual(inbox, [(message, True)])
            self.assertEqual(reopened.messages(), [("demo.bin", message.data, 1)])
            sent = [event for kind, event in events if kind == "tx"]
            total = (len(wire) + CHUNK - 1) // CHUNK
            self.assertEqual({line.split()[1] for line in sent}, {f"{i}/{total}" for i in range(2, total + 1)})
            events.clear()
            send_payload(wire, len(message.data), transfer, "fast", channel.exchange, cancel, report, ignore, resume=True)
            self.assertFalse(any(kind == "tx" for kind, _ in events))
            self.assertEqual(inbox, [(message, True)])

    def test_wrong_receiver_passphrase_cannot_complete_delivery(self):
        with TemporaryDirectory() as temp:
            store = Store(Path(temp) / "station.sqlite3")
            message = Message("", b"secret")
            transfer = 7
            wire = seal(message.pack(), "sender passphrase", transfer)
            receiver = Receiver(ignore, ignore, passphrase="incorrect passphrase", store=store)
            channel = Simulation(receiver, Event(), ignore, ignore, .03, paced=False)
            with self.assertRaisesRegex(RuntimeError, "could not unlock"):
                send_payload(wire, len(message.data), transfer, "balanced", channel.exchange, Event(), ignore, ignore)
            self.assertEqual(store.messages(), [])
            receiver.passphrase = "sender passphrase"
            send_payload(wire, len(message.data), transfer, "balanced", channel.exchange, Event(), ignore, ignore, resume=True)
            self.assertEqual(store.messages(), [("", b"secret", 1)])

    def test_comparison_measures_four_profiles_including_fast(self):
        rows = []
        compare("quiet", Event(), ignore, ignore, rows.append)
        self.assertEqual({row["mode"] for row in rows}, {"balanced", "robust", "adaptive", "fast"})
        self.assertTrue(all(row["delivered"] for row in rows))
        results = {row["mode"]: row["goodput"] for row in rows}
        self.assertGreater(results["fast"], results["balanced"])

    def test_interference_exposes_the_robustness_tradeoff(self):
        rows = []
        compare("interference", Event(), ignore, ignore, rows.append)
        profiles = {row["mode"]: row for row in rows}
        self.assertTrue(profiles["adaptive"]["acknowledged"])
        self.assertTrue(profiles["robust"]["acknowledged"])
        self.assertFalse(profiles["balanced"]["delivered"])
        self.assertFalse(profiles["fast"]["delivered"])
        self.assertGreater(profiles["adaptive"]["crc_failures"], 0)

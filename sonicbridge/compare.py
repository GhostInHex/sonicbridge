"""Repeatable simulated trials using the production modem and sender."""
from collections import Counter

from .link import Message, Receiver, Simulation, send_message

SCENARIOS = {
    "quiet": (0.03, 0, 0, 0),
    "interference": (0.22, 0.28, 60, 2),
}


def compare(scenario, cancel, observe, report, publish):
    noise, echo, burst_ms, drops = SCENARIOS[scenario]
    message = Message("", b"Sound can carry a message through interference. " * 3)
    for mode in ("balanced", "robust", "adaptive", "fast"):
        counts = Counter()
        received = []

        def count(kind, _message):
            counts[kind] += 1

        receiver = Receiver(count, lambda payload, _: received.append(payload), drops)
        channel = Simulation(receiver, cancel, observe, count, noise, echo=echo, burst_ms=burst_ms, paced=False)
        report("info", f"Testing {mode.title()} on the {scenario} channel")
        try:
            send_message(message, mode, channel.exchange, cancel, count, lambda *_: None)
            acknowledged = True
        except RuntimeError:
            acknowledged = False
        delivered = received == [message]
        publish({
            "mode": mode, "scenario": scenario, "attempts": counts["tx"], "retries": counts["retry"],
            "crc_failures": counts["crc"], "packets_received": counts["rx"], "delivered": delivered,
            "acknowledged": acknowledged, "seconds": round(channel.seconds, 3),
            "goodput": round(len(message.data) * 8 / channel.seconds, 2) if delivered else 0,
        })

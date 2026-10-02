"""Audit event log for token-lifecycle and confirmation statistics.

The web app records every confirmation attempt (both the inline and the
redirect paths); the worker records lifecycle migrations. Reports aggregate
these events so the two confirmation-failure paths are counted separately.
"""

import json
import threading
from collections import Counter


class InMemoryEventLog:
    def __init__(self):
        self._events = []
        self._lock = threading.Lock()

    def record(self, event_type, **fields):
        event = {"type": event_type}
        event.update(fields)
        with self._lock:
            self._events.append(event)
        return event

    def events(self):
        with self._lock:
            return list(self._events)


class JsonlEventLog(InMemoryEventLog):
    """Append-only JSON Lines log; safe for repeated CLI worker runs."""

    def __init__(self, path):
        super().__init__()
        self._path = path
        try:
            with open(path, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if line:
                        self._events.append(json.loads(line))
        except FileNotFoundError:
            pass

    def record(self, event_type, **fields):
        event = super().record(event_type, **fields)
        with open(self._path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, default=str) + "\n")
        return event


# Event types
ISSUED = "token_issued"
REDEEMED = "token_redeemed"
EXPIRED = "token_expired"
RESIDUE_CLEARED = "confirmed_residue_cleared"
CONFIRM_ATTEMPT = "confirm_attempt"


def aggregate(events):
    """Return counters keyed for the HTML report."""
    lifecycle = Counter()
    attempts = Counter()
    attempts_by_path = Counter()
    attempts_by_reason = Counter()
    reason_paths = Counter()
    for event in events:
        etype = event.get("type")
        if etype in (ISSUED, REDEEMED, EXPIRED, RESIDUE_CLEARED):
            lifecycle[etype] += 1
        elif etype == CONFIRM_ATTEMPT:
            outcome = event.get("outcome", "unknown")
            path = event.get("path", "unknown")
            attempts[outcome] += 1
            attempts_by_path[path] += 1
            attempts_by_reason[outcome] += 1
            reason_paths[f"{outcome}|{path}"] += 1
    return {
        "lifecycle": dict(lifecycle),
        "attempts": dict(attempts),
        "attempts_by_path": dict(attempts_by_path),
        "attempts_by_reason": dict(attempts_by_reason),
        "reason_paths": dict(reason_paths),
        "total": len(events),
    }

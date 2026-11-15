from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import os
import threading
import time

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None

@dataclass(frozen=True)
class Entry:
    seq: int
    ts: int
    event_type: str
    intent_id: str
    data: dict
    prev_hash: str
    hash: str

class IntentLog:
    def __init__(self, path=None):
        self.path = path
        self._entries = []
        self._lock = threading.RLock()
        if path:
            self._load()

    @staticmethod
    def _digest(prev_hash, payload):
        raw = prev_hash + json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()

    @staticmethod
    def _copy(entry):
        return Entry(entry.seq, entry.ts, entry.event_type, entry.intent_id,
                     deepcopy(entry.data), entry.prev_hash, entry.hash)

    def _load(self):
        try:
            with open(self.path, "rb") as source:
                lines = source.readlines()
        except FileNotFoundError:
            return
        for index, raw in enumerate(lines):
            try:
                value = json.loads(raw.decode("utf-8"))
                entry = Entry(**value)
                if not isinstance(entry.data, dict):
                    raise TypeError("entry data is not an object")
                self._entries.append(entry)
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
                if index == len(lines) - 1 and not raw.endswith(b"\n"):
                    break
                raise ValueError("intent log contains a corrupt record") from exc
        if not self.verify():
            raise ValueError("intent log hash chain verification failed")

    def append(self, event_type, intent_id, data):
        return self.append_many([(event_type, intent_id, data)])[0]

    def append_many(self, records):
        with self._lock:
            entries = []
            prev = self._entries[-1].hash if self._entries else "0" * 64
            now = int(time.time())
            for offset, (event_type, intent_id, data) in enumerate(records, 1):
                payload = {"seq": len(self._entries) + offset, "ts": now,
                           "event_type": event_type, "intent_id": intent_id,
                           "data": deepcopy(data)}
                entry = Entry(**payload, prev_hash=prev,
                              hash=self._digest(prev, payload))
                entries.append(entry)
                prev = entry.hash
            if self.path and entries:
                encoded = "".join(json.dumps(entry.__dict__, sort_keys=True) + "\n"
                                  for entry in entries)
                with open(self.path, "a", encoding="utf-8") as out:
                    if fcntl:
                        fcntl.flock(out.fileno(), fcntl.LOCK_EX)
                    try:
                        out.write(encoded)
                        out.flush()
                        os.fsync(out.fileno())
                    finally:
                        if fcntl:
                            fcntl.flock(out.fileno(), fcntl.LOCK_UN)
            self._entries.extend(entries)
            return tuple(self._copy(entry) for entry in entries)

    def entries(self, intent_id=None):
        with self._lock:
            return tuple(self._copy(entry) for entry in self._entries
                         if intent_id is None or entry.intent_id == intent_id)

    def verify(self):
        prev = "0" * 64
        for expected, entry in enumerate(self._entries, 1):
            payload = {key: getattr(entry, key) for key in
                       ("seq", "ts", "event_type", "intent_id", "data")}
            if (entry.seq != expected or entry.prev_hash != prev or
                    entry.hash != self._digest(prev, payload)):
                return False
            prev = entry.hash
        return True

    def replay_state(self, intent_id):
        mapping = {"PROPOSED": "PROPOSED", "HELD_ABUSE": "HELD_ABUSE",
                   "REVIEW_APPROVED": "PROPOSED", "REVIEW_REJECTED": "REJECTED",
                   "BATCH_APPROVED": "APPROVED", "SUBMIT_ATTEMPT": "SUBMITTING",
                   "SUBMIT_OK": "POSTED", "LOOKUP_FOUND": "POSTED",
                   "MARKED_UNCERTAIN": "UNCERTAIN", "RESOLVED_POSTED": "POSTED",
                   "RESOLVED_VOIDED": "VOIDED"}
        state = None
        for entry in self.entries(intent_id):
            if entry.event_type == "SUBMIT_ERROR":
                if entry.data.get("code") == "rejected":
                    state = "FAILED"
            else:
                state = mapping.get(entry.event_type, state)
        return state

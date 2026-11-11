"""Single-writer JSON snapshots with a flushed, replayable change journal."""
from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import uuid


ROOTS = ("requests", "subscriptions", "inventory", "downloads", "operations")


class StoreCorruption(RuntimeError):
    pass


def _encode(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _checksum(value: dict) -> str:
    return hashlib.sha256(_encode({k: v for k, v in value.items() if k != "_checksum"})).hexdigest()


def _changes(old: dict, new: dict, prefix: tuple = ()) -> list[dict]:
    result = []
    for key in sorted(old.keys() | new.keys()):
        path = [*prefix, key]
        if key not in new:
            result.append({"path": path, "delete": True})
        elif key not in old:
            result.append({"path": path, "value": new[key]})
        elif isinstance(old[key], dict) and isinstance(new[key], dict):
            result.extend(_changes(old[key], new[key], tuple(path)))
        elif old[key] != new[key]:
            result.append({"path": path, "value": new[key]})
    return result


class Store:
    def __init__(self, path: str | Path, *, lock_timeout: float = 30):
        given = Path(path)
        self.path = given if given.suffix == ".json" else given / "state.json"
        self.root = self.path.parent
        self.root.mkdir(parents=True, exist_ok=True)
        self.journal_path = self.root / "events.jsonl"
        self.lock_path = self.root / "follow.lock"
        self.lock_timeout = lock_timeout
        self._mutex = threading.RLock()
        self._depth = 0
        self._handle = None
        self.state = self._empty()
        self._durable = copy.deepcopy(self.state)
        with self.locked():
            self._load()

    @staticmethod
    def _empty() -> dict:
        return {"schema_version": 1, "last_applied_seq": 0, **{key: {} for key in ROOTS}}

    @contextlib.contextmanager
    def locked(self):
        """An OS lock, reentrant for the same Store instance and thread."""
        with self._mutex:
            if self._depth:
                self._depth += 1
                try:
                    yield self
                finally:
                    self._depth -= 1
                return
            handle = self.lock_path.open("a+b")
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"\0")
                handle.flush()
            deadline = time.monotonic() + self.lock_timeout
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        handle.close()
                        raise TimeoutError("Another follow worker owns the state lock")
                    time.sleep(0.05)
            self._handle, self._depth = handle, 1
            try:
                # A Store created before another writer ran must not use stale state.
                self._load()
                yield self
            finally:
                self._depth = 0
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()
                self._handle = None

    def _atomic_write(self, path: Path, data: bytes) -> None:
        if path.is_symlink():
            raise ValueError("State files may not be symlinks")
        fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".pending", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            for attempt in range(21):
                try:
                    os.replace(temporary, path)
                    break
                except PermissionError:
                    if attempt == 20:
                        raise
                    time.sleep(0.05)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _snapshot(self) -> None:
        document = copy.deepcopy(self.state)
        document["_checksum"] = _checksum(document)
        self._atomic_write(self.path, _encode(document) + b"\n")

    def _load(self) -> None:
        state = self._empty()
        snapshot_bad = False
        if self.path.exists():
            try:
                document = json.loads(self.path.read_bytes())
                if document.get("_checksum") != _checksum(document):
                    raise ValueError("Invalid snapshot checksum")
                document.pop("_checksum", None)
                if any(not isinstance(document.get(key), dict) for key in ROOTS):
                    raise ValueError("Invalid state roots")
                state = document
            except (ValueError, TypeError, KeyError):
                snapshot_bad = True
        if self.journal_path.is_symlink():
            raise StoreCorruption("Journal may not be a symlink")
        raw = self.journal_path.read_bytes() if self.journal_path.exists() else b""
        records = raw.splitlines(keepends=True)
        offset, previous_seq = 0, 0
        for index, line in enumerate(records):
            try:
                if not line.endswith(b"\n"):
                    raise ValueError("Incomplete journal line")
                event = json.loads(line)
                if event.get("_checksum") != _checksum(event):
                    raise ValueError("Invalid journal checksum")
                seq = event["seq"]
                if not isinstance(seq, int) or seq <= previous_seq or (previous_seq and seq != previous_seq + 1):
                    raise ValueError("Non-contiguous journal")
                if not previous_seq and seq > state["last_applied_seq"] + 1:
                    raise StoreCorruption("Missing journal history")
                previous_seq = seq
                if seq > state["last_applied_seq"]:
                    for change in event["changes"]:
                        target = state
                        for key in change["path"][:-1]:
                            target = target.setdefault(key, {})
                        key = change["path"][-1]
                        if change.get("delete"):
                            target.pop(key, None)
                        else:
                            target[key] = copy.deepcopy(change["value"])
                    state["last_applied_seq"] = seq
            except (ValueError, TypeError, KeyError, IndexError) as error:
                if index != len(records) - 1:
                    raise StoreCorruption("Corrupt journal before its tail") from error
                self._atomic_write(self.root / ("events.corrupt-" + uuid.uuid4().hex + ".jsonl"), raw[offset:])
                self._atomic_write(self.journal_path, raw[:offset])
                break
            offset += len(line)
        if snapshot_bad and previous_seq == 0:
            raise StoreCorruption("Neither snapshot nor journal can recover state")
        if any(not isinstance(state.get(key), dict) for key in ROOTS):
            raise StoreCorruption("Replayed state has invalid roots")
        self.state = state
        self._durable = copy.deepcopy(state)
        if raw or snapshot_bad:
            self._snapshot()

    def event(self, kind: str, payload: dict) -> dict:
        if not self._depth:
            raise RuntimeError("Hold Store.locked() while changing state or emitting events")
        if not isinstance(payload, dict) or not isinstance(kind, str) or not kind:
            raise ValueError("Event kind and dict payload required")
        seq = self._durable["last_applied_seq"] + 1
        self.state["last_applied_seq"] = seq
        event = {"seq": seq, "kind": kind, "payload": copy.deepcopy(payload), "changes": _changes(self._durable, self.state)}
        event["_checksum"] = _checksum(event)
        with self.journal_path.open("ab") as handle:
            handle.write(_encode(event) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        self._durable = copy.deepcopy(self.state)
        self._snapshot()
        return event

    def save(self) -> None:
        self.event("checkpoint", {})

    def register_request(self, request: dict) -> bool:
        if not self._depth:
            raise RuntimeError("Hold Store.locked() to register a request")
        request_id = request.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("A stable request_id is required")
        digest = hashlib.sha256(_encode(request)).hexdigest()
        existing = self.state["requests"].get(request_id)
        if existing:
            if existing.get("payload_hash") != digest:
                raise ValueError("request_id already belongs to different content")
            return False
        self.state["requests"][request_id] = {"payload_hash": digest, "status": "accepted", "request": copy.deepcopy(request)}
        self.event("request_accepted", {"request_id": request_id, "payload_hash": digest})
        return True

"""Recoverable local publication switch; caller holds the publisher lock.

No method deletes the recovery copy. A separate authorized retention operation
may retire it only after the caller confirms media-server availability.
"""
from __future__ import annotations

import copy
import hashlib
import os
from pathlib import Path
import re
import stat
from typing import Callable

from .store import Store


class ReplacementConflict(RuntimeError):
    pass


def _safe_path(value: str | Path) -> Path:
    path = Path(os.path.abspath(value))
    for node in (path, *path.parents):
        try:
            info = node.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ReplacementConflict("Symlink/reparse paths cannot participate in a replacement")
    return path


def file_sha256(path: str | Path) -> str:
    path = _safe_path(path)
    if not path.is_file():
        raise ReplacementConflict("Expected a regular replacement file")
    result = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            result.update(chunk)
    return result.hexdigest()


def _matches(path: Path, digest: str | None) -> bool:
    return digest is not None and path.is_file() and file_sha256(path) == digest


class Replacement:
    def __init__(self, store: Store, operation_id: str):
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", operation_id) or operation_id in {".", ".."}:
            raise ValueError("Invalid stable replacement operation_id")
        self.store, self.operation_id = store, operation_id

    @property
    def operation(self) -> dict:
        try:
            return self.store.state["operations"][self.operation_id]
        except KeyError as error:
            raise ReplacementConflict("Replacement has not been prepared") from error

    def _record(self, phase: str, **updates) -> dict:
        self.operation.update(updates, phase=phase)
        self.store.event("replacement_" + phase, {"operation_id": self.operation_id, **updates})
        return copy.deepcopy(self.operation)

    def prepare(self, candidate: str | Path, target: str | Path, expected_old_sha256: str | None = None, metadata: dict | None = None) -> dict:
        if not self.store._depth:
            raise RuntimeError("Replacement requires Store.locked() and caller's publisher lock")
        candidate, target = _safe_path(candidate), _safe_path(target)
        if candidate == target:
            raise ReplacementConflict("Candidate and active target must be independent")
        metadata = dict(metadata or {})
        backup_root = _safe_path(metadata.get("backup_root", target.parent / "_follow_recovery" / self.operation_id))
        metadata["backup_root"] = str(backup_root)
        backup = backup_root / ("previous" + target.suffix)
        if backup in {candidate, target}:
            raise ReplacementConflict("Recovery path overlaps a publication path")
        existing = self.store.state["operations"].get(self.operation_id)
        if existing:
            if existing.get("kind") != "replacement" or (existing["candidate"], existing["target"], existing["backup"]) != (str(candidate), str(target), str(backup)):
                raise ReplacementConflict("operation_id belongs to another replacement")
            if candidate.exists() and not _matches(candidate, existing["candidate_sha256"]):
                raise ReplacementConflict("operation_id cannot be reused for a different candidate")
            if expected_old_sha256 is not None and expected_old_sha256 != existing["old_sha256"]:
                raise ReplacementConflict("operation_id cannot be reused for a different old version")
            return copy.deepcopy(existing)
        candidate_digest = file_sha256(candidate)
        old_digest = file_sha256(target) if target.exists() else None
        if old_digest is not None and (expected_old_sha256 is None or old_digest != expected_old_sha256):
            raise ReplacementConflict("Existing target requires its exact trusted managed SHA256")
        if old_digest is None and expected_old_sha256 is not None:
            raise ReplacementConflict("Expected managed old target is missing")
        target.parent.mkdir(parents=True, exist_ok=True)
        backup_root.mkdir(parents=True, exist_ok=True)
        if backup.exists():
            raise ReplacementConflict("Untracked recovery file already exists")
        if len({candidate.stat().st_dev, target.parent.stat().st_dev, backup_root.stat().st_dev}) != 1:
            raise ReplacementConflict("Replacement and recovery files must share a filesystem")
        self.store.state["operations"][self.operation_id] = {
            "kind": "replacement", "phase": "prepared", "candidate": str(candidate),
            "target": str(target), "backup": str(backup), "candidate_sha256": candidate_digest,
            "old_sha256": old_digest, "had_old": old_digest is not None, "metadata": metadata,
        }
        return self._record("prepared")

    def _paths(self) -> tuple[Path, Path, Path]:
        op = self.operation
        return tuple(_safe_path(op[key]) for key in ("candidate", "target", "backup"))

    def run(self, verify: Callable[[Path], object]) -> dict:
        if not callable(verify):
            raise ValueError("Synchronous verification callback is required")
        try:
            return self.recover(verify)
        except Exception:
            if self.operation["phase"] in {"backup_intent", "backed_up", "install_intent", "switched"}:
                self.rollback()
            raise

    def recover(self, verify: Callable[[Path], object] | None = None) -> dict:
        if not self.store._depth:
            raise RuntimeError("Hold the state and publisher locks before recovery")
        op = self.operation
        if op["phase"] in {"verified", "confirmed", "rolled_back"}:
            return copy.deepcopy(op)
        if op["phase"] == "rollback_intent":
            return self.rollback()
        candidate, target, backup = self._paths()
        old, new = op["old_sha256"], op["candidate_sha256"]
        if op["phase"] in {"prepared", "backup_intent"}:
            # Never move a current version without first validating its replacement.
            if verify is None:
                if backup.exists():
                    return self.rollback()
                return copy.deepcopy(op)
            if not _matches(candidate, new):
                raise ReplacementConflict("Candidate changed or disappeared")
            try:
                accepted = bool(verify(candidate))
            except Exception:
                if backup.exists():
                    self.rollback()
                raise
            if not accepted:
                if backup.exists():
                    return self.rollback()
                return self._record("rolled_back", verification="candidate_rejected")
            if op["had_old"]:
                if backup.exists():
                    if not _matches(backup, old) or target.exists():
                        raise ReplacementConflict("Ambiguous backup transition")
                else:
                    if not _matches(target, old):
                        raise ReplacementConflict("Active target changed before backup")
                    self._record("backup_intent")
                    os.replace(target, backup)
            elif target.exists():
                raise ReplacementConflict("A new unrelated target appeared")
            self._record("backed_up")
        if op["phase"] in {"backed_up", "install_intent"}:
            if op["had_old"] and not _matches(backup, old):
                raise ReplacementConflict("Managed recovery copy is missing or changed")
            if target.exists():
                if not _matches(target, new) or candidate.exists():
                    raise ReplacementConflict("Ambiguous installation transition")
            else:
                if not _matches(candidate, new):
                    raise ReplacementConflict("Candidate changed before installation")
                self._record("install_intent")
                os.replace(candidate, target)
            self._record("switched")
        if op["phase"] == "switched":
            if not _matches(target, new):
                raise ReplacementConflict("Installed target no longer matches this operation")
            if verify is None:
                return copy.deepcopy(op)
            try:
                accepted = bool(verify(target))
            except Exception:
                self.rollback()
                raise
            if not accepted:
                return self.rollback()
            if not _matches(target, new):
                raise ReplacementConflict("Installed target changed during verification")
            return self._record("verified")
        return copy.deepcopy(op)

    def rollback(self) -> dict:
        if not self.store._depth:
            raise RuntimeError("Hold state and publisher locks before rollback")
        op = self.operation
        if op["phase"] == "rolled_back":
            return copy.deepcopy(op)
        candidate, target, backup = self._paths()
        old, new = op["old_sha256"], op["candidate_sha256"]
        if op["had_old"] and not (_matches(backup, old) or _matches(target, old)):
            raise ReplacementConflict("Cannot roll back without the exact managed old version")
        self._record("rollback_intent")
        if target.exists() and not _matches(target, old):
            if not _matches(target, new):
                raise ReplacementConflict("Rollback refuses to move an unrelated target")
            if candidate.exists():
                raise ReplacementConflict("Rollback candidate path is occupied")
            os.replace(target, candidate)
        if op["had_old"]:
            if target.exists():
                if not _matches(target, old):
                    raise ReplacementConflict("Rollback target conflict")
            else:
                os.replace(backup, target)
        return self._record("rolled_back")

    def confirm(self) -> dict:
        """Caller has verified Jellyfin; keep backup for authorized later cleanup."""
        if self.operation["phase"] not in {"verified", "confirmed"}:
            raise ReplacementConflict("Only a verified switch can be confirmed")
        return self._record("confirmed")

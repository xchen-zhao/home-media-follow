"""Read-only source-work decisions; daily scans and network backoff are external."""
from __future__ import annotations

import math
import re

from .core import in_requested_range, logical_key, resolution_rank
from .local_policy import local_requested


_TERMINAL_OPERATIONS = {
    "confirmed", "complete", "completed", "cancelled", "canceled",
    "rejected", "failed", "aborted", "rolled_back", "terminal",
}
_TERMINAL_DOWNLOADS = {"complete", "cancelled", "rejected"}


def candidate_quarantine_holds(state, show_id) -> dict:
    """Only exact maintenance confirmation releases a candidate for new work."""
    return {
        operation["candidate_id"]: {
            "operation_id": operation.get("operation_id", operation_id),
            "phase": operation.get("phase", "unknown"),
        }
        for operation_id, operation in state.get("operations", {}).items()
        if (isinstance(operation, dict) and operation.get("kind") == "candidate_quarantine"
            and operation.get("show_id") == show_id and operation.get("phase") != "confirmed"
            and isinstance(operation.get("candidate_id"), str) and operation["candidate_id"])
    }


def _key(candidate):
    if not isinstance(candidate, dict):
        return None
    try:
        return logical_key(candidate)
    except (KeyError, TypeError, ValueError):
        return None


def operation_unresolved(operation):
    return (isinstance(operation, dict) and operation.get("terminal") is not True
            and not any(operation.get(field) in _TERMINAL_OPERATIONS for field in ("status", "phase", "stage")))


def pending_cloud_transfers(state):
    return {operation_id: operation for operation_id, operation in state.get("operations", {}).items()
            if operation_unresolved(operation) and operation.get("kind") == "cloud_transfer"}


def pending_cloud_keys(state, show_id) -> set:
    """Logical keys with unresolved durable cloud side effects, including history."""
    result = set()
    inventory = state.get("inventory", {})
    for operation in state.get("operations", {}).values():
        if not isinstance(operation, dict) or operation.get("show_id") != show_id:
            continue
        if not operation_unresolved(operation):
            continue
        if operation.get("kind") == "cloud_transfer":
            candidate = inventory.get(operation.get("candidate_id"))
            key = _key(candidate) if isinstance(candidate, dict) and candidate.get("show_id") == show_id else None
        elif operation.get("kind") == "cloud_replacement":
            key = operation.get("logical_key")
            if not isinstance(key, str) or not key.startswith(show_id + ":"):
                key = None
        else:
            continue
        if key:
            result.add(key)
    return result


def _probe_due(candidate, now):
    probe = candidate.get("probe", {})
    if not isinstance(probe, dict) or probe.get("status") not in {None, "unknown", "rejected"}:
        return False
    deadline = candidate.get("next_probe_at", 0)
    return (not isinstance(deadline, bool) and isinstance(deadline, (int, float))
            and math.isfinite(deadline) and deadline <= now)


def _explicit_8k(candidate):
    text = " ".join(str(candidate.get(field, "")) for field in ("name", "source_path"))
    return bool(re.search(r"(?<!\d)8K|4320P", text, re.I))


def source_work_due(state, sub, now) -> bool:
    """Whether a recovery tick needs source APIs; never grants download authority.

    Caller owns daily complete scans and provider backoff. Paused/cancelled
    subscriptions never start source work. Candidate checks use the latest scan
    when scan_candidates is present and always stay within activation scope.
    """
    if sub.get("status") in {"paused", "cancelled", "canceled"}:
        return False
    active = sub.get("activation")
    if sub.get("needs_activation") or not active:
        return True
    show_id = sub.get("show_id", active.get("show_id"))
    if not isinstance(show_id, str) or not show_id or active.get("show_id") != show_id:
        return False
    busy, rejected = set(), set()
    for job in state.get("downloads", {}).values():
        if not isinstance(job, dict) or job.get("show_id") != show_id:
            continue
        key = job.get("logical_key") or _key(job.get("candidate"))
        status = job.get("status")
        if status not in _TERMINAL_DOWNLOADS:
            busy.add(key)
        elif status == "rejected":
            cid = job.get("candidate", {}).get("candidate_id", job.get("candidate_id"))
            if cid:
                rejected.add(cid)
    # A switched cloud replacement can simply be awaiting local/Jellyfin
    # confirmation. Its in-flight job is reconciled without new source calls.
    if pending_cloud_keys(state, show_id) - busy:
        return True
    if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now):
        return False
    inventory = state.get("inventory", {})
    held = candidate_quarantine_holds(state, show_id)
    candidates = (inventory.get(cid) for cid in sub["scan_candidates"]) if "scan_candidates" in sub else inventory.values()
    assets = sub.get("assets", {})
    for candidate in candidates:
        if (not isinstance(candidate, dict) or candidate.get("record_type") not in {None, "candidate"}
                or candidate.get("show_id") != show_id or candidate.get("candidate_id") in rejected
                or candidate.get("candidate_id") in held):
            continue
        key = _key(candidate)
        if not key or key in busy:
            continue
        try:
            requested = local_requested(candidate, sub)
        except (KeyError, TypeError, ValueError):
            requested = False
        if not requested:
            continue
        current = assets.get(key)
        rank = resolution_rank(candidate)
        if not current:
            if rank or _probe_due(candidate, now):
                return True
            continue
        if not current.get("managed_local"):
            continue
        current_candidate = current.get("candidate", {})
        current_rank = resolution_rank(current_candidate)
        if not current_rank or candidate.get("candidate_id") == current_candidate.get("candidate_id"):
            continue
        if rank:
            if rank[0] > current_rank[0]:
                return True
        elif _probe_due(candidate, now):
            if current_rank[0] < 3 or (current_rank[0] == 3 and _explicit_8k(candidate)
                                       and candidate.get("probe", {}).get("status") in {None, "unknown"}):
                return True
    return False

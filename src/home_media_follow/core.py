"""Pure selection rules. Observing an asset never creates a download request."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePosixPath
from typing import Any


def _positive_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("Expected a positive integer")
    return value


def logical_key(candidate: dict) -> str:
    show = candidate["show_id"]
    if not isinstance(show, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", show):
        raise ValueError("show_id must be stable ASCII")
    season = _positive_int(candidate.get("season", 1))
    episode = _positive_int(candidate["episode"])
    part = candidate.get("part") or ""
    if part not in ("", "上", "下"):
        raise ValueError("Unknown episode part")
    return f"{show}:S{season:02d}:E{episode:04d}:{part}"


def parse_episode(name: str, media_kind: str) -> dict | None:
    """Parse conservative episode forms; never infer an episode from a year."""
    if media_kind not in {"anime", "tv", "variety"} or not isinstance(name, str):
        return None
    text = name.replace("\\", "/").rsplit("/", 1)[-1]
    if re.search(r"预告|花絮|彩蛋|先导|剧场版|导演剪辑|加更|样片|合集|合辑|(?<![A-Za-z])SP\d*\b", text, re.I):
        return None
    if re.search(r"(?:E\d+|\d+)\s*[-~至]\s*(?:E)?\d+(?!\d|[KkPp])", text, re.I):
        return None
    season_matches = list(re.finditer(r"(?<![A-Za-z0-9])S(\d{1,3})E(\d{1,4})(?!\d)", text, re.I))
    if season_matches:
        if len(season_matches) != 1 or re.search(r"E\d+E\d+", text, re.I):
            return None
        season, episode = map(int, season_matches[0].groups())
    else:
        cn = list(re.finditer(r"第\s*(\d{1,4})\s*[集期话]", text))
        if cn:
            if len(cn) != 1:
                return None
            season, episode = 1, int(cn[0].group(1))
        else:
            match = re.match(r"^\s*(\d{1,4})(?=$|[._\s-])", text)
            if not match:
                return None
            season, episode = 1, int(match.group(1))
            if episode >= 1900:  # A leading release year is not an episode.
                return None
    if season < 1 or episode < 1:
        return None
    part = None
    if media_kind == "variety":
        parts = set(re.findall(r"(?:第\s*\d+\s*期\s*|[\s._(（\[-])([上下])(?:集|篇|半场)?(?=$|[\s._)）\]-])", text))
        if len(parts) > 1:
            return None
        part = next(iter(parts), None)
        if part is None and re.search(r"第\s*\d+\s*期[^0-9]{0,4}[上下]", text):
            return None  # An unrecognized split must not collapse two published parts.
    return {"season": season, "episode": episode, "part": part}


def parse_source_episode(show: dict, row: dict, root: str) -> dict | None:
    """Bind episodes to an explicitly registered season and source directories."""
    parsed = parse_episode(row['name'], show['media_kind'])
    if not parsed:
        return None
    scope = show.get('source_scope')
    if scope is not None:
        season = _positive_int(scope['season'])
        parents = scope.get('allowed_parent_paths')
        if (show['media_kind'] != 'variety' or show.get('season') != season
                or not isinstance(parents, list) or not parents
                or any(not isinstance(p, str) or not p or PurePosixPath(p).is_absolute()
                       or '..' in PurePosixPath(p).parts for p in parents)):
            raise ValueError('Invalid registered source season scope')
        path = PurePosixPath(row['path'])
        if '..' in path.parts:
            return None
        try:
            parent = path.relative_to(PurePosixPath(root)).parent.as_posix()
        except ValueError:
            return None
        if parent not in parents:
            return None
        if re.search(r'(?<![A-Za-z0-9])S\d{1,3}E\d{1,4}', row['name'], re.I) and parsed['season'] != season:
            return None
        parsed['season'] = season
    elif show.get('episode_mode') == 'absolute':
        parsed['season'] = show.get('season', 1)
    return parsed


def candidate_in_source_scope(show: dict, candidate: dict, root: str) -> bool:
    if show.get('source_scope') is None:
        return True
    parsed = parse_source_episode(show, {'name': candidate['name'], 'path': candidate['source_path']}, root)
    return parsed is not None and all(parsed.get(k) == candidate.get(k) for k in ('season', 'episode', 'part'))


def resolution_rank(candidate: dict | None) -> tuple[int, int, int, int] | None:
    """Tier, real pixel area, long edge, short edge; labels are not evidence."""
    if not isinstance(candidate, dict):
        return None
    probe = candidate.get("probe", {})
    if not isinstance(probe, dict) or probe.get("status") != "verified":
        return None
    width, height = probe.get("width"), probe.get("height")
    if any(isinstance(v, bool) or not isinstance(v, int) or v < 1 for v in (width, height)):
        return None
    long, short = max(width, height), min(width, height)
    if long < 1920 or long > short * 3:
        return None
    tier = 4 if long >= 7680 else 3 if long >= 3840 else 2 if long >= 2560 else 1
    return tier, width * height, long, short


def candidate_identity(candidate: dict) -> str:
    if candidate.get("candidate_id"):
        return str(candidate["candidate_id"])
    values = {key: candidate.get(key) for key in ("show_id", "season", "episode", "part", "source_id", "source_path", "size", "provider_fingerprint")}
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def choose_candidate(candidates: list[dict], current: dict | None = None) -> dict | None:
    keys = {logical_key(item) for item in candidates}
    if len(keys) > 1:
        raise ValueError("Select candidates for one logical episode at a time")
    if current is not None and keys and logical_key(current) not in keys:
        raise ValueError("Current asset belongs to a different logical episode")
    verified = [(resolution_rank(item), item) for item in candidates]
    verified = [(rank, item) for rank, item in verified if rank is not None]
    current_rank = resolution_rank(current)
    if not verified:
        return current if current_rank is not None else None
    best_rank = max(rank for rank, _ in verified)
    if current_rank is not None and current_rank[0] >= best_rank[0]:
        return current
    finalists = [item for rank, item in verified if rank == best_rank]
    return min(finalists, key=lambda item: (int(item.get("source_priority", 100)), str(item.get("source_id", "")), candidate_identity(item)))


def needs_upgrade(current: dict, candidate: dict) -> bool:
    # An upgrade must never cross episode ownership or downgrade a trusted asset.
    if logical_key(current) != logical_key(candidate):
        return False
    old, new = resolution_rank(current), resolution_rank(candidate)
    return old is not None and new is not None and new[0] > old[0]


def activation(candidates: list[dict], scan_id: str) -> dict:
    if not isinstance(scan_id, str) or not scan_id:
        raise ValueError("Activation requires a complete scan_id")
    if not candidates:
        raise ValueError("Cannot establish a watermark from an empty scan")
    keys = [(logical_key(item), item) for item in candidates]
    shows = {item["show_id"] for _, item in keys}
    if len(shows) != 1:
        raise ValueError("Activation is per show")
    season, episode = max((item.get("season", 1), item["episode"]) for _, item in keys)
    selected = sorted({key for key, item in keys if (item.get("season", 1), item["episode"]) == (season, episode)})
    return {"show_id": next(iter(shows)), "scan_id": scan_id, "watermark": {"season": season, "episode": episode}, "initial_selected": selected}


def in_requested_range(candidate: dict, active: dict) -> bool:
    logical_key(candidate)  # Reject malformed identity instead of broadening scope.
    if candidate["show_id"] != active.get("show_id") or not active.get("scan_id"):
        return False
    watermark = active.get("watermark", {})
    if not isinstance(watermark, dict):
        return False
    lower = (_positive_int(watermark.get("season")), _positive_int(watermark.get("episode")))
    return (candidate.get("season", 1), candidate["episode"]) >= lower

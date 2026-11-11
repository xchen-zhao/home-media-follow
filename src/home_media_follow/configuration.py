"""Explicit public configuration; credentials belong in the local DPAPI store."""
from __future__ import annotations
import json
import ntpath
import re
from pathlib import Path
from urllib.parse import urlsplit

class ConfigurationError(ValueError):
    pass

def template(root):
    root = Path(root).absolute()
    return {
        "schema_version": 1, "policy_revision": 3, "catalog_mode": "static",
        "cloud_root": "/HomeMediaFollow", "openlist_mount": "/Baidu",
        "scan_timezone": "Asia/Shanghai", "scan_interval_seconds": 3600,
        "recovery_interval_seconds": 900, "quality_retry_seconds": 900,
        "quality_retry_max_seconds": 86400, "min_free_bytes": 10 * 1024**3,
        "media_root": str(root / "media"), "staging": str(root / "staging"),
        "recovery_root": str(root / "recovery"),
        "publisher_lock": str(root / "state" / "publisher.lock"),
        "services": {"openlist": "http://127.0.0.1:5244", "aria2": "http://127.0.0.1:6800", "jellyfin": "http://127.0.0.1:8096"},
        "shows": [],
    }

def validate(document):
    if not isinstance(document, dict) or type(document.get("schema_version")) is not int or document.get("schema_version") != 1 or type(document.get("policy_revision")) is not int or document.get("policy_revision") != 3:
        raise ConfigurationError("unsupported_config_version")
    if document.get("scan_timezone") != "Asia/Shanghai":
        raise ConfigurationError("unsupported_scan_timezone")
    if document.get("catalog_mode", "static") not in {"static", "yinxiang"}:
        raise ConfigurationError("invalid_catalog_mode")
    for key in ("cloud_root", "openlist_mount"):
        value = document.get(key)
        if not isinstance(value, str) or not value.startswith("/") or value == "/" or "\\" in value or any(p in {".", "..", ""} for p in value[1:].split("/")):
            raise ConfigurationError("invalid_cloud_path")
    for key in ("media_root", "staging", "recovery_root", "publisher_lock"):
        value = document.get(key)
        if not isinstance(value, str) or not (Path(value).is_absolute() or ntpath.isabs(value)):
            raise ConfigurationError("absolute_path_required")
    for key in ("scan_interval_seconds", "recovery_interval_seconds", "quality_retry_seconds", "quality_retry_max_seconds"):
        value = document.get(key)
        if type(value) is not int or value <= 0:
            raise ConfigurationError("positive_interval_required")
    if not 900 <= document["scan_interval_seconds"] <= 86400:
        raise ConfigurationError("scan_interval_outside_supported_range")
    if type(document.get("min_free_bytes")) is not int or document["min_free_bytes"] < 0:
        raise ConfigurationError("invalid_free_space_limit")
    services = document.get("services")
    if not isinstance(services, dict) or set(services) != {"openlist", "aria2", "jellyfin"}:
        raise ConfigurationError("invalid_services")
    for value in services.values():
        try:
            parsed = urlsplit(value)
            if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"} or parsed.port is None:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise ConfigurationError("loopback_service_endpoint_required") from None
    shows = document.get("shows")
    if not isinstance(shows, list):
        raise ConfigurationError("invalid_shows")
    ids = set()
    for show in shows:
        if not isinstance(show, dict) or not isinstance(show.get("show_id"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", show["show_id"]) or show["show_id"] in ids:
            raise ConfigurationError("invalid_show_id")
        ids.add(show["show_id"])
        if show.get("media_kind") not in {"anime", "tv", "variety"} or type(show.get("season")) is not int or show["season"] < 1 or not isinstance(show.get("title"), str) or not show["title"].strip():
            raise ConfigurationError("invalid_show_metadata")
        title = show["title"]
        if re.search(r'[<>:"/\\|?*\x00-\x1f]', title) or title.endswith((' ', '.')) or re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", title, re.I):
            raise ConfigurationError("unsafe_show_title")
        if not isinstance(show.get("cloud_path"), str) or not show["cloud_path"].startswith(document["cloud_root"] + "/"):
            raise ConfigurationError("show_outside_cloud_root")
        source = show.get("source")
        if not isinstance(source, dict) or not source.get("short_url") or not source.get("source_root_id") or not source.get("source_root"):
            raise ConfigurationError("incomplete_source_binding")
        if not isinstance(source["short_url"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{4,22}", source["short_url"]):
            raise ConfigurationError("canonical_share_reference_required")
        if not isinstance(source["source_root_id"], str) or not re.fullmatch(r"[0-9]{1,32}", source["source_root_id"]):
            raise ConfigurationError("numeric_source_root_id_required")
        if not isinstance(source["source_root"], str) or not source["source_root"].startswith("/") or "\\" in source["source_root"] or any(v in {".", "..", ""} for v in source["source_root"][1:].split("/")):
            raise ConfigurationError("invalid_source_root")
    def walk(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if re.fullmatch(r"password|passwd|pwd|cookie|token|api_key|bduss|stoken|extraction_code", str(key), re.I):
                    raise ConfigurationError("secrets_not_allowed_in_config")
                walk(child)
        elif isinstance(value, list):
            for child in value: walk(child)
    walk(document)
    return document

def load(root):
    try:
        path = Path(root) / "config" / "shows.json"
        if path.stat().st_size > 4 * 1024**2:
            raise ConfigurationError("config_too_large")
        return validate(json.loads(path.read_text(encoding="utf-8-sig")))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ConfigurationError("config_unavailable_or_invalid_json") from None

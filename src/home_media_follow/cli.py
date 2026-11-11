"""Local entry point. JSON diagnostics never include credential values."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import getpass
import json
import os
from pathlib import Path
import shutil
import sys
import uuid

from . import gateway
from .configuration import ConfigurationError, load, template

DEFAULT_ROOT = "C:/HomeMediaFollow" if os.name == "nt" else "./instance"
MARKER = ".home-media-follow-instance"


def _json(value):
    print(json.dumps(value, ensure_ascii=True, allow_nan=False))


def initialize(root):
    root = Path(root).absolute()
    # No implicit adoption of an existing installation or symlink ancestry.
    if any(p.is_symlink() or (p.exists() and getattr(p.lstat(), "st_file_attributes", 0) & 0x400) for p in [root, *root.parents]):
        raise ConfigurationError("symlink_instance_root_rejected")
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()):
        raise ConfigurationError("instance_root_must_be_empty")
    for name in ("config", "state", "requests", "reports", "secrets", "media", "staging", "recovery"):
        (root / name).mkdir()
    (root / "config" / "shows.json").write_text(json.dumps(template(root), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (root / MARKER).write_text("home-media-follow instance v1\n", encoding="utf-8")
    return {"ok": True, "status": "initialized", "root": str(root), "subscriptions_activated": False}


def doctor(root):
    config = load(root)
    ffprobe = config.get("ffprobe") or shutil.which("ffprobe")
    return {"ok": os.name == "nt" and sys.version_info >= (3, 13) and bool(ffprobe and Path(ffprobe).is_file()),
            "read_only": True, "windows": os.name == "nt", "python_supported": sys.version_info >= (3, 13),
            "config_valid": True, "shows": len(config["shows"]), "ffprobe_found": bool(ffprobe and Path(ffprobe).is_file()),
            "credentials_present": (Path(root) / "secrets" / "service.dpapi").is_file(),
            "notes": ["Presence checks do not verify credentials, external services, or playback."]}


def prepare(root, action, show_ids, destination):
    config = load(root)
    request = {"request_id": str(uuid.uuid4()), "action": action, "show_ids": show_ids,
               "policy_revision": 3, "created_at": datetime.now(timezone.utc).isoformat()}
    gateway.validate_request(request, [show["show_id"] for show in config["shows"]])
    # Exclusive creation keeps a retry from silently changing request identity.
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(request, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return {"ok": True, "request_id": request["request_id"], "request_file": str(destination), "submitted": False}


def authorize(root, service, show_id=None):
    if os.name != "nt" or not sys.stdin.isatty():
        raise ConfigurationError("authorization_requires_local_windows_terminal")
    config = load(root)
    from .secrets import SecretStore
    store = SecretStore(Path(root) / "secrets" / "service.dpapi")
    if service == "source":
        if show_id not in {s["show_id"] for s in config["shows"]}:
            raise ConfigurationError("unknown_show_id")
        codes = store.load().get("extraction_codes", {})
        codes[show_id] = getpass.getpass("Source extraction code (empty if none): ") or None
        store.update({"extraction_codes": codes})
    elif service == "jellyfin":
        from .service_auth import authorize_jellyfin
        username = getpass.getpass("Jellyfin administrator name: ")
        return {"ok": True, **authorize_jellyfin(username, getpass.getpass("Jellyfin password: "), store.update, config["services"]["jellyfin"])}
    if service == "openlist":
        values = {"username": input("OpenList username: "), "password": getpass.getpass("OpenList password: ")}
        if not all(values.values()): raise ConfigurationError("empty_credential_rejected")
        store.update({"openlist": values})
    elif service == "aria2":
        value = getpass.getpass("Aria2 RPC secret: ")
        if not value: raise ConfigurationError("empty_credential_rejected")
        store.update({"aria2": {"secret": value}})
    elif service == "baidu":
        from .baidu_auth import launch_edge_login
        from .secrets import protect_secret_directory
        login = Path(root).absolute() / "login"
        login.mkdir(parents=True, exist_ok=True)
        protect_secret_directory(login)
        def on_ready(info):
            _json({"status": "waiting_for_user_login", "screenshot_path": info["screenshot_path"]})
        result = launch_edge_login(lambda session: store.update({"baidu_session": session}),
            profile_dir=login / "profile", screenshot_path=login / "qr.png",
            on_ready=on_ready, headless=False)
        return {"ok": True, "service": "baidu", "status": result["status"],
                "requires_capability_validation": True,
                "note": "Dedicated login profile retained locally until service-account validation."}
    elif service == "baidu-cookie":
        # Advanced local import for a dedicated session; never accepted as arguments.
        value = getpass.getpass("Dedicated Baidu session cookie (local only): ")
        if not value: raise ConfigurationError("empty_credential_rejected")
        store.update({"baidu_session": value})
    return {"ok": True, "status": "saved_locally", "service": service, "validity_verified": False}


def inspect_source(root, show_id, destination):
    """Verify a source and save local binding proposals; do not register/activate."""
    if os.name != "nt" or not sys.stdin.isatty():
        raise ConfigurationError("source_inspection_requires_local_windows_terminal")
    gateway._ids([show_id], required=True)
    config = load(root)
    from .secrets import SecretStore
    from .baidu import BaiduAdapter
    store = SecretStore(Path(root) / "secrets" / "service.dpapi")
    reference = getpass.getpass("Source share URL or short reference (local only): ")
    code = getpass.getpass("Extraction code (empty if none): ") or None
    provider = BaiduAdapter(store.baidu_session, cloud_root=config["cloud_root"])
    context = provider.verify_share(reference, code)
    roots = provider.list_share(context, "/")
    bindings = [{"root_name": row["name"], "source": {
        "short_url": provider._short(reference), "source_root_id": row["id"],
        "source_root": row["path"], "extraction_secret_ref": show_id}}
        for row in roots if row["is_dir"]]
    if not bindings:
        raise ConfigurationError("source_requires_top_level_directory")
    proposal = {"show_id": show_id, "bindings": bindings, "registered": False,
                "note": "Select the intended source root and copy its source object into your show configuration."}
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(proposal, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    codes = store.load().get("extraction_codes", {})
    codes[show_id] = code
    store.update({"extraction_codes": codes})
    return {"ok": True, "source_verified": True, "root_candidates": len(bindings),
            "proposal_file": str(destination), "subscriptions_activated": False}


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--root", default=DEFAULT_ROOT, help="Isolated instance root; pass before the command")
    commands = result.add_subparsers(dest="command", required=True)
    for command in ("init", "check", "doctor"):
        commands.add_parser(command)
    status = commands.add_parser("status")
    status.add_argument("--show-id", action="append", default=[])
    status.add_argument("--request-id")
    run = commands.add_parser("run")
    run.add_argument("--force-scan", action="store_true")
    req = commands.add_parser("prepare")
    req.add_argument("--action", choices=sorted(gateway.ACTIONS), required=True)
    req.add_argument("--show-id", action="append", required=True)
    req.add_argument("--request-file", type=Path, required=True)
    submit = commands.add_parser("submit")
    submit.add_argument("--request-file", type=Path, required=True)
    inspect = commands.add_parser("inspect-source")
    inspect.add_argument("--show-id", required=True)
    inspect.add_argument("--output", type=Path, required=True)
    auth = commands.add_parser("authorize")
    auth.add_argument("service", choices=("baidu", "baidu-cookie", "openlist", "aria2", "jellyfin", "source"))
    auth.add_argument("--show-id")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "init": result = initialize(args.root)
        elif args.command == "check": result = {"ok": True, "config_valid": True, "shows": len(load(args.root)["shows"])}
        elif args.command == "doctor": result = doctor(args.root)
        elif args.command == "prepare": result = prepare(args.root, args.action, args.show_id, args.request_file)
        elif args.command == "submit":
            load(args.root)
            request = gateway._read_json(args.request_file, gateway.MAX_INPUT_BYTES)
            result = gateway.submit(args.root, request)
        elif args.command == "status":
            load(args.root)
            query = {"show_ids": args.show_id}
            if args.request_id: query["request_id"] = args.request_id
            result = gateway.status(args.root, query)
        elif args.command == "authorize": result = authorize(args.root, args.service, args.show_id)
        elif args.command == "inspect-source": result = inspect_source(args.root, args.show_id, args.output)
        else:
            load(args.root)
            from .worker import Worker
            result = {"ok": True, **Worker(args.root).run(args.force_scan)}
        _json(result)
        return 0 if result.get("ok", True) else 1
    except (ConfigurationError, gateway.GatewayError) as error:
        _json({"ok": False, "error_code": str(error)})
        return 2
    except Exception as error:
        _json({"ok": False, "error_code": "operation_failed", "error_type": type(error).__name__})
        return 3

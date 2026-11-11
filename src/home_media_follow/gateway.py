"""SSH JSON inbox and read-only status. Never imports or instantiates Store."""
from __future__ import annotations

import argparse
import contextlib
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import uuid

MAX_INPUT_BYTES = 65536
MAX_STATE_BYTES = 64 * 1024 * 1024
DEFAULT_ROOT = r"C:\HomeMediaFollow"
ACTIONS = {"activate_latest", "pause", "resume", "cancel"}
REQUEST_FIELDS = {"request_id", "action", "show_ids", "policy_revision", "created_at"}
STATE_ROOTS = ("requests", "subscriptions", "inventory", "downloads", "operations")
SHOW_FIELDS = {"show_id", "title", "media_kind", "episode_mode", "season"}
PRIVATE_KEY = re.compile(r"pass(?:word|wd)|^pwd$|api.?key|cookie|token|secret|authorization|bduss|stoken|spwd|url|extraction|credential", re.I)


class GatewayError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def encode(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _uuid(value):
    try:
        valid = isinstance(value, str) and str(uuid.UUID(value)) == value
    except (ValueError, AttributeError, TypeError):
        valid = False
    if not valid:
        raise GatewayError("invalid_request_id")
    return value


def _ids(value, *, required=False):
    if not isinstance(value, list) or len(value) > 100 or (required and not value):
        raise GatewayError("invalid_show_ids")
    if any(not isinstance(s, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", s) for s in value):
        raise GatewayError("invalid_show_ids")
    if len(set(value)) != len(value):
        raise GatewayError("duplicate_show_ids")
    return value


def validate_request(request, known_ids):
    if not isinstance(request, dict) or set(request) != REQUEST_FIELDS:
        raise GatewayError("invalid_request_fields")
    _uuid(request["request_id"])
    if not isinstance(request["action"], str) or request["action"] not in ACTIONS:
        raise GatewayError("unsupported_action")
    ids = _ids(request["show_ids"], required=True)
    if not set(ids) <= set(known_ids):
        raise GatewayError("unknown_show_id")
    if type(request["policy_revision"]) is not int or request["policy_revision"] != 3:
        raise GatewayError("unsupported_policy_revision")
    try:
        timestamp = request["created_at"]
        if not isinstance(timestamp, str) or len(timestamp) > 64:
            raise ValueError()
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
    except (ValueError, TypeError):
        raise GatewayError("invalid_created_at") from None
    return request


def _read_json(path, limit):
    with Path(path).open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise GatewayError("file_too_large")
    try:
        return json.loads(data)
    except (ValueError, UnicodeDecodeError):
        raise GatewayError("invalid_json_file") from None


def _shows(root):
    try:
        document = _read_json(Path(root) / "config" / "shows.json", 4 * 1024 * 1024)
        rows = document["shows"] if isinstance(document, dict) else None
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise GatewayError("invalid_show_config")
        result = {}
        for row in rows:
            identifier = row.get("show_id")
            _ids([identifier], required=True)
            if identifier in result:
                raise GatewayError("invalid_show_config")
            result[identifier] = row
        return result
    except FileNotFoundError:
        raise GatewayError("show_config_missing") from None
    except (KeyError, TypeError):
        raise GatewayError("invalid_show_config") from None


@contextlib.contextmanager
def _inbox_lock(path, timeout=10):
    with path.open("a+b") as handle:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + timeout
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
                    raise GatewayError("inbox_busy") from None
                time.sleep(0.05)
        try:
            yield
        finally:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _atomic_write(path, data):
    fd, temporary = tempfile.mkstemp(prefix=".request-", suffix=".pending", dir=path.parent)
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
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _not_link(path):
    try: info=path.lstat()
    except FileNotFoundError: return
    if path.is_symlink() or getattr(info,'st_file_attributes',0) & 0x400:
        raise GatewayError('invalid_inbox_path')


def _order_directory(inbox):
    directory=inbox/'.order'
    _not_link(directory)
    directory.mkdir(exist_ok=True)
    return directory


def _receipt(path, known_ids):
    _not_link(path)
    value=_read_json(path,MAX_INPUT_BYTES+1024)
    if not isinstance(value,dict) or set(value)!={'sequence','payload_hash','request','_checksum'}:
        raise GatewayError('invalid_acceptance_receipt')
    checksum=hashlib.sha256(encode({key:item for key,item in value.items() if key!='_checksum'})).hexdigest()
    if value['_checksum']!=checksum: raise GatewayError('acceptance_receipt_checksum_mismatch')
    if type(value['sequence']) is not int or not 0 < value['sequence'] < 2**63:
        raise GatewayError('invalid_acceptance_receipt')
    request=validate_request(value['request'],known_ids)
    if request['request_id']!=path.stem or hashlib.sha256(encode(request)).hexdigest()!=value['payload_hash']:
        raise GatewayError('invalid_acceptance_receipt')
    return value


def _legacy_sort_key(path,known_ids):
    _not_link(path)
    request=validate_request(_read_json(path,MAX_INPUT_BYTES),known_ids)
    if request['request_id']!=path.stem: raise GatewayError('request_filename_mismatch')
    created=datetime.fromisoformat(request['created_at'].replace('Z','+00:00')).timestamp()
    return (path.stat().st_mtime_ns,created,path.name)


def _legacy_acceptances(inbox,known_ids,*,initialize=False):
    """Freeze pre-ledger requests once; missing new receipts never become legacy."""
    directory=_order_directory(inbox);path=directory/'legacy.json';_not_link(path)
    if path.exists():
        value=_read_json(path,16*1024*1024)
        if not isinstance(value,dict) or set(value)!={'requests','_checksum'} or not isinstance(value['requests'],list):
            raise GatewayError('invalid_legacy_acceptance_baseline')
        if value['_checksum']!=hashlib.sha256(encode({'requests':value['requests']})).hexdigest():
            raise GatewayError('legacy_acceptance_checksum_mismatch')
        result={}
        for rank,item in enumerate(value['requests']):
            if not isinstance(item,dict) or set(item)!={'request_id','payload_hash'}:
                raise GatewayError('invalid_legacy_acceptance_baseline')
            identifier=_uuid(item['request_id'])
            if identifier in result or not isinstance(item['payload_hash'],str) or not re.fullmatch('[a-f0-9]{64}',item['payload_hash']):
                raise GatewayError('invalid_legacy_acceptance_baseline')
            result[identifier]={'rank':rank,'payload_hash':item['payload_hash']}
        return result
    if any(directory.glob('*.json')):
        raise GatewayError('acceptance_baseline_missing')
    if not initialize: return None
    rows=[]
    for target in sorted(inbox.glob('*.json'),key=lambda p:_legacy_sort_key(p,known_ids)):
        rows.append({'request_id':target.stem,'payload_hash':hashlib.sha256(encode(_read_json(target,MAX_INPUT_BYTES))).hexdigest()})
    value={'requests':rows};value['_checksum']=hashlib.sha256(encode(value)).hexdigest()
    _atomic_write(path,encode(value)+b'\n')
    return {item['request_id']:{'rank':rank,'payload_hash':item['payload_hash']} for rank,item in enumerate(rows)}


def _recover_acceptances(inbox, known_ids):
    """A durable receipt precedes exposure; recover crash gaps before new work."""
    directory=_order_directory(inbox)
    baseline=_legacy_acceptances(inbox,known_ids)
    receipts={};sequences=set()
    for path in directory.glob('*.json'):
        if path.name in {'sequence.json','legacy.json'}: continue
        value=_receipt(path,known_ids)
        if value['sequence'] in sequences: raise GatewayError('duplicate_acceptance_sequence')
        sequences.add(value['sequence']);receipts[path.stem]=value
    if baseline is not None:
        for identifier,legacy in baseline.items():
            target=inbox/(identifier+'.json');_not_link(target)
            if not target.is_file(): raise GatewayError('legacy_request_missing')
            if legacy['payload_hash']!=hashlib.sha256(encode(_read_json(target,MAX_INPUT_BYTES))).hexdigest():
                raise GatewayError('legacy_request_payload_mismatch')
            if identifier in receipts: raise GatewayError('duplicate_legacy_acceptance_identity')
        for target in inbox.glob('*.json'):
            _not_link(target)
            if target.stem in receipts: continue
            legacy=baseline.get(target.stem)
            if legacy is None: raise GatewayError('unregistered_inbox_request')
            if legacy['payload_hash']!=hashlib.sha256(encode(_read_json(target,MAX_INPUT_BYTES))).hexdigest():
                raise GatewayError('legacy_request_payload_mismatch')
    for identifier,value in sorted(receipts.items(),key=lambda item:item[1]['sequence']):
        target=inbox/(identifier+'.json');_not_link(target)
        raw=encode(value['request'])
        if target.exists():
            if encode(_read_json(target,MAX_INPUT_BYTES))!=raw:
                raise GatewayError('request_id_payload_mismatch')
        else:
            _atomic_write(target,raw+b'\n')
    return receipts


def _reserve_sequence(directory, receipts):
    counter=directory/'sequence.json';_not_link(counter)
    last=max((item['sequence'] for item in receipts.values()),default=0)
    if counter.exists():
        value=_read_json(counter,1024)
        if (not isinstance(value,dict) or set(value)!={'last_sequence'}
                or type(value['last_sequence']) is not int or not 0 <= value['last_sequence'] < 2**63-1):
            raise GatewayError('invalid_acceptance_counter')
        last=max(last,value['last_sequence'])
    if last>=2**63-2: raise GatewayError('acceptance_sequence_exhausted')
    sequence=last+1
    _atomic_write(counter,encode({'last_sequence':sequence})+b'\n')
    return sequence


def ordered_inbox(root, known_ids):
    """Snapshot ingress in durable acceptance order, independent of filesystem time.

    Legacy files predate the acceptance ledger. Their fallback is mtime, parsed
    created_at, then ID. New commands should always use submit, never file copying.
    """
    inbox=Path(root)/'requests';inbox.mkdir(parents=True,exist_ok=True)
    _not_link(inbox);_not_link(inbox/'.inbox.lock')
    with _inbox_lock(inbox/'.inbox.lock'):
        receipts=_recover_acceptances(inbox,known_ids)
        baseline=_legacy_acceptances(inbox,known_ids)
        def key(path):
            if path.stem in receipts: return (1,receipts[path.stem]['sequence'],0,path.name)
            if baseline is not None: return (0,baseline[path.stem]['rank'],0,path.name)
            return (0,*_legacy_sort_key(path,known_ids))
        return sorted(inbox.glob('*.json'),key=key)


def submit(root, request):
    """Durably order ingress before exposing immutable request files to workers."""
    raw=encode(request)
    if len(raw)>MAX_INPUT_BYTES: raise GatewayError('input_too_large')
    known_ids=_shows(root)
    validate_request(request,known_ids)
    inbox=Path(root)/'requests';inbox.mkdir(parents=True,exist_ok=True)
    _not_link(inbox);_not_link(inbox/'.inbox.lock')
    target=inbox/(request['request_id']+'.json')
    digest=hashlib.sha256(raw).hexdigest()
    with _inbox_lock(inbox/'.inbox.lock'):
        _not_link(target)
        _legacy_acceptances(inbox,known_ids,initialize=True)
        # Check the incoming payload before any receipt-driven repair occurs.
        if target.exists() and encode(_read_json(target,MAX_INPUT_BYTES))!=raw:
            raise GatewayError('request_id_payload_mismatch')
        receipt_path=_order_directory(inbox)/(request['request_id']+'.json')
        if receipt_path.exists() and _receipt(receipt_path,known_ids)['payload_hash']!=digest:
            raise GatewayError('request_id_payload_mismatch')
        duplicate=target.exists() or receipt_path.exists()
        receipts=_recover_acceptances(inbox,known_ids)
        if request['request_id'] not in receipts and not target.exists():
            sequence=_reserve_sequence(_order_directory(inbox),receipts)
            receipt={'sequence':sequence,'payload_hash':digest,'request':request}
            receipt['_checksum']=hashlib.sha256(encode(receipt)).hexdigest()
            _atomic_write(receipt_path,encode(receipt)+b'\n')
            _atomic_write(target,raw+b'\n')
        # Existing legacy requests retain legacy ordering; retries never reorder.
    return {'ok':True,'status':'queued','request_id':request['request_id'],
            'payload_hash':digest,'duplicate':duplicate,'business_state_changed':False}


def _public(value):
    if isinstance(value, dict):
        return {key: _public(child) for key, child in value.items()
                if isinstance(key, str) and not PRIVATE_KEY.search(key) and not key.startswith("_")}
    if isinstance(value, list):
        return [_public(item) for item in value]
    if isinstance(value, str):
        if re.search(r"(?:cookie|authorization|bduss|stoken|token|pass(?:word|wd))\s*[:=]", value, re.I):
            return "[redacted]"
        return re.sub(r"https?://[^\s<>\"']+", "[url omitted]", value)
    return value


def _state(root):
    try:
        state = _read_json(Path(root) / "state" / "state.json", MAX_STATE_BYTES)
    except FileNotFoundError:
        return None, "missing"
    except GatewayError:
        return None, "corrupt"
    if not isinstance(state, dict) or any(not isinstance(state.get(key), dict) for key in STATE_ROOTS):
        return None, "corrupt"
    if "_checksum" in state:
        checksum = hashlib.sha256(encode({key: value for key, value in state.items() if key != "_checksum"})).hexdigest()
        if state["_checksum"] != checksum:
            return None, "corrupt"
    return state, "ready" if "_checksum" in state else "unverified_snapshot"


def _matches(record, show_ids, request_id=None, key=None):
    if not isinstance(record, dict):
        return False
    if request_id is not None:
        ids = record.get("request_ids", [])
        if key != request_id and record.get("request_id") != request_id and request_id not in (ids if isinstance(ids, list) else []):
            return False
    if show_ids is not None:
        nested = record.get("request", {})
        nested = nested if isinstance(nested, dict) else {}
        identifiers = record.get("show_ids", nested.get("show_ids", []))
        identifiers = identifiers if isinstance(identifiers, list) else []
        identifier = record.get("show_id", key)
        if identifier not in show_ids and not set(identifiers) & show_ids:
            return False
    return True


def status(root, query=None):
    """Read files only. Missing/corrupt state is reported, never repaired here."""
    query = {} if query is None else query
    if not isinstance(query, dict) or set(query) - {"request_id", "show_ids"}:
        raise GatewayError("invalid_status_query")
    request_id = _uuid(query["request_id"]) if "request_id" in query else None
    selected = set(_ids(query.get("show_ids", []))) or None
    shows = _shows(root)
    if selected is not None and not selected <= shows.keys():
        raise GatewayError("unknown_show_id")
    state, state_status = _state(root)
    records = {key: {} for key in STATE_ROOTS} if state is None else state
    inbox = {}
    paths = [(Path(root) / "requests" / (request_id + ".json"))] if request_id else sorted((Path(root) / "requests").glob("*.json"))
    for path in paths:
        try:
            _uuid(path.stem)
            document = _read_json(path, MAX_INPUT_BYTES)
            validate_request(document, shows)
        except (GatewayError, FileNotFoundError):
            continue
        if selected is None or set(document["show_ids"]) & selected:
            inbox[path.stem] = document
    if request_id and selected is None:
        original = inbox.get(request_id) or records["requests"].get(request_id, {}).get("request", {})
        selected = set(original.get("show_ids", []))
    collections = {}
    for name in ("requests", "subscriptions", "downloads", "operations"):
        collections[name] = {key: _public(record) for key, record in records[name].items()
                             if _matches(record, selected, request_id if name != "subscriptions" else None, key)}
    observed = sum(1 for key, item in records["inventory"].items() if _matches(item, selected, key=key))
    return {"ok": state_status not in ("corrupt",), "read_only": True, "state_status": state_status,
            "shows": [_public({key: value for key, value in row.items() if key in SHOW_FIELDS})
                      for identifier, row in sorted(shows.items()) if selected is None or identifier in selected],
            "inbox": _public(inbox), "inventory": {"observed_count": observed}, **collections}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("submit", "status"))
    parser.add_argument("--root", default=DEFAULT_ROOT)
    args = parser.parse_args(argv)
    try:
        data = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(data) > MAX_INPUT_BYTES:
            raise GatewayError("input_too_large")
        try:
            payload = json.loads(data) if data.strip() else {}
        except (ValueError, UnicodeDecodeError):
            raise GatewayError("invalid_json_input") from None
        result = submit(args.root, payload) if args.operation == "submit" else status(args.root, payload)
        print(json.dumps(_public(result), ensure_ascii=True, allow_nan=False))
        return 0 if result.get("ok") else 1
    except GatewayError as error:
        print(json.dumps({"ok": False, "error_code": error.code}))
        return 2
    except (OSError, ValueError, TypeError):
        print(json.dumps({"ok": False, "error_code": "gateway_io_or_data_error"}))
        return 3


if __name__ == "__main__":
    raise SystemExit(main())

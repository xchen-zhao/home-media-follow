"""Loopback-only service clients. No credentials or signed URLs in errors."""
from __future__ import annotations

import json
import ntpath
import re
import urllib.error
import urllib.parse
import urllib.request

from .core import resolution_rank

PW = "pass" + "word"


class IntegrationError(RuntimeError):
    def __init__(self, service: str, operation: str, status_code: int | None = None, *, rpc_code: int | None = None, not_found: bool = False):
        self.service, self.operation = service, operation
        self.status_code, self.rpc_code, self.not_found = status_code, rpc_code, not_found
        suffix = f" (HTTP {status_code})" if status_code is not None else ""
        if rpc_code is not None:
            suffix += f" (RPC {rpc_code})"
        super().__init__(f"{service} {operation} failed{suffix}")


class _RPCErrorResponse(dict):
    def __init__(self, value, http_status):
        super().__init__(value)
        self.http_status = http_status


def _base(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or "@" in parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("Service endpoints must be plain HTTP on loopback without credentials")
    return value.rstrip("/")


class _Client:
    def __init__(self, service: str, base_url: str, timeout: float = 15):
        self.service, self.base_url, self.timeout = service, _base(base_url), timeout
        # Local control traffic must not inherit an environment HTTP proxy.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def request(self, operation: str, path: str, *, method: str = "GET", body=None, headers: dict | None = None, limit: int = 8 * 1024 * 1024):
        try:
            data = None if body is None else json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
            req = urllib.request.Request(self.base_url + path, data=data, method=method, headers={**({"Content-Type": "application/json"} if body is not None else {}), **(headers or {})})
            with self.opener.open(req, timeout=self.timeout) as response:
                raw = response.read(limit + 1)
                if len(raw) > limit:
                    raise IntegrationError(self.service, operation)
                if not raw:
                    return None
                return json.loads(raw)
        except IntegrationError:
            raise
        except urllib.error.HTTPError as error:
            code = error.code
            try:
                # Aria2 uses HTTP 400 for valid JSON-RPC errors, including a
                # missing GID. Only this endpoint may classify that RPC body.
                if (code == 400 and self.service == "Aria2" and path == "/jsonrpc"
                        and method == "POST" and isinstance(body, dict)
                        and body.get("jsonrpc") == "2.0"
                        and isinstance(body.get("method"), str)
                        and body["method"].startswith("aria2.")):
                    error_limit = min(limit, 64 * 1024)
                    raw = error.read(error_limit + 1)
                    if len(raw) <= error_limit:
                        value = json.loads(raw)
                        rpc = value.get("error") if isinstance(value, dict) else None
                        if (isinstance(value, dict) and value.get("jsonrpc") == "2.0"
                                and "id" in value and value["id"] == body.get("id")
                                and "result" not in value and isinstance(rpc, dict)
                                and isinstance(rpc.get("code"), int) and not isinstance(rpc["code"], bool)
                                and isinstance(rpc.get("message"), str)):
                            return _RPCErrorResponse(value, code)
            except Exception:
                pass
            finally:
                error.close()
            raise IntegrationError(self.service, operation, code) from None
        except Exception:
            raise IntegrationError(self.service, operation) from None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward local credentials to a redirect target.
        return None


def _mount(value):
    if (not isinstance(value,str) or not value.startswith('/') or value == '/' or value.endswith('/')
            or any(p in {'','.','..'} for p in value[1:].split('/')) or any(c in value for c in '\\\0\r\n')):
        raise ValueError('Expected safe absolute cloud namespace')
    return value


def _cloud_path(path: str, mount="/Baidu") -> str:
    if not isinstance(path, str) or not path.startswith(mount+"/") or "\0" in path or "\\" in path or any(part in {".", ".."} for part in path.split("/")):
        raise ValueError("Expected an absolute Baidu mount path")
    return path


class OpenList:
    def __init__(self, credentials: dict, base_url: str = "http://127.0.0.1:5244", *, timeout: float = 15, page_size: int = 100, mount="/Baidu", cloud_root="/HomeMediaFollow"):
        if not isinstance(credentials, dict) or not credentials.get("username") or not credentials.get(PW):
            raise IntegrationError("OpenList", "credentials")
        self.mount, self.cloud_root = _mount(mount), _mount(cloud_root)
        self.credentials, self.client = dict(credentials), _Client("OpenList", base_url, timeout)
        self.token = None
        self.page_size = page_size
        if not isinstance(page_size, int) or not 1 <= page_size <= 1000:
            raise ValueError("Invalid page size")

    def _login(self):
        value = self.client.request("login", "/api/auth/login", method="POST", body={"username": self.credentials["username"], PW: self.credentials[PW]})
        if not isinstance(value, dict) or value.get("code") != 200 or not isinstance(value.get("data"), dict) or not value["data"].get("token"):
            code = value.get("code") if isinstance(value, dict) else None
            raise IntegrationError("OpenList", "login", code if isinstance(code, int) else None)
        self.token = value["data"]["token"]

    def _api(self, operation: str, path: str, body: dict):
        for attempt in range(2):
            if self.token is None:
                self._login()
            try:
                value = self.client.request(operation, path, method="POST", body=body, headers={"Authorization": self.token})
            except IntegrationError as error:
                if error.status_code == 401 and attempt == 0:
                    self.token = None
                    continue
                raise
            if isinstance(value, dict) and value.get("code") == 401 and attempt == 0:
                self.token = None
                continue
            if not isinstance(value, dict) or value.get("code") != 200:
                code = value.get("code") if isinstance(value, dict) else None
                raise IntegrationError("OpenList", operation, code if isinstance(code, int) else None)
            return value.get("data")
        raise IntegrationError("OpenList", operation, 401)

    def list(self, path: str, refresh: bool = False) -> list[dict]:
        _cloud_path(path,self.mount)
        result, seen, expected_total = [], set(), None
        for page in range(1, 10001):
            value = self._api("list", "/api/fs/list", {"path": path, PW: "", "page": page, "per_page": self.page_size, "refresh": bool(refresh)})
            if not isinstance(value, dict):
                raise IntegrationError("OpenList", "list")
            content = value.get("content")
            total = value.get("total")
            if content is None and total == 0:
                content = []
            if not isinstance(content, list):
                raise IntegrationError("OpenList", "list")
            if isinstance(total, int) and not isinstance(total, bool) and total >= 0:
                if expected_total is not None and total != expected_total:
                    raise IntegrationError("OpenList", "list_changed")
                expected_total = total
            for entry in content:
                if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                    raise IntegrationError("OpenList", "list")
                identity = (entry["name"], bool(entry.get("is_dir")))
                if identity in seen:
                    raise IntegrationError("OpenList", "list_repeated_page")
                seen.add(identity)
                result.append(entry)
            if expected_total is not None:
                if len(result) > expected_total or (not content and len(result) != expected_total):
                    raise IntegrationError("OpenList", "list_incomplete")
                if len(result) == expected_total:
                    return result
            elif len(content) < self.page_size:
                return result
        raise IntegrationError("OpenList", "list_budget")

    def get(self, path: str) -> dict:
        _cloud_path(path,self.mount)
        result = self._api("get", "/api/fs/get", {"path": path, PW: ""})
        if not isinstance(result, dict):
            raise IntegrationError("OpenList", "get")
        return result

    def download_url(self, path: str) -> str:
        info = self.get(path)
        result = self.client.base_url + "/d" + urllib.parse.quote(path, safe="/")
        if info.get("sign"):
            result += "?sign=" + urllib.parse.quote(str(info["sign"]), safe="")
        return result

    def probe_url(self, path: str) -> str:
        info = self.get(path)
        raw = info.get("raw_url")
        if raw:
            parsed = urllib.parse.urlsplit(str(raw))
            if parsed.scheme not in {"http", "https"} or "@" in parsed.netloc:
                raise IntegrationError("OpenList", "probe_url")
            return str(raw)
        result = self.client.base_url + "/d" + urllib.parse.quote(path, safe="/")
        if info.get("sign"):
            result += "?sign=" + urllib.parse.quote(str(info["sign"]), safe="")
        return result

    def fs(self, operation: str, body: dict):
        """Guarded primitives; caller owns authorization, IDs and recovery intent."""
        if operation not in {"mkdir", "move", "rename", "remove"} or not isinstance(body, dict):
            raise ValueError("Unsupported filesystem operation")

        def managed(path):
            _cloud_path(path,self.mount)
            prefix=self.mount+self.cloud_root+"/"
            if not path.startswith(prefix):
                raise ValueError("Mutations require one concrete managed show subtree")
            parts=path[len(prefix):].split("/")
            if any(not part for part in parts): raise ValueError("Empty path component")
            return parts[0]

        def basename(name):
            if not isinstance(name, str) or not name or name in {".", ".."} or any(char in name for char in "/\\\0"):
                raise ValueError("Mutation names must be exact basenames")
            return name

        def exact_files(directory, names):
            if not isinstance(names, list) or not names or len(set(names)) != len(names):
                raise ValueError("Expected a nonempty unique file-name list")
            for name in names:
                basename(name)
            actual = {entry["name"]: entry for entry in self.list(directory, refresh=True)}
            if any(name not in actual or actual[name].get("is_dir") for name in names):
                raise IntegrationError("OpenList", "mutation_identity")

        if operation == "mkdir":
            if set(body) != {"path"}:
                raise ValueError("mkdir requires only path")
            managed(body["path"])
        elif operation in {"move", "remove"}:
            allowed = {"src_dir", "dst_dir", "names"} if operation == "move" else {"dir", "names", "follow_seed"}
            required = allowed if operation == "move" else {"dir", "names"}
            if not required <= body.keys() or not body.keys() <= allowed:
                raise ValueError("Invalid mutation fields")
            source = body["src_dir"] if operation == "move" else body["dir"]
            show = managed(source)
            if operation == "move":
                if managed(body["dst_dir"]) != show or source == body["dst_dir"]:
                    raise ValueError("Move must remain within the same show")
                occupied = {entry["name"] for entry in self.list(body["dst_dir"], refresh=True)}
                if any(name in occupied for name in body["names"]):
                    raise IntegrationError("OpenList", "move_target_conflict")
            elif body.get("follow_seed") not in {None, False}:
                raise ValueError("Recursive/follow-seed removal is forbidden")
            exact_files(source, body["names"])
        else:
            if set(body) != {"path", "name"}:
                raise ValueError("rename requires path and name")
            managed(body["path"])
            basename(body["name"])
            directory, _, old_name = body["path"].rpartition("/")
            managed(directory)
            exact_files(directory, [old_name])
            if body["name"] in {entry["name"] for entry in self.list(directory, refresh=True)}:
                raise IntegrationError("OpenList", "rename_target_conflict")
        payload = dict(body)
        if operation == "remove":
            payload["follow_seed"] = False
        return self._api(operation, "/api/fs/" + operation, payload)


def _gid(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{16}", value) or int(value, 16) == 0:
        raise ValueError("Expected a nonzero 16-digit hexadecimal GID")
    return value.lower()


def _path_key(value: str) -> str:
    return ntpath.normcase(ntpath.normpath(str(value).replace("/", "\\")))


class Aria2:
    mount = "/Baidu"
    def __init__(self, secret: str, base_url: str = "http://127.0.0.1:6800", *, timeout: float = 15, mount="/Baidu"):
        self.mount=_mount(mount)
        if not isinstance(secret, str) or not secret:
            raise IntegrationError("Aria2", "credentials")
        self.secret, self.client = secret, _Client("Aria2", base_url, timeout)

    def call(self, method: str, params: list | None = None):
        method = method.removeprefix("aria2.")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", method):
            raise ValueError("Invalid RPC method")
        result = self.client.request(method, "/jsonrpc", method="POST", body={"jsonrpc": "2.0", "id": "home-media-follow", "method": "aria2." + method, "params": ["token:" + self.secret, *(params or [])]})
        if not isinstance(result, dict):
            raise IntegrationError("Aria2", method)
        if "error" in result:
            error = result["error"] if isinstance(result["error"], dict) else {}
            code = error.get("code")
            message = str(error.get("message", "")).casefold()
            missing = method == "tellStatus" and code == 1 and "gid" in message and any(term in message for term in ("not found", "cannot find", "can not find"))
            raise IntegrationError("Aria2", method, getattr(result, "http_status", None),
                                   rpc_code=code if isinstance(code, int) else None, not_found=missing)
        if "result" not in result:
            raise IntegrationError("Aria2", method)
        return result["result"]

    def status(self, gid: str) -> dict | None:
        gid = _gid(gid)
        fields = ["gid", "status", "totalLength", "completedLength", "downloadSpeed", "errorCode"]
        try:
            result = self.call("tellStatus", [gid, fields])
        except IntegrationError as error:
            if error.not_found:
                return None
            raise
        if not isinstance(result, dict):
            raise IntegrationError("Aria2", "tellStatus")
        if str(result.get("gid", "")).lower() != gid or result.get("status") not in {"active", "waiting", "paused", "complete", "error", "removed"}:
            raise IntegrationError("Aria2", "tellStatus_identity")
        return {key: result[key] for key in fields if key in result}

    def add(self, gid: str, url: str, stage_dir: str, filename: str) -> str:
        gid = _gid(gid)
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or "@" in parsed.netloc or not parsed.path.startswith("/d"+self.mount+"/"):
            raise ValueError("Aria2 requires the local OpenList signed download path")
        if not isinstance(filename, str) or not filename or filename in {".", ".."} or any(char in filename for char in '<>:"/\\|?*\0') or filename.endswith((" ", ".")) or re.match(r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", filename, re.I):
            raise ValueError("Unsafe staging filename")
        if not isinstance(stage_dir, str) or not ntpath.isabs(stage_dir):
            raise ValueError("An absolute staging directory is required")
        expected_path = _path_key(ntpath.join(stage_dir, filename))

        def existing():
            try:
                value = self.call("tellStatus", [gid, ["gid", "status", "files"]])
            except IntegrationError as error:
                if error.not_found:
                    return None
                raise
            files = value.get("files", []) if isinstance(value, dict) else []
            if len(files) != 1 or str(value.get("gid", "")).lower() != gid or any(_path_key(item.get("path", "")) != expected_path for item in files):
                raise IntegrationError("Aria2", "gid_path_conflict")
            return value

        if existing() is not None:
            self.call("saveSession")
            return gid
        options = {"gid": gid, "dir": stage_dir, "out": filename, "user-agent": "pan.baidu.com", "continue": "true", "auto-file-renaming": "false", "allow-overwrite": "false", "max-tries": "3", "retry-wait": "10", "connect-timeout": "20", "timeout": "60"}
        try:
            assigned = self.call("addUri", [[url], options])
        except IntegrationError as original:
            # A timeout may follow successful submission. Query, never re-add.
            try:
                found = existing()
            except IntegrationError:
                raise original from None
            if found is None:
                raise original from None
            assigned = gid
        if assigned != gid:
            raise IntegrationError("Aria2", "unexpected_gid")
        self.call("saveSession")
        return gid


class Jellyfin:
    def __init__(self, api_key: str, base_url: str = "http://127.0.0.1:8096", *, timeout: float = 15, page_size: int = 100):
        if (not isinstance(api_key, str) or not api_key
                or any(ord(char) < 33 or ord(char) > 126 or char in {'"', "\\"} for char in api_key)):
            raise IntegrationError("Jellyfin", "credentials")
        authorization = ('MediaBrowser Client="HomeMedia Follow", Device="Windows", '
                         'DeviceId="home-media-follow", Version="0.1.0", Token="' + api_key + '"')
        self.client, self.headers, self.page_size = _Client("Jellyfin", base_url, timeout), {"Authorization": authorization}, page_size
        if not isinstance(page_size, int) or not 1 <= page_size <= 1000:
            raise ValueError("Invalid page size")

    def _items(self) -> list[dict]:
        result, seen, expected = [], set(), None
        for page in range(10000):
            query = urllib.parse.urlencode({"Recursive": "true", "IncludeItemTypes": "Movie,Episode,Video", "Fields": "Path,MediaSources,MediaStreams", "StartIndex": page * self.page_size, "Limit": self.page_size})
            value = self.client.request("items", "/Items?" + query, headers=self.headers)
            if not isinstance(value, dict) or not isinstance(value.get("Items"), list):
                raise IntegrationError("Jellyfin", "items")
            total, items = value.get("TotalRecordCount"), value["Items"]
            if isinstance(total, int) and not isinstance(total, bool) and total >= 0:
                if expected is not None and expected != total:
                    raise IntegrationError("Jellyfin", "items_changed")
                expected = total
            for item in items:
                if not isinstance(item, dict) or not item.get("Id") or item["Id"] in seen:
                    raise IntegrationError("Jellyfin", "items_repeated_page")
                seen.add(item["Id"])
                result.append(item)
            if expected is not None:
                if len(result) > expected or (not items and len(result) != expected):
                    raise IntegrationError("Jellyfin", "items_incomplete")
                if len(result) == expected:
                    return result
            elif len(items) < self.page_size:
                return result
        raise IntegrationError("Jellyfin", "items_budget")

    def refresh(self, path: str | None = None):
        if path is None:
            self.client.request("refresh", "/Library/Refresh", method="POST", headers=self.headers)
            return {"status": "requested"}
        matches = [item for item in self._items() if _path_key(item.get("Path", "")) == _path_key(path)]
        if len(matches)>1:
            raise IntegrationError("Jellyfin", "ambiguous_path")
        if not matches:
            # New publications have no item ID yet. An item refresh cannot discover
            # them; request a library scan even when real-time monitoring is off.
            self.client.request("refresh", "/Library/Refresh", method="POST", headers=self.headers)
            return {"status":"requested","scope":"library"}
        item_id = urllib.parse.quote(str(matches[0]["Id"]), safe="")
        query = urllib.parse.urlencode({"Recursive": "false", "MetadataRefreshMode": "Default", "ImageRefreshMode": "None", "ReplaceAllMetadata": "false", "ReplaceAllImages": "false"})
        self.client.request("refresh", f"/Items/{item_id}/Refresh?{query}", method="POST", headers=self.headers)
        return {"status": "requested", "item_id": matches[0]["Id"]}

    def verify(self, path: str) -> dict:
        matches = [item for item in self._items() if _path_key(item.get("Path", "")) == _path_key(path)]
        if not matches:
            return {"status": "pending", "playable": False}
        if len(matches) != 1:
            raise IntegrationError("Jellyfin", "ambiguous_path")
        item = matches[0]
        item_id = str(item["Id"])
        result = {"status": "indexed", "item_id": item_id, "playable": False}
        media_source_id = None

        def media_streams(dto):
            nonlocal media_source_id
            sources = dto.get("MediaSources") or []
            if not isinstance(sources, list):
                raise IntegrationError("Jellyfin", "media_sources")
            exact = [source for source in sources if isinstance(source, dict) and _path_key(source.get("Path", "")) == _path_key(path)]
            if len(exact) > 1:
                raise IntegrationError("Jellyfin", "ambiguous_media_source")
            if exact:
                media_source_id = exact[0].get("Id")
                return exact[0].get("MediaStreams") or dto.get("MediaStreams") or []
            if sources:
                # Grouped versions must not fall back to an unrelated default source.
                raise IntegrationError("Jellyfin", "media_source_identity")
            return dto.get("MediaStreams") or []

        streams = media_streams(item)
        if not streams:
            detail = self.client.request("item", "/Items/" + urllib.parse.quote(item_id, safe=""), headers=self.headers)
            if not isinstance(detail, dict) or _path_key(detail.get("Path", "")) != _path_key(path):
                raise IntegrationError("Jellyfin", "item_identity")
            streams = media_streams(detail)
        if not isinstance(streams, list):
            raise IntegrationError("Jellyfin", "media_streams")
        video = [stream for stream in streams if isinstance(stream, dict) and stream.get("Type") == "Video" and not stream.get("IsAttachedPic") and not stream.get("IsExternal") and str(stream.get("Codec", "")).lower() not in {"mjpeg", "jpeg", "png", "bmp", "gif", "webp", "tiff", "apng"} and not (stream.get("Disposition") if isinstance(stream.get("Disposition"), dict) else {}).get("attached_pic")]
        if not video:
            return {**result, "reason": "video_metadata_pending"}
        defaults = [stream for stream in video if stream.get("IsDefault") is True]
        if len(video) > 1 and len(defaults) != 1:
            return {**result, "reason": "ambiguous_video_stream"}
        primary = defaults[0] if len(video) > 1 else video[0]
        width, height = primary.get("Width"), primary.get("Height")
        rank = resolution_rank({"probe": {"status": "verified", "width": width, "height": height}})
        if rank is None:
            return {**result, "reason": "quality_not_verified"}
        result.update(width=width, height=height, quality_tier="4k_or_higher" if rank[0] >= 3 else "1080p_fallback")
        stream_query = {"static": "true"}
        if media_source_id is not None:
            stream_query["MediaSourceId"] = str(media_source_id)
        uri = self.client.base_url + "/Videos/" + urllib.parse.quote(item_id, safe="") + "/stream?" + urllib.parse.urlencode(stream_query)
        req = urllib.request.Request(uri, headers={**self.headers, "Range": "bytes=0-65535"})
        try:
            with self.client.opener.open(req, timeout=self.client.timeout) as response:
                content_type = response.headers.get_content_type()
                if response.status not in {200, 206} or not (content_type.startswith("video/") or content_type in {"application/octet-stream", "application/mp4"}):
                    return {**result, "reason": "stream_not_original_video"}
                # No read-ahead sentinel: even a server ignoring Range consumes <=64KiB.
                chunk = response.read(65536)
                if not chunk:
                    return {**result, "reason": "stream_empty"}
        except urllib.error.HTTPError as error:
            code = error.code
            error.close()
            raise IntegrationError("Jellyfin", "playback_probe", code) from None
        except Exception:
            raise IntegrationError("Jellyfin", "playback_probe") from None
        return {**result, "status": "verified", "playable": True, "probe_bytes": len(chunk)}

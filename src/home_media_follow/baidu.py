"""Native Windows-compatible Baidu web-session adapter (standard library).

Protocol construction is based on qjfoidnh/BaiduPCS-Go v4.0.2:
baidupcs/transfer.go, baidupcs/prepare.go, internal/pcscommand/transfer.go.
Protocol adapted/reimplemented in Python; see THIRD_PARTY_NOTICES.md for the
Apache-2.0 upstream attribution and full license.
This is a third-party web-interface adapter, NOT the official trial MCP/OAuth.
No implicit authentication, credential discovery, transfer retry or deletion.
"""
from __future__ import annotations

import hashlib
import http.cookiejar
from http.cookies import SimpleCookie
from datetime import timezone
from email.utils import parsedate_to_datetime
import json
import posixpath
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request


class BaiduError(RuntimeError):
    """Errors deliberately exclude remote bodies, URLs, cookies and passwords."""
    def __init__(self, message, *, code=None, retry_after=0):
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after


class AuthRequired(BaiduError):
    pass


class NetworkError(BaiduError):
    pass


class IncompleteListing(BaiduError):
    pass


class ConflictError(BaiduError):
    pass


class ShareError(BaiduError):
    pass


class TransferUnknown(BaiduError):
    def __init__(self, intent):
        super().__init__("Transfer outcome unknown; reconcile persisted intent before retry",
                         code=intent.get("cause_code"), retry_after=intent.get("retry_after", 0))
        self.intent = intent


NETDISK_UA = "netdisk;P2SP;3.0.0.8;netdisk;11.12.3;ANG-AN00;android-android;10.0;JSbridge4.4.0;jointBridge;1.1.0;"
APP_ID = "250528"
MAX_RESPONSE = 8 * 1024 * 1024


def _retry_after(value):
    """A delay only; never propagate provider header text into errors/state."""
    if not isinstance(value, str) or len(value) > 256:
        return 0
    value = value.strip()
    if re.fullmatch(r"[0-9]+", value):
        return int(value)
    try:
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return max(0, date.timestamp() - time.time())
    except (ValueError, TypeError, OverflowError):
        return 0


def _id(value):
    value = str(value)
    if not re.fullmatch(r"[0-9]{1,32}", value):
        raise BaiduError("Invalid provider file identifier")
    return value


def _path(value, *, target=False, allow_root=False, cloud_root="/HomeMediaFollow"):
    if not isinstance(value, str) or not value.startswith("/"):
        raise BaiduError("An absolute cloud path is required")
    if any(c in value for c in "\\\r\n\x00") or any(x in (".", "..") for x in value.split("/")):
        raise BaiduError("Invalid cloud path")
    if "//" in value:
        raise BaiduError("Ambiguous cloud path")
    value = value.rstrip("/") or "/"
    if target:
        if value == cloud_root and allow_root:
            return value
        if not value.startswith(cloud_root+"/"):
            raise BaiduError("Target must be inside the registered media cloud root")
    return value


def normalize_item(row):
    if not isinstance(row, dict):
        raise IncompleteListing("Malformed provider item")
    fid = _id(row.get("fs_id", row.get("fsid", row.get("id", ""))))
    path = _path(row.get("path", ""))
    name = row.get("server_filename", row.get("filename", row.get("name")))
    if not isinstance(name, str) or not name or name != posixpath.basename(path):
        raise IncompleteListing("Provider name and path disagree")
    if name in (".", "..") or any(c in name for c in "/\\\r\n\x00"):
        raise IncompleteListing("Unsafe provider filename")
    size = row.get("size", 0)
    if isinstance(size, bool) or not str(size).isdigit():
        raise IncompleteListing("Invalid provider size")
    is_dir = row.get("isdir", row.get("is_dir", 0))
    if is_dir not in (0, 1, "0", "1", False, True):
        raise IncompleteListing("Invalid provider directory flag")
    return {"id": fid, "name": name, "path": path, "size": int(size),
            "is_dir": str(int(is_dir)) == "1", "provider_fingerprint": row.get("md5", row.get("provider_fingerprint")),
            "mtime": row.get("server_mtime", row.get("mtime"))}


class _NoForeignRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        p = urllib.parse.urlsplit(newurl)
        if p.scheme != "https" or p.hostname not in ("pan.baidu.com", "passport.baidu.com", "wappass.baidu.com"):
            raise NetworkError("Unexpected provider redirect")
        if req.get_method() != "GET":
            raise NetworkError("Write redirect requires reconciliation")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class BaiduAdapter:
    def __init__(self, secret_loader, timeout=20, *, transport=None, max_pages=10000, intent_sink=None, cloud_root="/HomeMediaFollow"):
        self.cloud_root = _path(cloud_root)
        if self.cloud_root == "/": raise ValueError("Cloud root cannot be filesystem root")
        self.secret_loader = secret_loader
        self.timeout = timeout
        self.transport = transport  # deterministic fixture transport; never installed globally
        self.max_pages = max_pages
        self.intent_sink = intent_sink
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                                 urllib.request.HTTPCookieProcessor(self.jar), _NoForeignRedirect())
        self._session_hash = None
        self._token = None
        self._share_secrets = {}
        self._seen = {}
        self._last_pages = {}

    def _cookie(self, name, value, domain=".baidu.com"):
        self.jar.set_cookie(http.cookiejar.Cookie(
            version=0, name=name, value=value, port=None, port_specified=False,
            domain=domain, domain_specified=True, domain_initial_dot=domain.startswith("."),
            path="/", path_specified=True, secure=True, expires=None, discard=True,
            comment=None, comment_url=None, rest={"HttpOnly": None}, rfc2109=False))

    def _load_session(self):
        try:
            secret = self.secret_loader()
        except Exception:
            raise AuthRequired("Independent Windows session unavailable") from None
        if isinstance(secret, dict):
            secret = secret.get("cookie", secret.get("cookie_header", ""))
        if not isinstance(secret, str) or not secret or "\r" in secret or "\n" in secret:
            raise AuthRequired("Independent Windows session required")
        fingerprint = hashlib.sha256(secret.encode()).digest()
        if fingerprint != self._session_hash:
            parsed = SimpleCookie()
            try:
                parsed.load(secret)
            except Exception:
                raise AuthRequired("Independent session cookie invalid") from None
            if not parsed.get("BDUSS") or not parsed["BDUSS"].value:
                raise AuthRequired("Independent browser login required")
            self.jar.clear()
            for name, morsel in parsed.items():
                self._cookie(name, morsel.value)
            self._token = None
            self._share_secrets.clear()
            self._seen.clear()
            self._session_hash = fingerprint

    def _request(self, path, query=None, form=None, *, json_result=True, referer=None):
        if not path.startswith("/") or "?" in path or "#" in path or "//" in path:
            raise BaiduError("Invalid provider endpoint")
        url = "https://pan.baidu.com" + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = urllib.parse.urlencode(form).encode() if form is not None else None
        headers = {"User-Agent": NETDISK_UA, "Accept": "application/json,text/html;q=0.9",
                   "Referer": referer or "https://pan.baidu.com/disk/home"}
        if form is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            if self.transport is not None:
                result = self.transport(req, self.timeout)
                if isinstance(result, dict):
                    if not json_result:
                        raise BaiduError("Expected provider HTML")
                    return self._check_result(result)
                raw = result.encode() if isinstance(result, str) else result
            else:
                with self.opener.open(req, timeout=self.timeout) as response:
                    final = urllib.parse.urlsplit(response.geturl())
                    if final.hostname != "pan.baidu.com":
                        raise AuthRequired("Browser reauthorization required")
                    raw = response.read(MAX_RESPONSE + 1)
            if not isinstance(raw, bytes) or len(raw) > MAX_RESPONSE:
                raise IncompleteListing("Provider response exceeds bound")
            text = raw.decode("utf-8-sig")
            if not json_result:
                return text
            try:
                result = json.loads(text)
            except ValueError:
                if any(x in text.lower() for x in ("passport.baidu.com", "loginstate", "登录")):
                    raise AuthRequired("Browser reauthorization required") from None
                raise BaiduError("Provider returned invalid JSON") from None
            return self._check_result(result)
        except urllib.error.HTTPError as exc:
            code = exc.code
            retry_after = _retry_after(exc.headers.get("Retry-After")) if code == 429 and exc.headers else 0
            exc.close()
            if code in (401, 403):
                raise AuthRequired("Browser reauthorization or verification required", code=code) from None
            raise NetworkError("Provider HTTP request failed", code=code, retry_after=retry_after) from None
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError):
            raise NetworkError("Provider network request failed") from None
        except UnicodeError:
            raise BaiduError("Invalid provider response encoding") from None

    @staticmethod
    def _check_result(result):
        if not isinstance(result, dict):
            raise BaiduError("Provider response is not an object")
        code = result.get("errno", result.get("error_code"))
        if code is None or isinstance(code, bool):
            raise BaiduError("Provider result code missing")
        try:
            code = int(code)
        except (TypeError, ValueError):
            raise BaiduError("Provider result code invalid") from None
        if code in (-6, -5, 110, 111, 401, 403):
            raise AuthRequired("Browser reauthorization required", code=code)
        if code in (-9, 13003):
            raise ShareError("Share extraction code rejected", code=code)
        if code in (8001, 9019):
            raise AuthRequired("Provider interactive verification required", code=code)
        if code != 0:
            raise BaiduError("Provider operation rejected", code=code)
        return result

    def _account_token(self):
        self._load_session()
        if not self._token:
            value = self._request("/api/gettemplatevariable", {"clienttype": "0", "app_id": APP_ID,
                                  "fields": '["bdstoken"]'})
            token = value.get("result", {}).get("bdstoken")
            if not isinstance(token, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", token):
                raise AuthRequired("Independent authenticated session not confirmed")
            self._token = token
        return self._token

    def validate_session(self):
        """Read-only capability check; return no account identity or secret."""
        self._account_token()
        return {"status": "authenticated", "method": "independent_web_session", "refresh_supported": False}

    @staticmethod
    def _short(value):
        if not isinstance(value, str):
            raise ShareError("Invalid share reference")
        if value.startswith("https://"):
            p = urllib.parse.urlsplit(value)
            if p.hostname != "pan.baidu.com" or not p.path.startswith("/s/"):
                raise ShareError("Invalid share host or path")
            value = p.path.split("/s/", 1)[1]
            if not value.startswith("1"):
                raise ShareError("Invalid full share reference")
            value = value[1:]
        # Canonical internal form omits the leading /s/ '1'.
        if not re.fullmatch(r"[A-Za-z0-9_-]{4,22}", value):
            raise ShareError("Invalid short share reference")
        return value

    @staticmethod
    def _page_fields(text):
        # Decode a strict JSON object around loginstate. Never eval JavaScript.
        result = {}
        for match in re.finditer(r'"?loginstate"?\s*:', text):
            start = max(0, match.start() - 65536)
            positions = [m.start() + start for m in re.finditer(r"\{", text[start:match.start()])]
            for i in reversed(positions[-64:]):
                try:
                    value, _ = json.JSONDecoder().raw_decode(text[i:])
                except ValueError:
                    continue
                if isinstance(value, dict) and "loginstate" in value:
                    result = value
                    break
            if result:
                break
        if not result:
            for key in ("loginstate", "bdstoken", "shareid", "share_uk", "uk"):
                m = re.search(r'["\']?' + key + r'["\']?\s*:\s*(?:["\']([^"\']*)["\']|([0-9]+))', text)
                if m:
                    result[key] = m.group(1) if m.group(1) is not None else m.group(2)
        if str(result.get("loginstate", "")) == "0":
            raise AuthRequired("Browser session is not logged in")
        return result

    def verify_share(self, short_url, pwd=None):
        short = self._short(short_url)
        self._load_session()
        if pwd is not None and not re.fullmatch(r"[A-Za-z0-9]{4}", pwd):
            raise ShareError("Invalid share extraction code format")
        page = self._request("/s/1" + short, json_result=False)
        fields = self._page_fields(page)
        if pwd:
            # A protected link may redirect to /share/init before it reveals
            # shareid/share_uk. The short reference still identifies that share.
            params = {"surl": short, "clienttype": "1", "time": str(int(time.time() * 1000))}
            for key, field in (("shareid", "shareid"), ("uk", "share_uk")):
                if fields.get(field):
                    params[key] = _id(fields[field])
            answer = self._request("/share/verify", params,
                                   {"pwd": pwd, "vcode": "null", "vcode_str": "null",
                                    "bdstoken": fields.get("bdstoken") or self._account_token()},
                                   referer="https://pan.baidu.com/s/1" + short)
            randsk = answer.get("randsk")
            if randsk:
                # Responses use either raw or percent-encoded randsk. Keep one
                # decoded value internally and exactly one encoding in BDCLND.
                randsk = urllib.parse.unquote(str(randsk))
                self._cookie("BDCLND", urllib.parse.quote(randsk, safe=""), ".pan.baidu.com")
            page = self._request("/s/1" + short, json_result=False,
                                 referer="https://pan.baidu.com/share/init?surl=" + short)
            fields = self._page_fields(page)
        else:
            randsk = None
        sid = _id(fields.get("shareid", ""))
        owner = _id(fields.get("share_uk", ""))
        if not randsk:
            randsk = next((urllib.parse.unquote(c.value) for c in self.jar if c.name == "BDCLND"), None)
        token = fields.get("bdstoken")
        if not isinstance(token, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", token):
            token = self._account_token()
        self._share_secrets[short] = {"randsk": randsk, "share_id": sid, "share_uk": owner,
                                      "bdstoken": token}
        self._seen.setdefault(short, {})
        return {"short_url": short, "share_id": sid, "share_uk": owner}

    def _activate_context(self, context):
        self._load_session()
        short = self._short(context["short_url"])
        if short not in self._share_secrets:
            raise AuthRequired("Share must be verified in the current session")
        if any(str(context.get(k)) != self._share_secrets[short][k] for k in ("share_id", "share_uk")):
            raise ShareError("Share context differs from verified source identity")
        randsk = self._share_secrets[short].get("randsk")
        if randsk:
            self._cookie("BDCLND", urllib.parse.quote(str(randsk), safe=""), ".pan.baidu.com")
        else:
            # A passwordless context must not inherit another share's BDCLND.
            for cookie in list(self.jar):
                if cookie.name == "BDCLND":
                    self.jar.clear(cookie.domain, cookie.path, cookie.name)
        return short

    def list_share(self, context, dir="/", page=1, page_size=100):
        short = self._activate_context(context)
        directory = _path(dir or "/")
        if not isinstance(page, int) or not 1 <= page <= self.max_pages:
            raise BaiduError("Invalid share page")
        if not isinstance(page_size, int) or not 1 <= page_size <= 100:
            raise BaiduError("Invalid share page size")
        params = {"shorturl": short, "shareid": _id(context["share_id"]),
                  "uk": _id(context["share_uk"]), "root": "1" if directory == "/" else "0",
                  "page": str(page), "num": str(page_size), "order": "name", "desc": "0",
                  "web": "5", "app_id": APP_ID, "channel": "chunlei",
                  "bdstoken": self._share_secrets[short]["bdstoken"]}
        if directory != "/":
            params["dir"] = directory
        value = self._request("/share/list", params, referer="https://pan.baidu.com/s/1" + short)
        rows = self._rows(value)
        if len(rows) > page_size:
            raise IncompleteListing("Provider ignored page size")
        seen = self._seen[short]
        for row in rows:
            prior = seen.get(row["id"])
            if prior is not None and prior != row:
                raise IncompleteListing("Share changed during scan")
            if directory != "/" and posixpath.dirname(row["path"]) != directory:
                raise IncompleteListing("Share returned an item outside requested directory")
            seen[row["id"]] = row
        self._last_pages[(short, directory, page)] = value
        return rows

    @staticmethod
    def _rows(value):
        rows = value.get("list")
        if not isinstance(rows, list):
            raise IncompleteListing("Provider file list missing")
        result = [normalize_item(row) for row in rows]
        if len({r["id"] for r in result}) != len(result):
            raise IncompleteListing("Duplicate provider identifiers in page")
        return result

    def _all_pages(self, fetch, metadata, page_size=100):
        result, ids = [], set()
        expected_total = None
        for page in range(1, self.max_pages + 1):
            rows = fetch(page, page_size)
            info = metadata(page)
            total = info.get("total", info.get("total_count"))
            if total is not None:
                if isinstance(total, bool) or not str(total).isdigit():
                    raise IncompleteListing("Provider total invalid")
                total = int(total)
                if expected_total is not None and total != expected_total:
                    raise IncompleteListing("Provider total changed during scan")
                expected_total = total
            new_ids = {r["id"] for r in rows}
            if ids.intersection(new_ids):
                raise IncompleteListing("Provider pagination repeated identifiers")
            ids.update(new_ids)
            result.extend(rows)
            more = info.get("has_more", info.get("hasmore"))
            if more not in (None, 0, 1, False, True, "0", "1"):
                raise IncompleteListing("Provider pagination flag invalid")
            stop = more in (0, False, "0") if more is not None else len(rows) < page_size
            if not rows and more in (1, True, "1"):
                raise IncompleteListing("Provider claims more files after an empty page")
            if stop:
                if expected_total is not None and expected_total != len(result):
                    raise IncompleteListing("Provider count and complete scan disagree")
                return result
        raise IncompleteListing("Provider page limit reached")

    def walk_share(self, context, root="/", *, max_items=50000, max_depth=20):
        short = self._activate_context(context)
        self._seen[short] = {}  # a new complete scan must not reuse a stale scan
        stack = [(_path(root), 0)]
        visited = set()
        result = []
        while stack:
            directory, depth = stack.pop()
            if directory in visited or depth > max_depth:
                raise IncompleteListing("Share directory loop or depth limit")
            visited.add(directory)
            rows = self._all_pages(lambda p, n: self.list_share(context, directory, p, n),
                                   lambda p: self._last_pages[(short, directory, p)])
            for row in rows:
                if row["is_dir"]:
                    stack.append((row["path"], depth + 1))
                else:
                    result.append(row)
                if len(self._seen[short]) > max_items:
                    raise IncompleteListing("Share item budget exceeded")
        return result

    def list_target(self, path):
        directory = _path(path, target=True, allow_root=True, cloud_root=self.cloud_root)
        token = self._account_token()
        metadata = {}
        def fetch(page, count):
            value = self._request("/api/list", {"dir": directory, "page": str(page), "num": str(count),
                                  "order": "name", "desc": "0", "clienttype": "0", "bdstoken": token})
            rows = self._rows(value)
            if len(rows) > count or any(posixpath.dirname(row["path"]) != directory for row in rows):
                raise IncompleteListing("Target listing scope mismatch")
            metadata[page] = value
            return rows
        return self._all_pages(fetch, lambda p: metadata[p])

    def transfer_selected(self, context, ids, target):
        if not callable(self.intent_sink):
            raise BaiduError("A durable intent_sink is required before any transfer")
        target = _path(target, target=True, cloud_root=self.cloud_root)
        short = self._activate_context(context)
        ids = [_id(v) for v in ids]
        if not ids or len(ids) != len(set(ids)) or len(ids) > 100:
            raise BaiduError("Selection must contain 1 to 100 distinct files")
        seen = self._seen[short]
        if any(fid not in seen or seen[fid]["is_dir"] for fid in ids):
            raise BaiduError("Every selected file must first be enumerated in this session")
        selected = [dict(seen[fid]) for fid in ids]
        if len({r["name"] for r in selected}) != len(selected):
            raise ConflictError("Selected files collide at the target")
        before = self.list_target(target)
        names = {r["name"]: r for r in before}
        if any(row["name"] in names for row in selected):
            raise ConflictError("Target name already exists; reconcile inventory instead of transferring")
        intent = {"target": target, "files": selected, "before_ids": [r["id"] for r in before],
                  "short_url": short, "submitted_at": time.time(), "stage": "prepared"}
        intent["operation_id"] = hashlib.sha256(json.dumps(
            {"short_url": short, "ids": sorted(ids), "target": target,
             "files": selected}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        token = self._account_token()
        try:
            self.intent_sink(json.loads(json.dumps(intent)))
        except Exception:
            raise BaiduError("Transfer intent could not be durably recorded; no write attempted") from None
        try:
            value = self._request("/share/transfer", {"shareid": _id(context["share_id"]),
                                  "from": _id(context["share_uk"]), "bdstoken": token,
                                  "app_id": APP_ID, "channel": "chunlei", "clienttype": "0",
                                  "web": "1", "ondup": "fail"},
                                  {"fsidlist": json.dumps([int(fid) for fid in ids], separators=(",", ":")), "path": target},
                                  referer="https://pan.baidu.com/s/1" + short)
        except BaiduError as exc:
            intent["cause_code"] = exc.code
            intent["retry_after"] = exc.retry_after
            intent["auth_required"] = isinstance(exc, AuthRequired)
            intent["stage"] = "write_unknown"
            try:
                self.intent_sink(json.loads(json.dumps(intent)))
            except Exception:
                # The original pre-write intent is already durable. A failed
                # outcome update never permits replaying an uncertain mutation.
                pass
            raise TransferUnknown(intent) from None
        # A malformed success response may follow an executed write as well.
        # Baidu web responses expose either legacy info[].path/fsid or
        # extra.list[].to/to_fs_id. Both identify the destination, not the
        # source fs_id. Conflicting envelopes must remain unreconciled.
        envelopes = []
        if value.get("info") is not None:
            if not isinstance(value["info"], list):
                raise TransferUnknown(intent)
            if value["info"]:
                envelopes.append(value["info"])
        extra = value.get("extra")
        if isinstance(extra, dict) and extra.get("list") is not None:
            if not isinstance(extra["list"], list):
                raise TransferUnknown(intent)
            if extra["list"]:
                envelopes.append(extra["list"])
        if not envelopes:
            raise TransferUnknown(intent)
        expected_paths = {target + "/" + row["name"] for row in selected}
        receipts = []
        for rows in envelopes:
            items = []
            for row in rows:
                try:
                    code = int(row.get("errno", 0))
                    path = _path(row.get("to", row.get("path", "")), target=True, cloud_root=self.cloud_root)
                    fid = _id(row.get("to_fs_id", row.get("fsid", row.get("fs_id", ""))))
                except (ValueError, TypeError, AttributeError, BaiduError):
                    raise TransferUnknown(intent) from None
                if code != 0 or posixpath.dirname(path) != target:
                    raise TransferUnknown(intent)
                items.append({"id": fid, "path": path})
            if (len(items) != len(selected) or {row["path"] for row in items} != expected_paths
                    or len({row["id"] for row in items}) != len(items)):
                raise TransferUnknown(intent)
            receipts.append(sorted(items, key=lambda row: row["path"]))
        if any(items != receipts[0] for items in receipts[1:]):
            raise TransferUnknown(intent)
        items = receipts[0]
        intent["receipt"] = {"items": items}
        intent["stage"] = "provider_response"
        try:
            self.intent_sink(json.loads(json.dumps(intent)))
            result = self.reconcile_transfer(intent)
        except Exception:
            raise TransferUnknown(intent) from None
        result["intent"] = intent
        return result

    def reconcile_transfer(self, intent):
        """Read-only reconciliation; never repeats a mutation.

        Persist intent BEFORE the caller's write. For recovery without a response,
        the target must have been absent in before_ids and match a nonempty opaque
        source fingerprint. Name+size alone intentionally remains unknown.
        """
        target = _path(intent["target"], target=True, cloud_root=self.cloud_root)
        files = intent.get("files", [])
        if not files:
            return {"status": "unknown", "reason": "expected_files_missing"}
        observed = {r["path"]: r for r in self.list_target(target)}
        receipts = {r["path"]: r for r in intent.get("receipt", {}).get("items", [])}
        before = set(str(v) for v in intent.get("before_ids", []))
        verified = []
        for source in files:
            name = source.get("name", source.get("server_filename", ""))
            if not name or posixpath.basename(name) != name:
                raise BaiduError("Invalid recovery filename")
            wanted = target + "/" + name
            row = observed.get(wanted)
            if not row or row["is_dir"] or row["size"] != int(source["size"]):
                return {"status": "unknown", "reason": "target_not_verified", "verified": verified}
            receipt = receipts.get(wanted)
            fp = source.get("provider_fingerprint", source.get("md5"))
            if receipt:
                matches = row["id"] == str(receipt["id"])
            else:
                matches = ("before_ids" in intent and row["id"] not in before and bool(fp)
                           and fp == row.get("provider_fingerprint"))
            if not matches:
                return {"status": "unknown", "reason": "target_identity_unconfirmed", "verified": verified}
            verified.append(row)
        if receipts and set(receipts) != {r["path"] for r in verified}:
            return {"status": "unknown", "reason": "provider_result_set_mismatch"}
        return {"status": "confirmed", "files": verified, "verification": "provider_identity_and_size"}

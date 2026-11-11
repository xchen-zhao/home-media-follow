"""Independent, user-assisted official Baidu login on the Windows host.

Only a NEW dedicated Edge profile is used. Cookie values go exclusively to the
injected local secret_sink; callbacks and return values contain no credentials.
Sources: Microsoft Edge DevTools Protocol; Chromium Storage.getCookies.
This is browser session authentication, not an OAuth refresh-token service.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import struct
import stat
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone


class LoginError(RuntimeError):
    """A deliberately credential-free login error."""


class _CDP:
    """Small loopback-only WebSocket CDP client, requiring no installed modules."""

    def __init__(self, url: str, timeout: float = 8):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "ws" or parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise LoginError("CDP endpoint must be loopback")
        self.sock = socket.create_connection((parsed.hostname, parsed.port), timeout)
        self.sock.settimeout(timeout)
        self.file = self.sock.makefile("rb")
        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        target = parsed.path or "/"
        if parsed.query:
            target += "?" + parsed.query
        request = (f"GET {target} HTTP/1.1\r\n"
                   f"Host: {parsed.netloc}\r\nUpgrade: websocket\r\n"
                   f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                   "Sec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(request.encode("ascii"))
        status = self.file.readline(4096)
        headers = {}
        total = len(status)
        while True:
            line = self.file.readline(4096)
            total += len(line)
            if total > 32768 or not line:
                self.close()
                raise LoginError("Invalid CDP handshake")
            if line == b"\r\n":
                break
            name, _, value = line.decode("latin1").partition(":")
            headers[name.lower()] = value.strip()
        expected = base64.b64encode(hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()).decode("ascii")
        if b" 101 " not in status or headers.get("sec-websocket-accept") != expected:
            self.close()
            raise LoginError("CDP handshake rejected")

    def close(self):
        self.file.close()
        self.sock.close()

    def _read(self, n):
        data = self.file.read(n)
        if len(data) != n:
            raise LoginError("CDP connection closed")
        return data

    def _send(self, payload, opcode=1):
        mask = secrets.token_bytes(4)
        n = len(payload)
        prefix = bytes([0x80 | opcode])
        if n < 126:
            prefix += bytes([0x80 | n])
        elif n < 65536:
            prefix += bytes([0x80 | 126]) + struct.pack("!H", n)
        else:
            prefix += bytes([0x80 | 127]) + struct.pack("!Q", n)
        self.sock.sendall(prefix + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def call(self, method, params=None):
        self._send(json.dumps({"id": 1, "method": method, "params": params or {}}).encode())
        parts = bytearray()
        while True:
            a, b = self._read(2)
            n = b & 127
            if n == 126:
                n = struct.unpack("!H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._read(8))[0]
            if n > 16 * 1024 * 1024 or len(parts) + n > 16 * 1024 * 1024:
                raise LoginError("CDP response exceeds limit")
            mask = self._read(4) if b & 128 else None
            data = self._read(n)
            if mask:
                data = bytes(v ^ mask[i % 4] for i, v in enumerate(data))
            opcode = a & 15
            if opcode == 8:
                raise LoginError("CDP connection closed")
            if opcode == 9:
                self._send(data, opcode=10)
                continue
            if opcode not in (0, 1):
                continue
            parts.extend(data)
            if not a & 128:
                continue
            value = json.loads(parts)
            parts.clear()
            if value.get("id") == 1:
                if "error" in value:
                    raise LoginError("CDP operation rejected")
                return value.get("result", {})


def _cdp_call(url, method, params=None):
    client = _CDP(url)
    try:
        return client.call(method, params)
    finally:
        client.close()


def _local_json(port, path):
    # Never inherit an environment HTTP proxy for a loopback debugger request.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"http://127.0.0.1:{int(port)}{path}", timeout=5) as response:
        raw = response.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise LoginError("CDP discovery exceeds limit")
    return json.loads(raw)


def _cookie_header(cookies):
    selected = {}
    for item in cookies:
        domain = str(item.get("domain", "")).lstrip(".").lower()
        if domain != "baidu.com" and domain != "pan.baidu.com":
            continue
        if item.get("expires", -1) > 0 and item["expires"] <= time.time():
            continue
        name, value = item.get("name", ""), item.get("value", "")
        if not name or any(c in str(name) + str(value) for c in "\r\n;"):
            continue
        # Host cookies override parent cookies with the same name.
        if name not in selected or domain == "pan.baidu.com":
            selected[name] = value
    if not selected.get("BDUSS"):
        return None
    return "; ".join(f"{k}={v}" for k, v in sorted(selected.items()))


def _edge_executable(explicit=None):
    if explicit:
        p = Path(explicit)
        if p.is_file():
            return str(p)
        raise LoginError("Specified Edge executable not found")
    for env in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
        if os.environ.get(env):
            p = Path(os.environ[env]) / "Microsoft/Edge/Application/msedge.exe"
            if p.is_file():
                return str(p)
    raise LoginError("Microsoft Edge not found; no browser was installed")


def _validate_cookie(cookie):
    from .baidu import BaiduAdapter, BaiduError
    try:
        BaiduAdapter(lambda: {"cookie": cookie}, timeout=8).validate_session()
        return True
    except BaiduError:
        return False


def _login_page_ready(page_ws, can_click):
    """Observed official /login entry only; never click arbitrary page controls."""
    expression = r"""(() => {
      if (location.hostname !== 'pan.baidu.com') return {ready:false,clicked:false};
      const visible = e => {const r=e.getBoundingClientRect();
        return r.width>0 && r.height>0 && getComputedStyle(e).visibility!=='hidden';};
      if (CAN_CLICK) {
        const buttons=Array.from(document.querySelectorAll('button.bd-login-button__wrapper'))
          .filter(e=>visible(e) && e.textContent.trim()==='去登录');
        if(buttons.length===1){buttons[0].click();return {ready:false,clicked:true};}
      }
      const text=document.body ? document.body.innerText : '';
      const qrText=/扫码登录|二维码登录|扫描二维码|手机扫码/.test(text);
      const dialogs=Array.from(document.querySelectorAll(
        '#passport-login-pop-dialog,.pass-login-pop-content,.tang-pass-pop-login,.bd-login-dialog'))
        .some(visible);
      const qr=Array.from(document.querySelectorAll(
        '.tang-pass-qrcode-img,img[src*="qrcode"],img[src*="qrCode"]')).some(visible);
      const frames=Array.from(document.querySelectorAll('iframe')).some(e=>
        visible(e) && /^https:\/\/(passport|wappass)\.baidu\.com\//.test(e.src));
      return {ready:(dialogs && (qrText || qr || frames)) || (qrText && qr),clicked:false};
    })()""".replace("CAN_CLICK", "true" if can_click else "false")
    answer = _cdp_call(page_ws, "Runtime.evaluate", {"expression": expression, "returnByValue": True})
    value = answer.get("result", {}).get("value", {})
    return {"ready": value.get("ready") is True, "clicked": value.get("clicked") is True}


def _owned_path(value):
    path=Path(value).absolute()
    for item in (path,*path.parents):
        try: info=item.lstat()
        except FileNotFoundError: continue
        if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0) & 0x400:
            raise LoginError('Reparse points are not allowed in dedicated login paths')
    return path.resolve()


def launch_edge_login(secret_sink, *, profile_dir, screenshot_path,
                      on_ready=None, headless=True, edge_path=None, timeout=600):
    """Open official login and save its session locally through secret_sink.

    Run locally on Windows. The caller must apply Windows ACLs to the profile and sink,
    and provision the sink for the actual service account (not user DPAPI).
    A failed/timed-out attempt keeps the dedicated profile for local recovery.
    No existing browser is attached to, closed, or read. No secrets in results.
    """
    if not callable(secret_sink):
        raise TypeError("A local encrypted secret_sink is required")
    profile = _owned_path(profile_dir)
    marker = profile / ".home-media-follow-auth"
    if profile.exists() and any(profile.iterdir()) and not marker.is_file():
        raise LoginError("Refusing to use an existing unrelated browser profile")
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    marker.touch(exist_ok=True)
    # Stale discovery from a previous closed attempt must not attach elsewhere.
    active = profile / "DevToolsActivePort"
    if active.is_file():
        try:
            previous = int(active.read_text(encoding="utf-8").splitlines()[0])
            _local_json(previous, "/json/version")
        except (OSError, ValueError, KeyError, IndexError, LoginError):
            pass
        else:
            raise LoginError("Dedicated login browser is already running")
    active.unlink(missing_ok=True)
    screenshot = _owned_path(screenshot_path)
    screenshot.parent.mkdir(parents=True, exist_ok=True)
    args = [_edge_executable(edge_path), f"--user-data-dir={profile}",
            "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0",
            "--no-first-run", "--no-default-browser-check", "--disable-sync",
            "--window-size=1280,960"]
    if headless:
        args.append("--headless=new")
    args.append("https://pan.baidu.com/disk/main")
    process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + timeout
    browser_ws = None
    port = None
    last_screenshot = 0.0
    stable_cookie = None
    cookie_since = 0.0
    authenticated_seen = False
    login_clicked = False
    result = None
    try:
        while time.monotonic() < deadline:
            if port is None:
                try:
                    lines = active.read_text(encoding="utf-8").splitlines()
                    port = int(lines[0])
                    if not 0 < port < 65536:
                        raise ValueError()
                    browser_ws = _local_json(port, "/json/version")["webSocketDebuggerUrl"]
                except (OSError, ValueError, KeyError):
                    port = None
                    time.sleep(0.5)
                    continue
            cookie = None
            try:
                cookie = _cookie_header(_cdp_call(browser_ws, "Storage.getCookies").get("cookies", []))
                if cookie:
                    # Do not screenshot after authentication. Never return the secret.
                    authenticated_seen = True
                    digest = hashlib.sha256(cookie.encode()).digest()
                    if digest != stable_cookie:
                        stable_cookie, cookie_since = digest, time.monotonic()
                    if time.monotonic() - cookie_since < 3 or not _validate_cookie(cookie):
                        time.sleep(1)
                        continue
                    try:
                        secret_sink({"cookie": cookie, "source": "independent_windows_edge",
                                     "created_at": datetime.now(timezone.utc).isoformat()})
                    except Exception:
                        raise LoginError("Local secret storage failed; profile retained") from None
                    result = {"status": "session_saved", "profile": str(profile),
                              "requires_capability_validation": True}
                    break
                if not authenticated_seen and time.monotonic() - last_screenshot >= 5:
                    targets = _local_json(port, "/json/list")
                    pages = [t for t in targets if t.get("type") == "page" and
                             urllib.parse.urlsplit(t.get("url", "")).hostname in
                             ("pan.baidu.com", "passport.baidu.com", "wappass.baidu.com")]
                    if pages:
                        readiness = _login_page_ready(pages[0]["webSocketDebuggerUrl"], not login_clicked)
                        login_clicked = login_clicked or readiness["clicked"]
                        if not readiness["ready"]:
                            last_screenshot = time.monotonic()
                            continue
                        # Recheck cookies immediately before capture to avoid account views.
                        if _cookie_header(_cdp_call(browser_ws, "Storage.getCookies").get("cookies", [])):
                            continue
                        value = _cdp_call(pages[0]["webSocketDebuggerUrl"], "Page.captureScreenshot",
                                          {"format": "png"})
                        if _cookie_header(_cdp_call(browser_ws, "Storage.getCookies").get("cookies", [])):
                            continue
                        png = base64.b64decode(value["data"], validate=True)
                        if not png.startswith(b"\x89PNG\r\n\x1a\n"):
                            raise LoginError("Login screenshot is invalid")
                        temp = screenshot.with_suffix(".pending.png")
                        temp.write_bytes(png)
                        temp.replace(screenshot)
                        info = {"status": "waiting_for_user_login", "port": port,
                                "profile": str(profile), "screenshot_path": str(screenshot)}
                        if on_ready:
                            on_ready(info)
                    last_screenshot = time.monotonic()
            except (OSError, ValueError, KeyError, LoginError):
                if cookie:
                    raise LoginError("Local secret storage failed; profile retained") from None
                # Browser navigation can briefly invalidate a target. No raw errors/logs.
                pass
            time.sleep(1)
        if result is None:
            raise LoginError("Official browser login not completed; dedicated profile retained")
        return result
    finally:
        if browser_ws:
            try:
                _cdp_call(browser_ws, "Browser.close")
            except Exception:
                pass
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            # Only the process launched with this dedicated profile is terminated.
            process.terminate()
        # Keep profile until caller confirms durable sink/service verification.


def remove_login_profile(profile_dir):
    """Explicit cleanup AFTER sink and service validation; refuses unrelated paths."""
    profile = _owned_path(profile_dir)
    if not (profile / ".home-media-follow-auth").is_file():
        raise LoginError("Not a dedicated login profile")
    shutil.rmtree(profile)

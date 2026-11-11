"""Bounded original-media probing, with no remote URLs in results or logs.

ffprobe sees only a random loopback URL. A single shared byte budget and deadline
cover HEAD/GET, all Range requests, redirects, and ffprobe's retries. No content
is saved to disk. Windows needs only its existing bundled ffprobe executable.
"""
from __future__ import annotations

from fractions import Fraction
import http.client
import http.server
import json
import math
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import threading
import time
import urllib.parse


DEFAULT_FFPROBE = "ffprobe"
IMAGE_CODECS = {"mjpeg", "png", "webp", "gif", "bmp", "tiff", "apng"}
MAX_REDIRECTS = 16
MAX_REQUESTS = 128


def _unknown(reason, *, bytes_read=0):
    return {"status": "unknown", "reason": reason, "width": None,
            "height": None, "codec": None, "duration": None, "bytes_read": bytes_read}


def _number(value):
    try:
        x = float(value)
        return x if math.isfinite(x) else None
    except (ValueError, TypeError, OverflowError):
        return None


def _flag(value):
    return value in (1, "1", True)


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _media_result(document, *, bytes_read=0):
    if not isinstance(document, dict) or not isinstance(document.get("streams"), list):
        return _unknown("media_parameters_missing", bytes_read=bytes_read)
    streams = [s for s in document["streams"] if isinstance(s, dict)
               and s.get("codec_type") == "video"
               and not _flag(_mapping(s.get("disposition")).get("attached_pic"))
               and s.get("codec_name") not in IMAGE_CODECS]
    if not streams:
        return _unknown("no_primary_video", bytes_read=bytes_read)
    if len(streams) > 1:
        defaults = [s for s in streams if _flag(_mapping(s.get("disposition")).get("default"))]
        if len(defaults) != 1:
            return _unknown("ambiguous_primary_video", bytes_read=bytes_read)
        stream = defaults[0]
    else:
        stream = streams[0]
    w, h = stream.get("width"), stream.get("height")
    if isinstance(w, bool) or isinstance(h, bool) or not isinstance(w, int) or not isinstance(h, int) or min(w, h) <= 0:
        return _unknown("invalid_dimensions", bytes_read=bytes_read)
    rotation = _mapping(stream.get("tags")).get("rotate")
    side_data = stream.get("side_data_list")
    for side in side_data if isinstance(side_data, list) else []:
        if isinstance(side, dict) and "rotation" in side:
            rotation = side["rotation"]
    if rotation is not None:
        angle = _number(rotation)
        if angle is None or abs(angle / 90 - round(angle / 90)) > 0.001:
            return _unknown("ambiguous_rotation", bytes_read=bytes_read)
        if round(angle / 90) % 2:
            w, h = h, w
    duration = _number(stream.get("duration"))
    if not duration or duration <= 0:
        duration = _number(_mapping(document.get("format")).get("duration"))
    if not duration or duration <= 0:
        return _unknown("duration_unverified", bytes_read=bytes_read)
    result = {"status": "verified", "width": w, "height": h,
              "codec": stream.get("codec_name"), "duration": duration,
              "stream_index": stream.get("index"), "bytes_read": bytes_read}
    if not result["codec"]:
        return _unknown("codec_unverified", bytes_read=bytes_read)
    aspect = max(w, h) / min(w, h)
    if aspect > 3:
        result.update(status="unknown", reason="abnormal_aspect_ratio")
        return result
    if max(w, h) < 1920:
        result.update(status="rejected", reason="below_minimum_resolution")
    # Encoding pixel dimensions are evidence, not proof of native source quality.
    for field in ("avg_frame_rate", "r_frame_rate"):
        try:
            fps = float(Fraction(str(stream.get(field, "0/1"))))
        except (ValueError, ZeroDivisionError, OverflowError):
            continue
        if math.isfinite(fps) and fps > 0:
            result["fps"] = fps
            break
    bitrate = _number(stream.get("bit_rate")) or _number(_mapping(document.get("format")).get("bit_rate"))
    if bitrate and bitrate > 0:
        result["bitrate"] = int(bitrate)
    bit_depth = _number(stream.get("bits_per_raw_sample"))
    if bit_depth and bit_depth > 0:
        result["bit_depth"] = int(bit_depth)
    transfer = stream.get("color_transfer")
    if transfer in ("smpte2084", "arib-std-b67"):
        result["hdr"] = transfer
    return result


def _executable(override):
    if override:
        return os.fspath(override)
    if Path(DEFAULT_FFPROBE).is_file():
        return DEFAULT_FFPROBE
    return shutil.which("ffprobe")


def _command(ffprobe, source):
    fields = ("stream=index,codec_name,codec_type,width,height,duration,avg_frame_rate,r_frame_rate,"
              "bit_rate,bits_per_raw_sample,color_transfer:stream_disposition=attached_pic,default:"
              "stream_tags=rotate:stream_side_data=rotation:format=duration,bit_rate")
    return [ffprobe, "-v", "error", "-rw_timeout", "5000000", "-probesize", "1048576",
            "-analyzeduration", "1000000", "-show_entries", fields, "-of", "json", source]


def _run(ffprobe, source, timeout):
    try:
        completed = subprocess.run(_command(ffprobe, source), stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   timeout=max(0.01, timeout), check=False)
        if completed.returncode:
            return None, "probe_failed"
        if len(completed.stdout) > 8 * 1024 * 1024:
            return None, "probe_output_limit"
        return json.loads(completed.stdout), None
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except (ValueError, OSError):
        return None, "probe_unavailable_or_invalid_output"


def _url(value):
    if not isinstance(value, str) or any(c in value for c in "\r\n\x00"):
        raise ValueError("Invalid media URL")
    p = urllib.parse.urlsplit(value)
    if p.scheme not in ("http", "https") or not p.hostname or "@" in p.netloc or p.fragment:
        raise ValueError("Invalid media URL")
    if p.port is not None and not 1 <= p.port <= 65535:
        raise ValueError("Invalid media port")
    return p


def _origin(parsed):
    return parsed.scheme, parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == "https" else 80)


class _Budget:
    def __init__(self, url, headers, max_bytes, timeout):
        self.url = url
        self.headers = headers
        self.remaining = max_bytes
        self.read_bytes = 0
        self.deadline = time.monotonic() + timeout
        self.failure = None
        self.redirects = 0
        self.requests = 0
        self.lock = threading.Lock()
        self.connections = set()
        self.stopping = False

    def fail(self, reason):
        with self.lock:
            if self.failure is None and not self.stopping:
                self.failure = reason

    def seconds(self):
        left = self.deadline - time.monotonic()
        if left <= 0:
            self.fail("timeout")
            raise TimeoutError()
        return min(left, 5)

    def add_request(self):
        with self.lock:
            if self.stopping or self.failure:
                raise ValueError()
            self.requests += 1
            if self.requests > MAX_REQUESTS:
                self.failure = "request_limit"
                raise ValueError()
        self.seconds()

    def reserve(self):
        self.seconds()
        with self.lock:
            if self.stopping or self.failure:
                raise ValueError()
            if self.remaining <= 0:
                self.failure = "byte_budget_exhausted"
                raise ValueError()
            amount = min(self.remaining, 32768)
            self.remaining -= amount
            return amount

    def account(self, reserved, received):
        with self.lock:
            self.remaining += reserved - received
            self.read_bytes += received

    def close(self):
        with self.lock:
            self.stopping = True
            connections = list(self.connections)
        for connection in connections:
            try:
                if connection.sock:
                    connection.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()


def _upstream(ctx, method, range_header):
    target = ctx.url
    headers = dict(ctx.headers)
    if range_header:
        headers["Range"] = range_header
    else:
        headers.pop("Range", None)
    headers["Connection"] = "close"
    while True:
        ctx.add_request()
        parsed = _url(target)
        connection_type = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        connection = connection_type(parsed.hostname, parsed.port, timeout=ctx.seconds())
        with ctx.lock:
            ctx.connections.add(connection)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        try:
            connection.request(method, path, headers=headers)
            response = connection.getresponse()
        except Exception:
            with ctx.lock:
                ctx.connections.discard(connection)
            connection.close()
            raise
        if response.status in (301, 302, 303, 307, 308):
            location = response.getheader("Location")
            response.close()  # do not consume arbitrary redirect bodies
            connection.close()
            with ctx.lock:
                ctx.connections.discard(connection)
                ctx.redirects += 1
                if ctx.redirects > MAX_REDIRECTS:
                    ctx.failure = "redirect_limit"
                    raise ValueError()
            if not location:
                ctx.fail("invalid_redirect")
                raise ValueError()
            redirected = urllib.parse.urljoin(target, location)
            new = _url(redirected)
            if parsed.scheme == "https" and new.scheme != "https":
                ctx.fail("insecure_redirect")
                raise ValueError()
            if _origin(new) != _origin(parsed):
                # Secret auth/cookies and signed Referer must never reach a CDN.
                headers = {k: v for k, v in headers.items()
                           if k.lower() in ("user-agent", "accept", "accept-encoding", "range", "connection")}
            target = redirected
            continue
        return connection, response


def probe_url(url, headers=None, max_bytes=32 * 1024 * 1024, timeout=60, *, ffprobe=None):
    """Probe an original URL with a hard cumulative upstream body-read bound."""
    try:
        _url(url)
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise ValueError()
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError()
        clean_headers = {"User-Agent": "pan.baidu.com"}
        for key, value in (headers or {}).items():
            if not isinstance(key, str) or not isinstance(value, str) or any(c in key + value for c in "\r\n\x00"):
                raise ValueError()
            if key.lower() in ("host", "connection", "content-length", "transfer-encoding", "range"):
                raise ValueError()
            clean_headers[key] = value
    except (ValueError, TypeError, AttributeError):
        return _unknown("invalid_probe_request")
    executable = _executable(ffprobe)
    if not executable:
        return _unknown("ffprobe_unavailable")
    context = _Budget(url, clean_headers, max_bytes, float(timeout))
    key = secrets.token_hex(24)

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_HEAD(self):
            self.serve(True)

        def do_GET(self):
            self.serve(False)

        def serve(self, head):
            if self.path != "/" + key:
                self.send_error(404)
                return
            connection = response = None
            reserved = 0
            try:
                # No new body requests are accepted once the shared budget is gone.
                with context.lock:
                    if not head and context.remaining <= 0:
                        context.failure = "byte_budget_exhausted"
                        raise ValueError()
                connection, response = _upstream(context, "HEAD" if head else "GET", self.headers.get("Range"))
                if response.status not in (200, 206):
                    context.fail("upstream_http_error")
                    self.send_error(502)
                    return
                encoding = response.getheader("Content-Encoding", "identity").lower()
                if encoding not in ("identity", ""):
                    context.fail("encoded_media_response")
                    self.send_error(502)
                    return
                self.send_response(response.status)
                for header in ("Content-Length", "Content-Range", "Content-Type", "Accept-Ranges"):
                    if response.getheader(header):
                        self.send_header(header, response.getheader(header))
                self.send_header("Connection", "close")
                self.end_headers()
                self.connection.settimeout(context.seconds())
                if head:
                    return
                while True:
                    reserved = context.reserve()
                    if connection.sock:
                        connection.sock.settimeout(context.seconds())
                    chunk = response.read(reserved)
                    context.account(reserved, len(chunk))
                    reserved = 0
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                # ffprobe legitimately closes a read once it has enough headers.
                pass
            except (TimeoutError, socket.timeout):
                if not context.stopping:
                    context.fail("timeout")
            except (OSError, ValueError, http.client.HTTPException):
                if not context.stopping:
                    context.fail("upstream_read_failed")
            finally:
                if reserved:
                    # A failed read can consume bytes before raising. Spend the
                    # reservation rather than allowing retries to reuse its budget.
                    if not context.stopping:
                        context.account(reserved, reserved)
                if response:
                    response.close()
                if connection:
                    connection.close()
                    with context.lock:
                        context.connections.discard(connection)
                self.close_connection = True

    class Server(http.server.ThreadingHTTPServer):
        daemon_threads = True
        block_on_close = False

    server = Server(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        source = f"http://127.0.0.1:{server.server_port}/{key}"
        data, failure = _run(executable, source, max(0.01, context.deadline - time.monotonic()))
        # Stop readers before examining the final aggregate byte count/failure.
        server.shutdown()
        context.close()
        if context.failure:
            return _unknown(context.failure, bytes_read=context.read_bytes)
        if failure:
            return _unknown(failure, bytes_read=context.read_bytes)
        return _media_result(data, bytes_read=context.read_bytes)
    finally:
        server.shutdown()
        context.close()
        server.server_close()
        thread.join(timeout=1)


def probe_file(path, timeout=60, *, ffprobe=None):
    """Probe a completed local file before publishing; never modify the file."""
    try:
        source = Path(path)
        if not source.is_file() or source.is_symlink() or source.stat().st_size <= 0:
            return _unknown("local_media_missing_or_invalid")
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            return _unknown("invalid_probe_request")
        before = source.stat()
    except (OSError, TypeError, ValueError):
        return _unknown("local_media_missing_or_invalid")
    executable = _executable(ffprobe)
    if not executable:
        return _unknown("ffprobe_unavailable")
    data, failure = _run(executable, str(source.resolve()), float(timeout))
    if failure:
        return _unknown(failure)
    try:
        after = source.stat()
    except OSError:
        return _unknown("local_media_changed")
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        return _unknown("local_media_changed")
    result = _media_result(data)
    result["file_size"] = after.st_size
    # ffprobe's local I/O byte count is not exposed; do not invent a read amount.
    result["bytes_read"] = None
    return result

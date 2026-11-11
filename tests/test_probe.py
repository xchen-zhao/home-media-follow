import contextlib
import http.server
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from home_media_follow import probe


def stream(width=3840, height=2160, **kwargs):
    return {"index": 0, "codec_type": "video", "codec_name": "h264", "width": width,
            "height": height, "disposition": {"default": 0, "attached_pic": 0}, **kwargs}


def media(*streams):
    return {"streams": list(streams or [stream()]), "format": {"duration": "100.5"}}


@contextlib.contextmanager
def upstream(*, body=b"x" * 8192, delay=0, redirect=None, seen=None):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def log_message(self, *args):
            pass
        def do_HEAD(self):
            self.serve(True)
        def do_GET(self):
            self.serve(False)
        def serve(self, head):
            if seen is not None:
                seen.append(dict(self.headers))
            if delay:
                time.sleep(delay)
            if redirect:
                self.send_response(302)
                self.send_header("Location", redirect(self.server.server_port))
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            # Intentionally ignores Range: correctness must not depend on support.
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Type", "application/octet-stream")
            self.end_headers()
            if not head:
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass
    class Server(http.server.ThreadingHTTPServer):
        daemon_threads = True
        block_on_close = False
    server = Server(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/original?sign=never-in-output"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def consume_remote_result(ffprobe, source, timeout, *, count=1):
    for _ in range(count):
        req = urllib.request.Request(source, headers={"Range": "bytes=1000-"})
        try:
            with urllib.request.urlopen(req, timeout=max(0.2, timeout)) as response:
                response.read()
        except (OSError, urllib.error.URLError, probe.http.client.HTTPException):
            pass
    return media(), None


class ParametersTests(unittest.TestCase):
    def test_accepts_crop_and_1080p_rejects_lower_resolution(self):
        for w, h in [(3840, 1600), (1920, 800), (1920, 1080), (2560, 1440)]:
            self.assertEqual(probe._media_result(media(stream(w, h)))["status"], "verified")
        r = probe._media_result(media(stream(1280, 720)))
        self.assertEqual((r["status"], r["reason"]), ("rejected", "below_minimum_resolution"))

    def test_images_and_attached_cover_do_not_hide_real_1080_video(self):
        cover = stream(codec_name="mjpeg", disposition={"attached_pic": 1, "default": 1})
        r = probe._media_result(media(cover, stream(1920, 1080)))
        self.assertEqual((r["status"], r["width"]), ("verified", 1920))
        self.assertEqual(probe._media_result(media(cover))["status"], "unknown")

    def test_multiple_video_tracks_require_unique_default(self):
        a, b = stream(), stream(1920, 1080, index=1)
        self.assertEqual(probe._media_result(media(a, b))["reason"], "ambiguous_primary_video")
        a["disposition"]["default"] = 1
        self.assertEqual(probe._media_result(media(a, b))["width"], 3840)
        b["disposition"]["default"] = 1
        self.assertEqual(probe._media_result(media(a, b))["status"], "unknown")

    def test_invalid_dimensions_aspect_duration_and_rotation(self):
        self.assertEqual(probe._media_result(media(stream(3840, 500)))["reason"], "abnormal_aspect_ratio")
        self.assertEqual(probe._media_result(media(stream(0, 1080)))["reason"], "invalid_dimensions")
        self.assertEqual(probe._media_result({"streams": [stream()]})["reason"], "duration_unverified")
        rotated = probe._media_result(media(stream(side_data_list=[{"rotation": -90}])))
        self.assertEqual((rotated["width"], rotated["height"]), (2160, 3840))
        self.assertEqual(probe._media_result(media(stream(tags={"rotate": "17"})))["status"], "unknown")

    def test_fps_and_encoded_pixel_measurement_are_actual_parameters(self):
        r = probe._media_result(media(stream(1280, 720, sample_aspect_ratio="3:2",
            avg_frame_rate="30000/1001", bit_rate="6000000")))
        self.assertEqual(r["status"], "rejected")  # SAR cannot manufacture pixels.
        self.assertAlmostEqual(r["fps"], 29.97002997)
        self.assertEqual(r["bitrate"], 6000000)


class BoundedHTTPTests(unittest.TestCase):
    def test_range_ignored_is_truncated_at_hard_budget(self):
        seen = []
        with upstream(body=b"x" * 200000, seen=seen) as url, \
             patch.object(probe, "_run", side_effect=consume_remote_result):
            result = probe.probe_url(url, max_bytes=4096, timeout=3, ffprobe="fixture")
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["bytes_read"], 4096)
        self.assertEqual(result["reason"], "byte_budget_exhausted")
        self.assertEqual(seen[0]["Range"], "bytes=1000-")
        self.assertNotIn("never-in-output", json.dumps(result))

    def test_requests_share_one_budget_instead_of_resetting_per_retry(self):
        def consume(*args):
            return consume_remote_result(*args, count=2)
        with upstream(body=b"x" * 2000) as url, patch.object(probe, "_run", side_effect=consume):
            result = probe.probe_url(url, max_bytes=3000, timeout=3, ffprobe="fixture")
        self.assertEqual(result["bytes_read"], 3000)
        self.assertEqual(result["status"], "unknown")

    def test_cross_origin_redirect_strips_secrets_and_counts_received_body(self):
        target_headers = []
        with upstream(body=b"x" * 1000, seen=target_headers) as final_url:
            with upstream(redirect=lambda port: final_url) as initial_url, \
                 patch.object(probe, "_run", side_effect=consume_remote_result):
                result = probe.probe_url(initial_url, headers={"Authorization": "local-only",
                    "Cookie": "cookie-local-only", "Referer": "private-signature"}, ffprobe="fixture", timeout=3)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["bytes_read"], 1000)
        self.assertNotIn("Authorization", target_headers[0])
        self.assertNotIn("Cookie", target_headers[0])
        self.assertNotIn("Referer", target_headers[0])

    def test_redirect_loop_has_finite_budget_and_no_secret_error(self):
        with upstream(redirect=lambda port: f"http://127.0.0.1:{port}/loop?sign=hidden") as url, \
             patch.object(probe, "_run", side_effect=consume_remote_result):
            result = probe.probe_url(url, ffprobe="fixture", timeout=3)
        self.assertEqual(result["reason"], "redirect_limit")
        self.assertNotIn("hidden", json.dumps(result))

    def test_timeout_is_cumulative_and_bounded(self):
        start = time.monotonic()
        with upstream(delay=0.5) as url, patch.object(probe, "_run", side_effect=consume_remote_result):
            result = probe.probe_url(url, timeout=0.1, ffprobe="fixture")
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["reason"], "timeout")
        self.assertLess(time.monotonic() - start, 1)

    def test_invalid_url_credentials_and_headers_are_not_used(self):
        for url in ["file:///etc/passwd", "http://user" + chr(64) + "127.0.0.1/", "http://host/#fragment"]:
            self.assertEqual(probe.probe_url(url)["reason"], "invalid_probe_request")
        self.assertEqual(probe.probe_url("http://127.0.0.1/", headers={"X": "\r\nInjected: yes"})["status"], "unknown")


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "local ffmpeg tools unavailable")
class ActualMediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.file = Path(cls.temp.name) / "original.mp4"
        subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
            "color=c=black:s=1920x1080:r=1", "-t", "2", "-c:v", "mpeg4", "-q:v", "10",
            "-movflags", "+faststart", "-y", str(cls.file)], check=True, timeout=20,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_local_file_actual_dimensions_codec_and_duration(self):
        result = probe.probe_file(self.file)
        self.assertEqual(result["status"], "verified")
        self.assertEqual((result["width"], result["height"], result["codec"]), (1920, 1080, "mpeg4"))
        self.assertGreater(result["duration"], 0)
        self.assertIsNone(result["bytes_read"])

    def test_real_ffprobe_original_http_range_ignored(self):
        data = self.file.read_bytes()
        with upstream(body=data) as url:
            result = probe.probe_url(url, max_bytes=4 * 1024 * 1024, timeout=10)
        self.assertEqual(result["status"], "verified", result)
        self.assertEqual((result["width"], result["height"]), (1920, 1080))
        self.assertGreater(result["bytes_read"], 0)
        self.assertLessEqual(result["bytes_read"], 4 * 1024 * 1024)

    def test_local_file_change_while_probing_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "mutable.mp4"
            shutil.copyfile(self.file, source)
            def fake(*args):
                source.write_bytes(b"changed")
                return media(), None
            with patch.object(probe, "_run", side_effect=fake):
                self.assertEqual(probe.probe_file(source)["reason"], "local_media_changed")


if __name__ == "__main__":
    unittest.main()

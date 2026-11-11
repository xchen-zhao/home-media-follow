import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import Mock, patch
import urllib.parse

from home_media_follow.service_auth import authorize_jellyfin, ServiceAuthError


def document(value=None, status=200):
    return status, b"" if value is None else json.dumps(value).encode()


@contextlib.contextmanager
def fixture(route):
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def handle_request(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            body = json.loads(raw) if raw else None
            calls.append((self.command, self.path, body, dict(self.headers)))
            status, content = route(self.command, self.path, body, self.headers)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        do_POST = handle_request
        do_GET = handle_request
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


class ServiceAuthTests(unittest.TestCase):
    def test_401_is_sanitized_and_never_calls_secret_sink(self):
        def route(method, path, body, headers):
            return document({"error": "fixture-secret https://fixture.invalid/private"}, 401)
        sink = Mock()
        with fixture(route) as (base, _):
            with self.assertRaises(ServiceAuthError) as caught:
                authorize_jellyfin("fixture-user", "fixture-secret", sink, base)
            self.assertEqual(caught.exception.status_code, 401)
            self.assertNotIn("fixture-secret", str(caught.exception))
            self.assertNotIn("https://", str(caught.exception))
            sink.assert_not_called()

    def test_non_admin_does_not_create_or_store_key(self):
        def route(method, path, body, headers):
            if path == "/Sessions/Logout":
                return document(status=204)
            return document({"AccessToken": "fixture-session", "User": {"Id": "user", "Policy": {"IsAdministrator": False}}})
        sink = Mock()
        with fixture(route) as (base, calls):
            with self.assertRaises(ServiceAuthError) as caught:
                authorize_jellyfin("fixture-user", "fixture-secret", sink, base)
            self.assertEqual(caught.exception.operation, "administrator_required")
            self.assertFalse(any("/Auth/Keys" in path for _, path, _, _ in calls))
            self.assertTrue(any(path == "/Sessions/Logout" for _, path, _, _ in calls))
            sink.assert_not_called()

    def test_reuses_dedicated_key_and_sink_only_receives_api_key(self):
        sink_values = []
        def route(method, path, body, headers):
            self.assertTrue(headers.get("Authorization", "").startswith("MediaBrowser "))
            self.assertNotIn("fixture-session", path)
            self.assertNotIn("fixture-key", path)
            if path == "/Users/AuthenticateByName":
                self.assertEqual(body, {"Username": "fixture-user", "Pw": "fixture-secret"})
                self.assertIn('Client="HomeMedia-Follow"', headers["Authorization"])
                self.assertNotIn("Token=", headers["Authorization"])
                return document({"AccessToken": "fixture-session", "User": {"Id": "admin", "Policy": {"IsAdministrator": True}}})
            if path == "/Auth/Keys":
                self.assertEqual(method, "GET")
                return document({"Items": [{"AppName": "Other-Application", "AccessToken": "other-key"}, {"AppName": "HomeMedia-Follow", "AccessToken": "fixture-key"}], "TotalRecordCount": 2})
            if path == "/System/Info":
                self.assertIn('Token="fixture-key"', headers["Authorization"])
                return document({"Id": "server", "Version": "10.11.2"})
            if path == "/Sessions/Logout":
                self.assertIn('Token="fixture-session"', headers["Authorization"])
                return document(status=204)
            self.fail("Unexpected auth operation")
        with fixture(route) as (base, calls):
            result = authorize_jellyfin("fixture-user", "fixture-secret", sink_values.append, base)
            self.assertEqual(result, {"status": "authorized", "service": "jellyfin", "key_action": "reused"})
            self.assertEqual(sink_values, [{"jellyfin": {"api_key": "fixture-key"}}])
            self.assertNotIn("fixture-key", json.dumps(result))
            self.assertFalse(any(method == "POST" and path.startswith("/Auth/Keys") for method, path, _, _ in calls))

    def test_create_response_loss_reconciles_and_second_run_reuses(self):
        keys, creates = [], 0
        def route(method, path, body, headers):
            nonlocal creates
            if path == "/Users/AuthenticateByName":
                return document({"AccessToken": "fixture-session", "User": {"Policy": {"IsAdministrator": True}}})
            if path == "/Auth/Keys":
                return document({"Items": keys, "TotalRecordCount": len(keys)})
            if path.startswith("/Auth/Keys?"):
                self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(path).query), {"app": ["HomeMedia-Follow"]})
                creates += 1
                keys.append({"AppName": "HomeMedia-Follow", "AccessToken": "fixture-key"})
                return 200, b"lost-response"
            if path == "/System/Info":
                return document({"Id": "server", "Version": "10"})
            return document(status=204)
        sink = Mock()
        with fixture(route) as (base, _):
            first = authorize_jellyfin("fixture-user", "fixture-secret", sink, base)
            second = authorize_jellyfin("fixture-user", "fixture-secret", sink, base)
            self.assertEqual(first["key_action"], "created")
            self.assertEqual(second["key_action"], "reused")
            self.assertEqual(creates, 1)
            self.assertEqual(sink.call_count, 2)

    def test_key_verification_and_storage_failure_are_sanitized(self):
        def route(method, path, body, headers):
            if path == "/Users/AuthenticateByName":
                return document({"AccessToken": "fixture-session", "User": {"Policy": {"IsAdministrator": True}}})
            if path == "/Auth/Keys":
                return document({"Items": [{"AppName": "HomeMedia-Follow", "AccessToken": "fixture-key"}]})
            if path == "/System/Info":
                return document({"Id": "server", "Version": "10"})
            return document(status=204)
        sink = Mock(side_effect=RuntimeError("fixture-key fixture-secret storage failure"))
        with fixture(route) as (base, _):
            with self.assertRaises(ServiceAuthError) as caught:
                authorize_jellyfin("fixture-user", "fixture-secret", sink, base)
            self.assertEqual(caught.exception.operation, "credential_store")
            self.assertNotIn("fixture-key", str(caught.exception))

    def test_help_does_not_access_secrets(self):
        from home_media_follow import cli
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as result:
            cli.main(["authorize", "--help"])
        self.assertEqual(result.exception.code, 0)
        self.assertIn("jellyfin", output.getvalue())

    def test_cli_getpass_is_local_and_output_is_sanitized(self):
        from home_media_follow import cli
        from types import SimpleNamespace
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as directory:
            directory = str(Path(directory).resolve())
            cli.initialize(directory)
            output = io.StringIO()
            with patch.object(cli, "os", SimpleNamespace(name="nt")), patch.object(cli.sys.stdin, "isatty", return_value=True), patch.object(cli.getpass, "getpass", side_effect=["fixture-user", "fixture-secret"]) as prompts, patch("home_media_follow.service_auth.authorize_jellyfin", return_value={"status": "authorized", "service": "jellyfin", "key_action": "reused"}) as auth, patch("home_media_follow.secrets.SecretStore") as store, contextlib.redirect_stdout(output):
                self.assertEqual(cli.main(["--root", directory, "authorize", "jellyfin"]), 0)
                self.assertEqual(prompts.call_count, 2)
                auth.assert_called_once_with("fixture-user", "fixture-secret", store.return_value.update, "http://127.0.0.1:8096")
            self.assertNotIn("fixture-secret", output.getvalue())
            self.assertNotIn("fixture-user", output.getvalue())


if __name__ == "__main__":
    unittest.main()

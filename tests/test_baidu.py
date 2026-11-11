import json
import base64
import hashlib
import io
import socket
from email.utils import formatdate
from urllib.error import HTTPError
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import Mock, patch

from home_media_follow.baidu import (
    AuthRequired, BaiduAdapter, BaiduError, ConflictError, IncompleteListing,
    ShareError, TransferUnknown, normalize_item, NetworkError,
)
from home_media_follow import baidu_auth


SHORT = "A234567890123456789012"
PAGE = '<script>locals.mset({"loginstate":1,"shareid":"100","share_uk":"200","bdstoken":"' + 'a' * 32 + '"});</script>'


def item(fid, name="01.mp4", size=123, path="/source", fingerprint="opaque-fingerprint", isdir=0):
    return {"fs_id": str(fid), "server_filename": name, "path": path + "/" + name,
            "size": size, "md5": fingerprint, "isdir": isdir}


class Fixture:
    def __init__(self, callback=None):
        self.calls = []
        self.callback = callback

    def __call__(self, request, timeout):
        p = urlsplit(request.full_url)
        q = parse_qs(p.query)
        f = parse_qs(request.data.decode()) if request.data is not None else {}
        self.calls.append((p.path, q, f))
        if self.callback:
            result = self.callback(p.path, q, f)
            if result is not None:
                return result
        if p.path.startswith("/s/"):
            return PAGE
        if p.path == "/share/verify":
            return {"errno": 0, "randsk": "ephemeral-only"}
        if p.path == "/api/gettemplatevariable":
            return {"errno": 0, "result": {"bdstoken": "b" * 32}}
        if p.path in ("/share/list", "/api/list"):
            return {"errno": 0, "list": []}
        raise AssertionError("Unexpected endpoint")


class BaiduTests(unittest.TestCase):
    def adapter(self, callback=None, sink=None):
        self.http = Fixture(callback)
        return BaiduAdapter(lambda: {"cookie": "BDUSS=independent-test; STOKEN=test-only"},
                            transport=self.http, intent_sink=sink)

    def test_verify_returns_no_secret_and_session_reauth_has_separate_error(self):
        b = self.adapter()
        context = b.verify_share("https://pan.baidu.com/s/1" + SHORT, "abcd")
        self.assertEqual(context, {"short_url": SHORT, "share_id": "100", "share_uk": "200"})
        self.assertNotIn("ephemeral-only", json.dumps(context))
        self.assertEqual(b.validate_session()["status"], "authenticated")
        with self.assertRaises(AuthRequired):
            BaiduAdapter(lambda: "unrelated=thing").validate_session()

    def test_protected_init_page_verification_uses_surl_before_share_ids_exist(self):
        visits = []
        def response(path, query, form):
            if path.startswith("/s/"):
                visits.append(path)
                return '<html><body>share/init</body></html>' if len(visits) == 1 else PAGE
            if path == "/share/verify":
                self.assertEqual(query["surl"], [SHORT])
                self.assertNotIn("shareid", query)
                self.assertNotIn("uk", query)
                self.assertEqual(form["pwd"], ["abcd"])
                self.assertEqual(form["bdstoken"], ["b" * 32])
                return {"errno": 0, "randsk": "ephemeral-only"}
        adapter = self.adapter(response)
        result = adapter.verify_share("https://pan.baidu.com/s/1" + SHORT, "abcd")
        self.assertEqual(result["share_id"], "100")
        self.assertEqual(result["share_uk"], "200")
        self.assertEqual(len(visits), 2)
        self.assertNotIn("ephemeral-only", json.dumps(result))

    def test_share_verify_always_binds_short_reference_even_with_page_ids(self):
        def response(path, query, form):
            if path == "/share/verify":
                self.assertEqual(query["surl"], [SHORT])
                self.assertEqual(query["shareid"], ["100"])
                self.assertEqual(query["uk"], ["200"])
        self.adapter(response).verify_share(SHORT, "abcd")

    def test_list_uses_verified_page_token_and_exactly_once_encoded_bdclnd(self):
        for returned in ("opaque+/=value", "opaque%2B%2F%3Dvalue"):
            with self.subTest(encoded="%" in returned):
                def response(path, query, form):
                    if path == "/share/verify":
                        return {"errno": 0, "randsk": returned}
                    if path == "/share/list":
                        self.assertEqual(query["bdstoken"], ["a" * 32])
                        return {"errno": 0, "list": []}
                adapter = self.adapter(response)
                context = adapter.verify_share(SHORT, "abcd")
                adapter.list_share(context, "/source")
                cookies = [c.value for c in adapter.jar if c.name == "BDCLND"]
                self.assertEqual(cookies, ["opaque%2B%2F%3Dvalue"])
                self.assertNotIn("bdstoken", context)

    def test_missing_share_page_token_uses_validated_account_token_for_listing(self):
        def response(path, query, form):
            if path.startswith("/s/"):
                return '<script>{"loginstate":1,"shareid":"100","share_uk":"200"}</script>'
            if path == "/share/list":
                self.assertEqual(query["bdstoken"], ["b" * 32])
                return {"errno": 0, "list": []}
        adapter = self.adapter(response)
        adapter.list_share(adapter.verify_share(SHORT), "/")

    def test_provider_error_cannot_echo_secrets(self):
        b = self.adapter(lambda p, q, f: {"errno": -6, "errmsg": "BDUSS=never-echo"})
        with self.assertRaises(AuthRequired) as caught:
            b.validate_session()
        self.assertNotIn("never-echo", str(caught.exception))

    def test_http_429_exposes_only_parsed_retry_after_delay(self):
        now = 1700000000
        for header, expected in (("7200", 7200), (formatdate(now + 7200, usegmt=True), 7200),
                                 ("invalid-private-fixture", 0), ("-10", 0),
                                 (formatdate(now - 100, usegmt=True), 0)):
            with self.subTest(header_kind="date" if "," in header else "delay"):
                stream = io.BytesIO(b"private-fixture")
                def response(path, query, form):
                    raise HTTPError("https://pan.baidu.com/private-fixture", 429, "private-fixture",
                                    {"Retry-After": header}, stream)
                adapter = self.adapter(response)
                with patch("home_media_follow.baidu.time.time", return_value=now):
                    with self.assertRaises(NetworkError) as caught:
                        adapter.validate_session()
                self.assertEqual(caught.exception.code, 429)
                self.assertEqual(caught.exception.retry_after, expected)
                self.assertNotIn("private-fixture", str(caught.exception))
                self.assertTrue(stream.closed)

    def test_share_code_error_is_not_account_expiry(self):
        b = self.adapter(lambda p, q, f: {"errno": -9, "msg": "sensitive"} if p == "/share/verify" else None)
        with self.assertRaises(ShareError):
            b.verify_share(SHORT, "abcd")

    def test_ids_are_strings_and_opaque_fingerprint_is_not_changed(self):
        r = normalize_item(item("9007199254740993123"))
        self.assertEqual(r["id"], "9007199254740993123")
        self.assertEqual(r["provider_fingerprint"], "opaque-fingerprint")

    def test_share_walk_is_complete_for_pages_and_bound_subtree(self):
        def response(p, q, f):
            if p == "/share/list":
                page = int(q["page"][0])
                return {"errno": 0, "list": [item(i, f"{i}.mp4") for i in range(1, 101)] if page == 1 else [item(101, "101.mp4")]}
        b = self.adapter(response)
        context = b.verify_share(SHORT, "abcd")
        self.assertEqual(len(b.walk_share(context, "/source")), 101)
        self.assertEqual([q["root"][0] for p, q, f in self.http.calls if p == "/share/list"], ["0", "0"])

    def test_recursive_share_walk_stays_on_enumerated_paths(self):
        def response(p, q, f):
            if p == "/share/list":
                if q["root"] == ["1"]:
                    return {"errno": 0, "list": [item(1, "season", 0, path="", isdir=1)]}
                self.assertEqual(q["dir"], ["/season"])
                return {"errno": 0, "list": [item(2, path="/season")]}
        b = self.adapter(response)
        self.assertEqual([x["id"] for x in b.walk_share(b.verify_share(SHORT))], ["2"])

    def test_repeated_pages_do_not_silently_complete(self):
        b = self.adapter(lambda p, q, f: {"errno": 0, "list": [item(1)], "has_more": 1} if p == "/share/list" else None)
        with self.assertRaises(IncompleteListing):
            b.walk_share(b.verify_share(SHORT), "/source")

    def test_total_count_disagreement_rejects_scan(self):
        b = self.adapter(lambda p, q, f: {"errno": 0, "list": [item(1)], "total": 2, "has_more": 0} if p == "/share/list" else None)
        with self.assertRaises(IncompleteListing):
            b.walk_share(b.verify_share(SHORT), "/source")

    def test_mid_scan_source_mutation_and_foreign_directory_are_rejected(self):
        changing = [123]
        def response(p, q, f):
            if p == "/share/list":
                return {"errno": 0, "list": [item(1, size=changing[0])]}
        b = self.adapter(response)
        c = b.verify_share(SHORT)
        b.list_share(c, "/source")
        changing[0] += 1
        with self.assertRaises(IncompleteListing):
            b.list_share(c, "/source")
        with self.assertRaises(IncompleteListing):
            b.list_share(c, "/other")

    def test_new_secret_invalidates_old_verified_context(self):
        current = ["BDUSS=first"]
        b = self.adapter()
        b.secret_loader = lambda: current[0]
        c = b.verify_share(SHORT, "abcd")
        current[0] = "BDUSS=second"
        with self.assertRaises(AuthRequired):
            b.list_share(c)

    def test_target_list_paginates_and_rejects_repetition(self):
        def response(p, q, f):
            if p == "/api/list":
                page = int(q["page"][0])
                return {"errno": 0, "list": [item(i, str(i), path="/HomeMediaFollow/作品") for i in range(1, 101)] if page == 1 else []}
        b = self.adapter(response)
        self.assertEqual(len(b.list_target("/HomeMediaFollow/作品")), 100)
        self.assertEqual(sum(p == "/api/list" for p, q, f in self.http.calls), 2)

    def test_transfer_has_durable_intent_and_receipt_before_confirmation(self):
        writes = []
        transferred = [False]
        def response(p, q, f):
            if p == "/share/list":
                return {"errno": 0, "list": [item(1)]}
            if p == "/api/list":
                return {"errno": 0, "list": [item(9, path="/HomeMediaFollow/作品")] if transferred[0] else []}
            if p == "/share/transfer":
                self.assertEqual(writes[0]["stage"], "prepared")
                self.assertEqual(f["path"], ["/HomeMediaFollow/作品"])
                self.assertEqual(json.loads(f["fsidlist"][0]), [1])
                self.assertEqual(q["ondup"], ["fail"])
                transferred[0] = True
                return {"errno": 0, "info": [{"to": "/HomeMediaFollow/作品/01.mp4", "to_fs_id": "9"}]}
        b = self.adapter(response, writes.append)
        c = b.verify_share(SHORT)
        b.list_share(c, "/source")
        result = b.transfer_selected(c, ["1"], "/HomeMediaFollow/作品")
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(writes[1]["stage"], "provider_response")
        self.assertNotIn("bdstoken", json.dumps(result))

    def test_transfer_supports_both_native_success_envelopes_and_large_numeric_ids(self):
        source_id = "9007199254740993123"
        destination_id = "9007199254740993124"
        target = "/HomeMediaFollow/作品"
        for shape in ("info", "extra", "both", "conflicting", "duplicate"):
            with self.subTest(shape=shape):
                transferred = [False]
                writes = []
                def response(path, query, form):
                    if path == "/share/list":
                        return {"errno": 0, "list": [item(source_id, fingerprint=None)]}
                    if path == "/api/list":
                        return {"errno": 0, "list": [item(destination_id, path=target, fingerprint=None)] if transferred[0] else []}
                    if path == "/share/transfer":
                        self.assertEqual(json.loads(form["fsidlist"][0]), [int(source_id)])
                        self.assertEqual(form["fsidlist"][0], "[" + source_id + "]")
                        transferred[0] = True
                        old = {"path": target + "/01.mp4", "fsid": destination_id}
                        new = {"to": target + "/01.mp4", "to_fs_id": destination_id,
                               "from": "/source/01.mp4", "from_fs_id": source_id}
                        value = {"errno": 0}
                        if shape in {"info", "both", "conflicting"}:
                            value["info"] = [old]
                        if shape in {"extra", "both", "conflicting", "duplicate"}:
                            if shape == "conflicting":
                                new["to_fs_id"] = "123456"
                            value["extra"] = {"list": [new, new] if shape == "duplicate" else [new]}
                        return value
                adapter = self.adapter(response, writes.append)
                context = adapter.verify_share(SHORT)
                adapter.list_share(context, "/source")
                if shape in {"conflicting", "duplicate"}:
                    with self.assertRaises(TransferUnknown):
                        adapter.transfer_selected(context, [source_id], target)
                    self.assertEqual(len(writes), 1)
                else:
                    result = adapter.transfer_selected(context, [source_id], target)
                    self.assertEqual(result["status"], "confirmed")
                    self.assertEqual(result["files"][0]["id"], destination_id)
                    self.assertEqual(writes[-1]["stage"], "provider_response")
                self.assertEqual(sum(p == "/share/transfer" for p, q, f in self.http.calls), 1)

    def test_write_without_journal_or_unenumerated_selection_is_blocked(self):
        b = self.adapter()
        c = b.verify_share(SHORT)
        with self.assertRaises(BaiduError):
            b.transfer_selected(c, ["1"], "/HomeMediaFollow/作品")
        b.intent_sink = lambda intent: None
        with self.assertRaises(BaiduError):
            b.transfer_selected(c, ["1"], "/HomeMediaFollow/作品")
        self.assertFalse(any(p == "/share/transfer" for p, q, f in self.http.calls))

    def test_conflicting_target_is_not_overwritten(self):
        def response(p, q, f):
            if p == "/share/list":
                return {"errno": 0, "list": [item(1)]}
            if p == "/api/list":
                return {"errno": 0, "list": [item(9, path="/HomeMediaFollow/作品")]}
        b = self.adapter(response, lambda intent: None)
        c = b.verify_share(SHORT)
        b.list_share(c, "/source")
        with self.assertRaises(ConflictError):
            b.transfer_selected(c, ["1"], "/HomeMediaFollow/作品")
        self.assertFalse(any(p == "/share/transfer" for p, q, f in self.http.calls))

    def test_lost_write_response_preserves_unknown_and_never_retries(self):
        intents = []
        def response(p, q, f):
            if p == "/share/list":
                return {"errno": 0, "list": [item(1)]}
            if p == "/share/transfer":
                raise socket.timeout("BDUSS=do-not-log")
        b = self.adapter(response, intents.append)
        c = b.verify_share(SHORT)
        b.list_share(c, "/source")
        with self.assertRaises(TransferUnknown) as caught:
            b.transfer_selected(c, ["1"], "/HomeMediaFollow/作品")
        self.assertEqual(sum(p == "/share/transfer" for p, q, f in self.http.calls), 1)
        self.assertEqual(caught.exception.intent["before_ids"], [])
        self.assertNotIn("do-not-log", str(caught.exception))

    def test_transfer_429_remains_unknown_and_preserves_global_backoff_metadata(self):
        intents = []
        def response(path, query, form):
            if path == "/share/list":
                return {"errno": 0, "list": [item(1)]}
            if path == "/share/transfer":
                raise HTTPError("https://pan.baidu.com/private-fixture", 429, "private-fixture",
                                {"Retry-After": "7200"}, io.BytesIO(b"private-fixture"))
        adapter = self.adapter(response, intents.append)
        context = adapter.verify_share(SHORT)
        adapter.list_share(context, "/source")
        with self.assertRaises(TransferUnknown) as caught:
            adapter.transfer_selected(context, ["1"], "/HomeMediaFollow/作品")
        self.assertEqual(caught.exception.code, 429)
        self.assertEqual(caught.exception.retry_after, 7200)
        self.assertEqual(caught.exception.intent["retry_after"], 7200)
        self.assertNotIn("private-fixture", json.dumps(caught.exception.intent))
        self.assertEqual(sum(path == "/share/transfer" for path, q, f in self.http.calls), 1)
        self.assertEqual(len(intents), 2)
        self.assertEqual(intents[-1]["stage"], "write_unknown")
        self.assertEqual(intents[-1]["cause_code"], 429)
        self.assertEqual(intents[-1]["retry_after"], 7200)

    def test_outcome_journal_failure_never_replays_uncertain_transfer(self):
        intents = []
        def sink(intent):
            intents.append(intent)
            if intent["stage"] == "write_unknown":
                raise OSError("private-fixture")
        def response(path, query, form):
            if path == "/share/list":
                return {"errno": 0, "list": [item(1)]}
            if path == "/share/transfer":
                raise socket.timeout("private-fixture")
        adapter = self.adapter(response, sink)
        context = adapter.verify_share(SHORT)
        adapter.list_share(context, "/source")
        with self.assertRaises(TransferUnknown) as caught:
            adapter.transfer_selected(context, ["1"], "/HomeMediaFollow/作品")
        self.assertEqual(caught.exception.intent["stage"], "write_unknown")
        self.assertNotIn("private-fixture", str(caught.exception))
        self.assertEqual(intents[0]["stage"], "prepared")
        self.assertEqual(sum(path == "/share/transfer" for path, q, f in self.http.calls), 1)

    def test_same_size_target_alone_cannot_confirm_unknown(self):
        b = self.adapter(lambda p, q, f: {"errno": 0, "list": [item(9, path="/HomeMediaFollow/作品", fingerprint=None)]} if p == "/api/list" else None)
        intent = {"target": "/HomeMediaFollow/作品", "files": [{"name": "01.mp4", "size": 123}], "before_ids": []}
        self.assertEqual(b.reconcile_transfer(intent)["status"], "unknown")
        intent["receipt"] = {"items": [{"path": "/HomeMediaFollow/作品/01.mp4", "id": "9"}]}
        self.assertEqual(b.reconcile_transfer(intent)["status"], "confirmed")

    def test_reconciliation_requires_pre_snapshot_for_fingerprint_recovery(self):
        b = self.adapter(lambda p, q, f: {"errno": 0, "list": [item(9, path="/HomeMediaFollow/作品")]} if p == "/api/list" else None)
        intent = {"target": "/HomeMediaFollow/作品", "files": [normalize_item(item(1))]}
        self.assertEqual(b.reconcile_transfer(intent)["status"], "unknown")
        intent["before_ids"] = []
        self.assertEqual(b.reconcile_transfer(intent)["status"], "confirmed")

    def test_outside_target_and_traversal_are_rejected_without_network(self):
        b = self.adapter()
        for target in ["/apps/bdpan/x", "/HomeMediaFollow/../x", "/HomeMediaFollow//作品", "/HomeMediaFollow\\作品"]:
            with self.assertRaises(BaiduError):
                b.list_target(target)
        self.assertFalse(self.http.calls)


class LoginTests(unittest.TestCase):
    def test_login_entry_is_domain_scoped_unique_and_only_clicked_once(self):
        result = {"result": {"value": {"ready": False, "clicked": True}}}
        with patch.object(baidu_auth, "_cdp_call", return_value=result) as call:
            value = baidu_auth._login_page_ready("ws://127.0.0.1/page", True)
            self.assertTrue(value["clicked"])
            script = call.call_args.args[2]["expression"]
            self.assertIn("location.hostname !== 'pan.baidu.com'", script)
            self.assertIn("buttons.length===1", script)
            self.assertIn("e.textContent.trim()==='去登录'", script)
            self.assertIn("if (true)", script)
            baidu_auth._login_page_ready("ws://127.0.0.1/page", False)
            self.assertIn("if (false)", call.call_args.args[2]["expression"])

    def test_cdp_page_target_handshake_does_not_append_empty_query(self):
        class Socket:
            def __init__(self):
                self.sent = []
            def settimeout(self, value):
                pass
            def sendall(self, value):
                self.sent.append(value)
            def makefile(self, mode):
                nonce = base64.b64encode(b"x" * 16).decode()
                accepted = base64.b64encode(hashlib.sha1(
                    (nonce + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
                return io.BytesIO(b"HTTP/1.1 101 Switching Protocols\r\nSec-WebSocket-Accept: " + accepted + b"\r\n\r\n")
            def close(self):
                pass
        for suffix, expected in [("/devtools/page/page-id", b"GET /devtools/page/page-id HTTP/1.1"),
                                 ("/devtools/page/page-id?x=1", b"GET /devtools/page/page-id?x=1 HTTP/1.1")]:
            fake = Socket()
            with patch.object(baidu_auth.socket, "create_connection", return_value=fake), \
                 patch.object(baidu_auth.secrets, "token_bytes", return_value=b"x" * 16):
                c = baidu_auth._CDP("ws://127.0.0.1:12345" + suffix)
                c.close()
            self.assertEqual(fake.sent[0].split(b"\r\n")[0], expected)

    def test_cookie_capture_is_restricted_to_new_profile_baidu_domains(self):
        cookies = [{"domain": ".baidu.com", "name": "BDUSS", "value": "in-memory-only"},
                   {"domain": "evil.example", "name": "unrelated", "value": "private"},
                   {"domain": "pan.baidu.com", "name": "STOKEN", "value": "local"}]
        result = baidu_auth._cookie_header(cookies)
        self.assertIn("STOKEN=local", result)
        self.assertNotIn("private", result)
        self.assertIsNone(baidu_auth._cookie_header([]))

    def test_existing_unrelated_profile_is_refused_without_launch(self):
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as directory:
            (Path(directory) / "unrelated-profile").touch()
            with patch.object(baidu_auth.subprocess, "Popen") as launch:
                with self.assertRaises(baidu_auth.LoginError):
                    baidu_auth.launch_edge_login(lambda value: None, profile_dir=directory,
                                                  screenshot_path=Path(directory) / "login.png")
                launch.assert_not_called()

    def test_symlink_login_profile_rejected_before_launch(self):
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as directory:
            root=Path(directory);(root/'other').mkdir()
            try: (root/'profile').symlink_to(root/'other',target_is_directory=True)
            except OSError: self.skipTest('Symlink creation unavailable')
            with patch.object(baidu_auth.subprocess,'Popen') as launch:
                with self.assertRaises(baidu_auth.LoginError):
                    baidu_auth.launch_edge_login(lambda value: None,profile_dir=root/'profile',screenshot_path=root/'login.png')
            launch.assert_not_called()

    def test_login_passes_secret_only_to_sink_and_never_screenshots_account(self):
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as directory:
            profile = Path(directory) / "dedicated"
            saved = []
            def start(*args, **kwargs):
                (profile / "DevToolsActivePort").write_text("12345\n/devtools/browser/own")
                process = Mock()
                process.wait.return_value = 0
                return process
            def cdp(url, method, params=None):
                if method == "Storage.getCookies":
                    return {"cookies": [{"domain": ".baidu.com", "name": "BDUSS", "value": "local-only"}]}
                self.assertEqual(method, "Browser.close")
                return {}
            with patch.object(baidu_auth, "_edge_executable", return_value="msedge.exe"), \
                 patch.object(baidu_auth.subprocess, "Popen", side_effect=start), \
                 patch.object(baidu_auth, "_validate_cookie", return_value=True), \
                 patch.object(baidu_auth, "_local_json", return_value={"webSocketDebuggerUrl": "ws://127.0.0.1:12345/browser"}), \
                 patch.object(baidu_auth, "_cdp_call", side_effect=cdp):
                result = baidu_auth.launch_edge_login(saved.append, profile_dir=profile,
                    screenshot_path=Path(directory) / "login.png", timeout=6)
            self.assertEqual(result["status"], "session_saved")
            self.assertNotIn("local-only", json.dumps(result))
            self.assertEqual(len(saved), 1)
            self.assertFalse((Path(directory) / "login.png").exists())
            self.assertTrue(profile.exists())


if __name__ == "__main__":
    unittest.main()

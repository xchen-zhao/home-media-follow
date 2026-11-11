import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import unittest
import urllib.parse

from home_media_follow.integrations import Aria2, IntegrationError, Jellyfin, OpenList


PW = "pass" + "word"
GID = "1234567890abcdef"
CLOUD = "/Baidu/HomeMediaFollow/测试作品"
LOCAL = r"D:\BaiduNetdiskDownload\动漫\测试作品\Season 01\测试作品 - S01E01.mp4"


def document(value, status=200):
    return status, {"Content-Type": "application/json"}, json.dumps(value, ensure_ascii=False).encode()


@contextlib.contextmanager
def fixture(route):
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def handle_request(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            body = json.loads(raw) if raw else None
            calls.append({"method": self.command, "path": self.path, "headers": dict(self.headers), "body": body})
            status, headers, content = route(self.command, self.path, body, self.headers)
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, str(value))
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            try:
                self.wfile.write(content)
            except (BrokenPipeError, ConnectionResetError):
                pass
        do_GET = handle_request
        do_POST = handle_request
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class OpenListTests(unittest.TestCase):
    def test_complete_pages_and_once_only_relogin(self):
        login_count, expired = 0, False
        def route(method, path, body, headers):
            nonlocal login_count, expired
            if path == "/api/auth/login":
                login_count += 1
                self.assertEqual(body[PW], "fake-local-login")
                return document({"code": 200, "data": {"token": "token-" + str(login_count)}})
            if not expired:
                expired = True
                return document({"code": 401, "message": "private response must not escape"})
            page = body["page"]
            return document({"code": 200, "data": {"total": 2, "content": [{"name": f"{page}.mp4", "is_dir": False, "size": page}]}})
        with fixture(route) as (base, calls):
            entries = OpenList({"username": "fixture", PW: "fake-local-login"}, base, page_size=1).list(CLOUD, refresh=True)
            self.assertEqual([entry["name"] for entry in entries], ["1.mp4", "2.mp4"])
            self.assertEqual(login_count, 2)
            self.assertTrue(all(call["body"]["refresh"] for call in calls if call["path"] == "/api/fs/list"))

    def test_repeat_page_does_not_return_partial_listing(self):
        def route(method, path, body, headers):
            return document({"code": 200, "data": {"token": "fixture"}}) if path.endswith("login") else document({"code": 200, "data": {"total": 2, "content": [{"name": "same.mp4", "is_dir": False}]}})
        with fixture(route) as (base, _):
            with self.assertRaises(IntegrationError):
                OpenList({"username": "fixture", PW: "fixture"}, base, page_size=1).list(CLOUD)

    def test_download_uses_original_local_path_and_probe_original_raw(self):
        def route(method, path, body, headers):
            return document({"code": 200, "data": {"token": "fixture"}}) if path.endswith("login") else document({"code": 200, "data": {"name": "01.mp4", "size": 100, "raw_url": "https://fixture.invalid/original?private=fake", "sign": "signed:123"}})
        with fixture(route) as (base, _):
            client = OpenList({"username": "fixture", PW: "fixture"}, base)
            url = client.download_url(CLOUD + "/01.mp4")
            self.assertEqual(urllib.parse.unquote(urllib.parse.urlsplit(url).path), "/d" + CLOUD + "/01.mp4")
            self.assertIn("sign=signed%3A123", url)
            self.assertTrue(url.startswith(base))
            self.assertEqual(client.probe_url(CLOUD + "/01.mp4"), "https://fixture.invalid/original?private=fake")

    def test_errors_and_redirects_never_expose_credentials(self):
        def route(method, path, body, headers):
            return 302, {"Location": "https://fixture.invalid/?token=do-not-leak"}, b""
        with fixture(route) as (base, calls):
            client = OpenList({"username": "fixture", PW: "do-not-leak"}, base)
            with self.assertRaises(IntegrationError) as caught:
                client.list(CLOUD)
            self.assertNotIn("do-not-leak", str(caught.exception))
            self.assertNotIn("https://", str(caught.exception))
            self.assertEqual(len(calls), 1)

    def test_mutations_exact_files_and_same_show_only(self):
        mutations = []
        def route(method, path, body, headers):
            if path.endswith("login"):
                return document({"code": 200, "data": {"token": "fixture"}})
            if path.endswith("list"):
                entries = [{"name": "01.mp4", "is_dir": False}, {"name": "directory", "is_dir": True}] if body["path"] == CLOUD else []
                return document({"code": 200, "data": {"content": entries, "total": len(entries)}})
            mutations.append((path, body))
            return document({"code": 200, "data": None})
        with fixture(route) as (base, _):
            client = OpenList({"username": "fixture", PW: "fixture"}, base)
            client.fs("mkdir", {"path": CLOUD + "/_follow_recovery/op"})
            client.fs("move", {"src_dir": CLOUD, "dst_dir": CLOUD + "/_follow_recovery/op", "names": ["01.mp4"]})
            client.fs("remove", {"dir": CLOUD, "names": ["01.mp4"], "follow_seed": False})
            self.assertEqual(len(mutations), 3)
            for body in [
                {"dir": "/Baidu/HomeMediaFollow", "names": ["测试作品"]},
                {"dir": CLOUD, "names": ["../other.mp4"]},
                {"dir": CLOUD, "names": ["directory"]},
                {"dir": CLOUD, "names": ["01.mp4"], "follow_seed": True},
            ]:
                with self.assertRaises((ValueError, IntegrationError)):
                    client.fs("remove", body)
            with self.assertRaises(ValueError):
                client.fs("move", {"src_dir": CLOUD, "dst_dir": "/Baidu/HomeMediaFollow/另一作品", "names": ["01.mp4"]})
            self.assertEqual(len(mutations), 3)

    def test_mutation_lost_response_is_not_blindly_retried(self):
        attempts = 0
        def route(method, path, body, headers):
            nonlocal attempts
            if path.endswith("login"):
                return document({"code": 200, "data": {"token": "fixture"}})
            attempts += 1
            return 200, {"Content-Type": "application/json"}, b"truncated-result"
        with fixture(route) as (base, _):
            client = OpenList({"username": "fixture", PW: "fixture"}, base)
            with self.assertRaises(IntegrationError):
                client.fs("mkdir", {"path": CLOUD + "/_follow_candidates/one"})
            self.assertEqual(attempts, 1)


class Aria2Tests(unittest.TestCase):
    def test_http_400_missing_gid_allows_exactly_one_new_submission(self):
        tasks, adds = {}, []
        def route(method, path, body, headers):
            self.assertEqual(path, "/jsonrpc")
            name, params = body["method"].split(".")[-1], body["params"][1:]
            result = {"jsonrpc": "2.0", "id": body["id"]}
            if name == "tellStatus":
                if params[0] not in tasks:
                    return document({**result, "error": {"code": 1, "message": "GID#1234567890abcdef is not found"}}, 400)
                return document({**result, "result": tasks[params[0]]})
            if name == "addUri":
                adds.append(params)
                tasks[GID] = {"gid": GID, "status": "active", "files": [{"path": "D:/stage/one.mp4"}]}
                return document({**result, "result": GID})
            return document({**result, "result": "OK"})
        with fixture(route) as (base, _):
            client = Aria2("fixture", base)
            self.assertIsNone(client.status(GID))
            for _ in range(2):
                self.assertEqual(client.add(GID, base + "/d/Baidu/one.mp4", "D:/stage", "one.mp4"), GID)
            self.assertEqual(len(adds), 1)

    def test_http_400_rpc_unauthorized_is_not_missing_gid(self):
        def route(method, path, body, headers):
            return document({"jsonrpc": "2.0", "id": body["id"],
                             "error": {"code": 1, "message": "Unauthorized token private-fixture"}}, 400)
        with fixture(route) as (base, _):
            with self.assertRaises(IntegrationError) as caught:
                Aria2("fixture", base).status(GID)
            self.assertEqual(caught.exception.status_code, 400)
            self.assertEqual(caught.exception.rpc_code, 1)
            self.assertFalse(caught.exception.not_found)
            self.assertNotIn("private-fixture", str(caught.exception))

    def test_http_401_403_and_500_never_classify_body_as_missing_gid(self):
        for status in (401, 403, 500):
            with self.subTest(status=status):
                def route(method, path, body, headers):
                    return document({"jsonrpc": "2.0", "id": body["id"],
                                     "error": {"code": 1, "message": "GID is not found private-fixture"}}, status)
                with fixture(route) as (base, _):
                    with self.assertRaises(IntegrationError) as caught:
                        Aria2("fixture", base).status(GID)
                    self.assertEqual(caught.exception.status_code, status)
                    self.assertIsNone(caught.exception.rpc_code)
                    self.assertFalse(caught.exception.not_found)
                    self.assertNotIn("private-fixture", str(caught.exception))

    def test_http_400_invalid_or_oversized_rpc_error_remains_http_failure(self):
        valid = {"jsonrpc": "2.0", "id": "home-media-follow", "error": {"code": 1, "message": "GID is not found"}}
        cases = [b"not-json-private-fixture", json.dumps([]).encode(),
                 json.dumps({"error": valid["error"]}).encode(),
                 json.dumps({**valid, "id": "wrong-request"}).encode(),
                 json.dumps({**valid, "jsonrpc": "1.0"}).encode(),
                 json.dumps({**valid, "result": "ambiguous"}).encode(),
                 json.dumps({**valid, "error": {"code": True, "message": "GID is not found"}}).encode(),
                 json.dumps({**valid, "error": {"code": 1, "message": {}}}).encode(),
                 json.dumps({**valid, "error": {"code": 1, "message": "private-fixture" * 6000}}).encode()]
        for index, content in enumerate(cases):
            with self.subTest(case=index):
                def route(method, path, body, headers):
                    return 400, {"Content-Type": "application/json"}, content
                with fixture(route) as (base, _):
                    with self.assertRaises(IntegrationError) as caught:
                        Aria2("fixture", base).status(GID)
                    self.assertEqual(caught.exception.status_code, 400)
                    self.assertIsNone(caught.exception.rpc_code)
                    self.assertFalse(caught.exception.not_found)
                    self.assertNotIn("private-fixture", str(caught.exception))

    def test_http_400_exception_is_limited_to_aria2_post_rpc_endpoint(self):
        for service, endpoint, method in (("OpenList", "/jsonrpc", "POST"),
                                          ("Aria2", "/other", "POST"),
                                          ("Aria2", "/jsonrpc", "GET")):
            with self.subTest(service=service, endpoint=endpoint, method=method):
                def route(method, path, body, headers):
                    return document({"jsonrpc": "2.0", "id": body["id"],
                                     "error": {"code": 1, "message": "GID is not found"}}, 400)
                with fixture(route) as (base, _):
                    client = Aria2("fixture", base).client
                    client.service = service
                    with self.assertRaises(IntegrationError) as caught:
                        client.request("tellStatus", endpoint, method=method, body={
                            "jsonrpc": "2.0", "id": "fixture", "method": "aria2.tellStatus"})
                    self.assertEqual(caught.exception.status_code, 400)
                    self.assertIsNone(caught.exception.rpc_code)
                    self.assertFalse(caught.exception.not_found)

    def test_idempotent_add_and_signed_url_not_in_status(self):
        tasks, adds = {}, []
        def route(method, path, body, headers):
            name, params = body["method"].split(".")[-1], body["params"][1:]
            if name == "tellStatus":
                if params[0] not in tasks:
                    return document({"error": {"code": 1, "message": "GID is not found"}})
                return document({"result": tasks[params[0]]})
            if name == "addUri":
                adds.append(params)
                options = params[1]
                tasks[GID] = {"gid": GID, "status": "active", "files": [{"path": options["dir"] + "/" + options["out"], "uris": [{"uri": params[0][0]}]}], "totalLength": "100", "completedLength": "1", "downloadSpeed": "1"}
                return document({"result": GID})
            return document({"result": "OK"})
        with fixture(route) as (base, _):
            client = Aria2("fixture-secret", base)
            url = base + "/d/Baidu/file.mp4?sign=private-fixture"
            for _ in range(2):
                self.assertEqual(client.add(GID, url, r"D:\Media\Downloads\_follow\one", "one.mp4"), GID)
            self.assertEqual(len(adds), 1)
            self.assertEqual(adds[0][1]["allow-overwrite"], "false")
            status = client.status(GID)
            self.assertNotIn("files", status)
            self.assertNotIn("private-fixture", json.dumps(status))

    def test_lost_add_response_reconciles_without_second_add(self):
        submitted, adds = False, 0
        def route(method, path, body, headers):
            nonlocal submitted, adds
            name = body["method"].split(".")[-1]
            if name == "tellStatus":
                return document({"result": {"gid": GID, "status": "active", "files": [{"path": "D:/stage/one.mp4"}]}}) if submitted else document({"error": {"code": 1, "message": "GID is not found"}})
            if name == "addUri":
                adds += 1; submitted = True
                return 200, {"Content-Type": "application/json"}, b"truncated-response"
            return document({"result": "OK"})
        with fixture(route) as (base, _):
            client = Aria2("fixture", base)
            self.assertEqual(client.add(GID, base + "/d/Baidu/one.mp4?sign=fixture", "D:/stage", "one.mp4"), GID)
            self.assertEqual(adds, 1)

    def test_existing_different_path_and_raw_provider_url_rejected(self):
        def route(method, path, body, headers):
            return document({"result": {"gid": GID, "status": "active", "files": [{"path": "D:/other/one.mp4"}]}})
        with fixture(route) as (base, calls):
            client = Aria2("fixture", base)
            with self.assertRaises(IntegrationError):
                client.add(GID, base + "/d/Baidu/one.mp4", "D:/stage", "one.mp4")
            with self.assertRaises(ValueError):
                client.add(GID, "https://provider.invalid/original", "D:/stage", "one.mp4")
            self.assertFalse(any(call["body"]["method"].endswith("addUri") for call in calls))

    def test_rpc_errors_are_sanitized_and_not_mistaken_for_absence(self):
        def route(method, path, body, headers):
            return document({"error": {"code": 1, "message": "Unauthorized token private-fixture https://provider.invalid/secret"}})
        with fixture(route) as (base, _):
            with self.assertRaises(IntegrationError) as caught:
                Aria2("fixture", base).status(GID)
            self.assertFalse(caught.exception.not_found)
            self.assertNotIn("private-fixture", str(caught.exception))


class JellyfinTests(unittest.TestCase):
    def test_modern_authorization_is_used_for_listing_refresh_and_original_stream(self):
        expected = ('MediaBrowser Client="HomeMedia Follow", Device="Windows", '
                    'DeviceId="home-media-follow", Version="0.1.0", Token="fixture-key"')
        def route(method, path, body, headers):
            if headers.get("Authorization") != expected:
                return document({"error": "Unauthorized"}, 401)
            self.assertIsNone(headers.get("X-Emby-Token"))
            self.assertNotIn("fixture-key", path)
            if path.startswith("/Items?"):
                return document({"TotalRecordCount": 1, "Items": [{"Id": "one", "Path": LOCAL,
                    "MediaStreams": [{"Type": "Video", "Codec": "hevc", "Width": 3840, "Height": 2160}]}]})
            if path.startswith("/Items/one/Refresh") or path == "/Library/Refresh":
                return 204, {}, b""
            if path.startswith("/Videos/one/stream"):
                return 206, {"Content-Type": "video/mp4"}, b"video-data"
            self.fail("Unexpected authenticated request")
        with fixture(route) as (base, _):
            client = Jellyfin("fixture-key", base)
            self.assertEqual(client.refresh(LOCAL)["status"], "requested")
            self.assertTrue(client.verify(LOCAL)["playable"])
            self.assertEqual(client.refresh()["status"], "requested")

    def test_invalid_authorization_keys_are_rejected_without_exposure(self):
        for key in (None, "", 'private"fixture', "private\\fixture", "private\r\nfixture",
                    "private\x00fixture", "private\tfixture", "private fixture", "private\x7ffixture", "privateéfixture"):
            with self.subTest(kind=type(key).__name__):
                with self.assertRaises(IntegrationError) as caught:
                    Jellyfin(key)
                self.assertEqual(caught.exception.operation, "credentials")
                self.assertNotIn("private", str(caught.exception))

    def test_multiple_video_tracks_require_exactly_one_default(self):
        for flags in ((False, False), (True, True), ("true", False)):
            with self.subTest(defaults=flags):
                def route(method, path, body, headers):
                    self.assertTrue(path.startswith("/Items?"))
                    return document({"TotalRecordCount": 1, "Items": [{"Id": "one", "Path": LOCAL, "MediaStreams": [
                        {"Type": "Video", "Index": 0, "Codec": "hevc", "Width": 3840, "Height": 2160, "IsDefault": flags[0]},
                        {"Type": "Video", "Index": 1, "Codec": "h264", "Width": 1920, "Height": 1080, "IsDefault": flags[1]},
                    ]}]})
                with fixture(route) as (base, calls):
                    result = Jellyfin("fixture", base).verify(LOCAL)
                    self.assertEqual(result["reason"], "ambiguous_video_stream")
                    self.assertFalse(result["playable"])
                    self.assertEqual(len(calls), 1)

    def test_unique_default_video_is_used_after_excluding_still_images(self):
        def route(method, path, body, headers):
            if path.startswith("/Items?"):
                return document({"TotalRecordCount": 1, "Items": [{"Id": "one", "Path": LOCAL, "MediaStreams": [
                    {"Type": "Video", "Index": 0, "Codec": "webp", "Width": 7680, "Height": 4320, "IsDefault": True},
                    {"Type": "Video", "Index": 1, "Codec": "hevc", "Width": 3840, "Height": 2160},
                    {"Type": "Video", "Index": 2, "Codec": "h264", "Width": 1920, "Height": 1080, "IsDefault": True},
                ]}]})
            return 206, {"Content-Type": "video/mp4"}, b"video-data"
        with fixture(route) as (base, _):
            result = Jellyfin("fixture", base).verify(LOCAL)
            self.assertTrue(result["playable"])
            self.assertEqual((result["width"], result["height"]), (1920, 1080))

    def test_original_media_source_is_selected_explicitly(self):
        def route(method, path, body, headers):
            if path.startswith("/Items?"):
                return document({"TotalRecordCount": 1, "Items": [{"Id": "selected", "Path": LOCAL, "MediaSources": [
                    {"Id": "wrong-version", "Path": "D:/other.mp4", "MediaStreams": [{"Type": "Video", "Width": 7680, "Height": 4320}]},
                    {"Id": "exact-version", "Path": LOCAL, "MediaStreams": [{"Type": "Video", "Width": 1920, "Height": 1080, "Codec": "h264"}]},
                ]}]})
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
            self.assertEqual(query["MediaSourceId"], ["exact-version"])
            return 206, {"Content-Type": "video/mp4", "Content-Range": "bytes 0-9/1000"}, b"video-data"
        with fixture(route) as (base, _):
            result = Jellyfin("fixture", base).verify(LOCAL)
            self.assertEqual(result["quality_tier"], "1080p_fallback")
            self.assertTrue(result["playable"])

    def test_hls_manifest_does_not_count_as_original_playback(self):
        def route(method, path, body, headers):
            if path.startswith("/Items?"):
                return document({"TotalRecordCount": 1, "Items": [{"Id": "one", "Path": LOCAL, "MediaStreams": [{"Type": "Video", "Width": 3840, "Height": 2160}]}]})
            return 200, {"Content-Type": "application/vnd.apple.mpegurl"}, b"#EXTM3U"
        with fixture(route) as (base, _):
            result = Jellyfin("fixture", base).verify(LOCAL)
            self.assertEqual(result["status"], "indexed")
            self.assertFalse(result["playable"])

    def test_full_path_pagination_and_real_bounded_original_probe(self):
        def route(method, path, body, headers):
            parsed = urllib.parse.urlsplit(path)
            self.assertEqual(headers.get("Authorization"), 'MediaBrowser Client="HomeMedia Follow", Device="Windows", DeviceId="home-media-follow", Version="0.1.0", Token="fixture-key"')
            self.assertIsNone(headers.get("X-Emby-Token"))
            self.assertNotIn("fixture-key", path)
            if parsed.path == "/Items":
                start = int(urllib.parse.parse_qs(parsed.query)["StartIndex"][0])
                item = {"Id": "other", "Path": "D:/other.mp4"} if start == 0 else {"Id": "selected", "Path": LOCAL, "MediaStreams": [{"Type": "Video", "Index": 0, "Codec": "hevc", "Width": 3840, "Height": 1600}]}
                return document({"TotalRecordCount": 2, "Items": [item]})
            self.assertEqual(parsed.path, "/Videos/selected/stream")
            self.assertEqual(urllib.parse.parse_qs(parsed.query), {"static": ["true"]})
            self.assertEqual(headers.get("Range"), "bytes=0-65535")
            # Deliberately ignore Range: client must still stop after 64 KiB.
            return 200, {"Content-Type": "video/mp4"}, b"video" * 30000
        with fixture(route) as (base, _):
            result = Jellyfin("fixture-key", base, page_size=1).verify(LOCAL.lower().replace("\\", "/"))
            self.assertEqual(result["status"], "verified")
            self.assertTrue(result["playable"])
            self.assertEqual(result["probe_bytes"], 65536)

    def test_no_path_match_is_pending_without_playback(self):
        def route(method, path, body, headers):
            return document({"TotalRecordCount": 1, "Items": [{"Id": "one", "Path": "D:/different.mp4"}]})
        with fixture(route) as (base, calls):
            self.assertEqual(Jellyfin("fixture", base).verify(LOCAL)["status"], "pending")
            self.assertEqual(len(calls), 1)

    def test_4k_cover_does_not_admit_720p_video(self):
        def route(method, path, body, headers):
            return document({"TotalRecordCount": 1, "Items": [{"Id": "one", "Path": LOCAL, "MediaStreams": [{"Type": "Video", "Codec": "mjpeg", "Width": 3840, "Height": 2160}, {"Type": "Video", "Codec": "hevc", "Width": 1280, "Height": 720}]}]})
        with fixture(route) as (base, calls):
            result = Jellyfin("fixture", base).verify(LOCAL)
            self.assertEqual(result["status"], "indexed")
            self.assertFalse(result["playable"])
            self.assertEqual(len(calls), 1)

    def test_refresh_new_unindexed_file_requests_library_scan(self):
        def route(method,path,body,headers):
            if path.startswith('/Items?'):return document({'TotalRecordCount':0,'Items':[]})
            if method=='POST' and path=='/Library/Refresh':return 204,{},b''
            self.fail('Unexpected request')
        with fixture(route) as (base,calls):
            self.assertEqual(Jellyfin('fixture-key',base).refresh(LOCAL),{'status':'requested','scope':'library'})
        self.assertTrue(any(c['method']=='POST' and c['path']=='/Library/Refresh' for c in calls))

    def test_refresh_ambiguous_path_does_not_request_scan(self):
        def route(method,path,body,headers):
            if path.startswith('/Items?'):
                return document({'TotalRecordCount':2,'Items':[{'Id':'one','Path':LOCAL},{'Id':'two','Path':LOCAL}]})
            self.fail('Ambiguous path must not cause any refresh mutation')
        with fixture(route) as (base,calls):
            with self.assertRaises(IntegrationError) as caught:Jellyfin('fixture-key',base).refresh(LOCAL)
            self.assertEqual(caught.exception.operation,'ambiguous_path')
        self.assertFalse(any(c['method']=='POST' for c in calls))

    def test_refresh_and_playback_auth_failure(self):
        def route(method, path, body, headers):
            if path.startswith("/Items?"):
                return document({"TotalRecordCount": 1, "Items": [{"Id": "one", "Path": LOCAL, "MediaStreams": [{"Type": "Video", "Codec": "hevc", "Width": 1920, "Height": 1080}]}]})
            if path.startswith("/Videos/"):
                return document({"message": "private-fixture"}, 401)
            return 204, {}, b""
        with fixture(route) as (base, calls):
            client = Jellyfin("fixture-key", base)
            self.assertEqual(client.refresh(LOCAL)["status"], "requested")
            self.assertTrue(any(call["method"] == "POST" and call["path"].startswith("/Items/one/Refresh") for call in calls))
            with self.assertRaises(IntegrationError) as caught:
                client.verify(LOCAL)
            self.assertEqual(caught.exception.status_code, 401)
            self.assertNotIn("private-fixture", str(caught.exception))

    def test_api_credentials_and_non_loopback_control_endpoints_fail_closed(self):
        with self.assertRaises(IntegrationError):
            Jellyfin("")
        with self.assertRaises(ValueError):
            Jellyfin("fixture", "https://remote.invalid")


if __name__ == "__main__":
    unittest.main()

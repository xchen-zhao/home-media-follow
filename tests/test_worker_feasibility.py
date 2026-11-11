"""Local fault fixtures for real client contracts and recovery boundaries."""
import json
import hashlib
import os
from contextlib import nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory, gettempdir
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from unittest.mock import MagicMock, Mock

from home_media_follow import hook
from home_media_follow.core import logical_key
from home_media_follow.integrations import Aria2, IntegrationError, Jellyfin
from home_media_follow.store import Store
from home_media_follow.worker import Worker


def candidate():
    return {
        "show_id": "example", "season": 1, "episode": 3, "part": None,
        "candidate_id": "candidate-example", "name": "episode.mp4", "size": 4,
        "source_path": "/Example/4K/episode.mp4", "source_id": "source-3",
        "cloud": {"path": "/HomeMediaFollow/Example/episode.mp4"},
        "probe": {"status": "verified", "width": 3840, "height": 2160, "duration": 1200},
    }


def temporary():
    # macOS /var is itself a symlink; replacement deliberately rejects reparse ancestors.
    return TemporaryDirectory(dir=Path(gettempdir()).resolve())


def worker(root):
    result = Worker.__new__(Worker)
    result.root = Path(root)
    result.clock = lambda:1000
    result.store = Store(result.root / "state")
    result.config = {"staging": str(result.root / "_follow"), "min_free_bytes": 0,
                     "publisher_lock": str(result.root / "publish.lock"),
                     "legacy_receipts": str(result.root / "legacy-receipts")}
    result.shows = {"example": {"show_id": "example", "title": "Example", "cloud_path": "/HomeMediaFollow/Example"}}
    result._destination = lambda *args: result.root / "media" / "episode.mp4"
    result.file_probe = lambda _: candidate()["probe"]
    result.openlist = SimpleNamespace(download_url=lambda _: "http://127.0.0.1:5244/d/Baidu/test.mp4")
    return result


def jellyfin(path, streams):
    result = Jellyfin("fixture-only")
    item = {"Id": "item-example", "Path": str(path), "MediaStreams": streams}
    result.client.request = lambda *args, **kwargs: {"Items": [item], "TotalRecordCount": 1}
    response = MagicMock()
    response.__enter__.return_value = response
    response.status = 206
    response.headers.get_content_type.return_value = "video/mp4"
    response.read.return_value = b"video fixture"
    result.client.opener = SimpleNamespace(open=lambda *args, **kwargs: response)
    return result


class WorkerFeasibilityTests(unittest.TestCase):
    def test_worker_accepts_native_jellyfin_quality_result(self):
        with temporary() as directory:
            subject = worker(directory)
            path = subject.root / "media" / "episode.mp4"
            path.parent.mkdir()
            path.write_bytes(b"good")
            subject.jellyfin = jellyfin(path, [{"Type": "Video", "Index": 0, "IsDefault": True,
                                             "Codec": "hevc", "Width": 3840, "Height": 2160}])
            subject.cloud = SimpleNamespace(confirm=lambda _: None)
            job = {"download_id": "job", "show_id": "example", "candidate": candidate(),
                   "destination": str(path), "status": "published", "refresh_requested_at": "prior",
                   "logical_key": logical_key(candidate())}
            with subject.store.locked():
                subject.store.state["downloads"]["job"] = job
                subject.store.state["subscriptions"]["example"] = {"assets": {
                    job["logical_key"]: {"candidate": candidate(), "sha256": hashlib.sha256(b"good").hexdigest()}}}
                subject._verify_jellyfin(job)
            self.assertEqual(job["status"], "complete")

    def test_pending_refresh_does_not_consume_marker_and_retries_at_recovery_cadence(self):
        with temporary() as directory:
            subject=worker(directory);clock=[1000];subject.clock=lambda:clock[0]
            refresh=Mock(side_effect=[{'status':'pending'},{'status':'requested'}])
            subject.jellyfin=SimpleNamespace(refresh=refresh,verify=lambda _: {'status':'pending','playable':False})
            job={'download_id':'job','show_id':'example','candidate':candidate(),'destination':str(subject.root/'new.mp4'),'status':'published'}
            with subject.store.locked():
                subject.store.state['downloads']['job']=job
                subject._verify_jellyfin(job)
                self.assertNotIn('refresh_requested_at',job)
                subject._verify_jellyfin(job)
                self.assertEqual(refresh.call_count,1)
                clock[0]+=900;subject._verify_jellyfin(job)
                self.assertEqual(refresh.call_count,2)
                self.assertIn('refresh_requested_at',job)
                self.assertEqual(job['status'],'published')

    def test_metadata_write_failure_is_retried_then_verified_without_touching_other_file(self):
        with temporary() as directory:
            subject=worker(directory);clock=[1000];subject.clock=lambda:clock[0]
            subject.shows['example']['media_kind']='tv'
            path=subject.root/'media'/'episode.mp4';path.parent.mkdir();path.write_bytes(b'good')
            unrelated=path.parent/'unrelated.nfo';unrelated.write_bytes(b'untouched')
            write=Mock(side_effect=[OSError('temporary metadata error'),None])
            subject.publisher=lambda:SimpleNamespace(write_metadata=write,create_poster=Mock())
            verify=Mock(side_effect=[{'status':'pending','playable':False},{'status':'verified','playable':True,'width':3840,'height':2160,'item_id':'fixture'}])
            subject.jellyfin=SimpleNamespace(refresh=Mock(return_value={'status':'requested'}),verify=verify)
            subject.cloud=SimpleNamespace(confirm=lambda _:None)
            job={'download_id':'job','show_id':'example','candidate':candidate(),'destination':str(path),
                 'status':'published','metadata_status':'retry_pending','logical_key':logical_key(candidate())}
            with subject.store.locked():
                subject.store.state['downloads']['job']=job
                subject.store.state['subscriptions']['example']={'assets':{job['logical_key']:{'candidate':candidate(),'destination':str(path),'sha256':hashlib.sha256(b'good').hexdigest()}}}
                subject._verify_jellyfin(job)
                self.assertEqual(job['metadata_status'],'retry_pending')
                self.assertEqual(job['status'],'published')
                self.assertEqual(verify.call_count,0)
                subject._verify_jellyfin(job);self.assertEqual(write.call_count,1)
                clock[0]+=900;subject._verify_jellyfin(job)
                self.assertEqual(job['metadata_status'],'written')
                self.assertEqual(write.call_count,2)
                self.assertEqual(job['status'],'published')
                subject._verify_jellyfin(job)
                self.assertEqual(job['status'],'complete')
            self.assertEqual(unrelated.read_bytes(),b'untouched')
            self.assertEqual(path.read_bytes(),b'good')

    def test_jellyfin_does_not_select_ambiguous_video_stream(self):
        client = jellyfin("D:/media/episode.mp4", [
            {"Type": "Video", "Index": 0, "Codec": "hevc", "Width": 3840, "Height": 2160},
            {"Type": "Video", "Index": 1, "Codec": "hevc", "Width": 1920, "Height": 1080},
        ])
        self.assertNotEqual(client.verify("D:/media/episode.mp4")["status"], "verified")

    def test_queue_uses_native_aria2_client_contract(self):
        with temporary() as directory:
            subject = worker(directory)
            client = Aria2("fixture-only")
            calls = []

            def rpc(method, params=None):
                calls.append((method, params))
                if method == "tellStatus":
                    raise IntegrationError("Aria2", method, rpc_code=1, not_found=True)
                if method == "addUri":
                    return params[1]["gid"]
                return "OK"

            client.call = rpc
            subject.aria2 = client
            subscription = {"request_id": "request-example", "assets": {}}
            # The fixture uses a real local temporary directory. Production retains
            # its Windows absolute-drive check; still exercise the actual str guard.
            absolute_guard = (patch("home_media_follow.integrations.ntpath.isabs",
                                    side_effect=lambda value: isinstance(value,str) and Path(value).is_absolute())
                              if os.name != "nt" else nullcontext())
            with subject.store.locked(), absolute_guard:
                subject._queue(subject.shows["example"], subscription, candidate(), None)
            self.assertEqual(len([c for c in calls if c[0] == "addUri"]), 1)
            self.assertEqual(next(iter(subject.store.state["downloads"].values()))["status"], "queued")

    def test_unregistered_same_size_video_is_not_adopted(self):
        with temporary() as directory:
            subject = worker(directory)
            destination = subject._destination(None, None, None)
            destination.parent.mkdir()
            destination.write_bytes(b"WRNG")
            subject.aria2 = SimpleNamespace(add=lambda *args: self.fail("Must resolve existing-file conflict first"))
            subscription = {"request_id": "request-example", "assets": {}}
            with subject.store.locked():
                try:
                    subject._queue(subject.shows["example"], subscription, candidate(), None)
                except ValueError:
                    pass
            self.assertNotIn(logical_key(candidate()), subscription["assets"])
            self.assertFalse(any(j.get("status") == "adopted" for j in subject.store.state["downloads"].values()))
            self.assertEqual(destination.read_bytes(), b"WRNG")

    def test_resume_subscription_leaves_authorized_download_running(self):
        with temporary() as directory:
            subject = worker(directory)
            requests = subject.root / "requests"
            requests.mkdir()
            request_id = "64c3b384-ab5b-421e-a1b0-c6f19cb038a3"
            envelope = {"request_id": request_id, "action": "resume", "show_ids": ["example"],
                        "policy_revision": 3, "created_at": "2026-11-07T00:00:00+00:00"}
            (requests / (request_id + ".json")).write_text(json.dumps(envelope))
            calls = []

            def rpc(method, params=None):
                calls.append(method)
                if method in {"pause", "unpause", "remove"}:
                    raise IntegrationError("Aria2", method, rpc_code=1)
                return "OK"

            subject.aria2 = SimpleNamespace(
                status=lambda _: {"gid": "1234567890abcdef", "status": "active"}, call=rpc)
            with subject.store.locked():
                subject.store.state["subscriptions"]["example"] = {
                    "show_id": "example", "status": "paused", "assets": {}, "activation": {"scan_id": "old"}}
                subject.store.state["downloads"]["job"] = {
                    "show_id": "example", "status": "downloading", "gid": "1234567890abcdef"}
                subject.store.event("fixture_active_download", {})
                subject.inbox()
            self.assertFalse(set(calls) & {"pause", "unpause", "remove"})
            self.assertEqual(subject.store.state["downloads"]["job"]["status"], "downloading")
            self.assertEqual(subject.store.state["requests"][request_id]["status"], "applied")

    def test_missing_manifest_uppercase_managed_path_never_calls_legacy(self):
        with temporary() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            (root / "config" / "shows.json").write_text(json.dumps({"staging": str(root / "_follow")}))
            source = str(root / "_FOLLOW" / "job" / "episode.mp4")
            with patch.object(hook.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as legacy:
                result = hook.main(["1234567890abcdef", "1", source], root=root)
            self.assertEqual(result, 1)
            legacy.assert_not_called()


if __name__ == "__main__":
    unittest.main()

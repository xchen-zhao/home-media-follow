"""User semantics against the real Worker; every external boundary is a fixture."""
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import tempfile
import unittest
from unittest.mock import patch
import uuid

from home_media_follow.core import candidate_identity, logical_key, resolution_rank
from home_media_follow.cloud import CloudVersions
from home_media_follow.catalog import Catalog, binding_fingerprint
from home_media_follow.integrations import Aria2, IntegrationError
from home_media_follow.worker import Worker


def probe(width=3840, height=2160, status="verified"):
    return {"status": status, "width": width, "height": height, "duration": 1800}


def row(episode, quality="4K", part=None):
    name = (f"第{episode}期（{part}）_{quality}" if part else f"{episode}_{quality}") + ".mkv"
    return {"id": f"{episode}-{quality}-{part or 'whole'}", "name": name, "path": "/source/" + name,
            "size": 100, "is_dir": False, "provider_fingerprint": "opaque:" + name}


class FixtureSecrets:
    def load(self):
        return {"extraction_codes": {}}


class FixtureProvider:
    def __init__(self):
        self.rows = []
        self.verifies = []

    def verify_share(self, short, code):
        self.verifies.append(short)
        return {"short_url": short}

    def list_share(self, context, directory, page=1):
        return [{"id": "fixture-root", "is_dir": True, "path": "/source"}] if directory == "/" else self.rows

    def walk_share(self, context, root):
        return copy.deepcopy(self.rows)

    def list_target(self, path):
        return [{**copy.deepcopy(item), "path": "/HomeMediaFollow/Test/" + item["name"]} for item in self.rows]


class FixtureOpenList:
    def __init__(self, provider):
        self.provider, self.list_calls = provider, []

    def list(self, path: str, refresh: bool = False) -> list[dict]:
        if not path.startswith('/Baidu/'):
            raise ValueError('Expected an absolute Baidu mount path')
        self.list_calls.append((path, refresh))
        cloud_path = path[len('/Baidu'):]
        # Return the provider's current listing, not a cached pre-promotion copy.
        return [copy.deepcopy(item) for item in self.provider.list_target(cloud_path)
                if str(PurePosixPath(item['path']).parent) == cloud_path]

    def download_url(self, path):
        return "http://127.0.0.1/d/" + path.lstrip("/")


class FixtureAria2(Aria2):
    """Keep the real add/status validation, replace only RPC I/O."""
    def __init__(self):
        self.calls, self.jobs = [], {}

    def call(self, method, params=None):
        params = params or []
        self.calls.append((method, copy.deepcopy(params)))
        if method == "tellStatus":
            if params[0] not in self.jobs:
                raise IntegrationError("Aria2", method, rpc_code=1, not_found=True)
            return copy.deepcopy(self.jobs[params[0]])
        if method == "addUri":
            options = params[1]; gid = options["gid"]
            self.jobs[gid] = {"gid": gid, "status": "waiting", "completedLength": "0", "downloadSpeed": "0",
                              "files": [{"path": str(Path(options["dir"]) / options["out"])}]}
            return gid
        if method in {"pause", "unpause", "remove"}:
            self.jobs[params[0]]["status"] = {"pause": "paused", "unpause": "waiting", "remove": "removed"}[method]
        return "OK"

    def add(self, gid, url, stage_dir, filename):
        if not isinstance(stage_dir, str):
            raise ValueError("Worker must pass the real Aria2 string contract")
        # Fixtures live on macOS; RPC validation still receives a Windows path.
        return super().add(gid, url, "D:\\fixture\\" + Path(stage_dir).name, filename)


class FixtureCloud:
    def __init__(self):
        self.selections, self.confirmations = [], []

    def select(self, show, key, selected, previous=None):
        self.selections.append({"key": key, "selected": copy.deepcopy(selected), "previous": copy.deepcopy(previous)})
        return None

    def confirm(self, operation):
        self.confirmations.append(operation)


class FixtureJellyfin:
    def refresh(self, destination):
        return {"status": "requested"}

    def verify(self, destination):
        # Same public schema as the real Jellyfin client.
        return {"status": "verified", "playable": True, "quality_tier": "1080p_fallback",
                "width": 1920, "height": 1080, "probe_bytes": 64, "item_id": "fixture-item"}


class WorkerExperienceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / "config").mkdir()
        self.show = {"show_id": "test", "title": "Test", "media_kind": "anime", "cloud_path": "/HomeMediaFollow/Test",
                     "season": 1, "episode_mode": "absolute", "source": {"short_url": "fixtureShare", "source_root": "/source"}}
        config = {"shows": [self.show], "staging": str(self.root / "staging"), "media_root": str(self.root / "media"),
                  "publisher_lock": str(self.root / "publish.lock"), "recovery_root": str(self.root / "recovery"),
                  "min_free_bytes": 0, "quality_retry_seconds": 900, "scan_hour": 18,
                  "catalog_url": "https://app.yinxiang.com/fx/00000000-0000-4000-8000-000000000000"}
        (self.root / "config" / "shows.json").write_text(json.dumps(config))
        self.provider, self.aria = FixtureProvider(), FixtureAria2()
        self.worker = Worker(self.root, provider=self.provider, openlist=FixtureOpenList(self.provider), aria2=self.aria,
                             secret_store=FixtureSecrets(), jellyfin=FixtureJellyfin(), file_probe=lambda _: probe(), clock=lambda: 200000)
        self.cloud = FixtureCloud(); self.worker.cloud = self.cloud
        self.probes = {}
        def measured(show, candidate, context, targets):
            fallback = probe(1920, 1080) if "1080" in candidate["name"] else probe()
            candidate["probe"] = copy.deepcopy(self.probes.get(candidate["name"], fallback))
            candidate["cloud"] = next(item for item in self.provider.list_target(show["cloud_path"]) if item["name"] == candidate["name"])
            candidate["managed_cloud"] = True
        self.worker._probe_candidate = measured
        self.worker._destination = lambda show, item, current=None: Path(current["destination"]) if current else self.root / "media" / (logical_key(item).replace(":", "_") + ".mkv")
        self.catalog_patcher = patch("home_media_follow.catalog.Catalog.check", return_value={})
        self.catalog_patcher.start()
        self.addCleanup(self.catalog_patcher.stop)

    def active(self):
        sub = {"show_id": "test", "status": "active", "assets": {}, "needs_activation": True, "request_id": str(uuid.uuid4())}
        self.worker.store.state["subscriptions"]["test"] = sub
        return sub

    def test_complete_scan_excludes_bonus_pending_without_changing_requests(self):
        sub=self.active();sub['needs_activation']=False
        sub['activation']={'show_id':'test','scan_id':'old','watermark':{'season':1,'episode':10},'initial_selected':[]}
        old={'record_type':'candidate','show_id':'test','season':1,'episode':4,'part':None,
             'name':'第4期超前彩蛋.mp4','source_path':'/source/第4期超前彩蛋.mp4','source_id':'old','size':10}
        cid=candidate_identity(old);old['candidate_id']=cid;key=logical_key(old)
        sub['scan_candidates']=[cid];sub['pending_quality']={key:{'reason':'no_verified_1080p_candidate'},
            'test:S01:E0003:':{'reason':'source_missing'}}
        self.provider.rows=[row(10)]
        with self.worker.store.locked():
            self.worker.store.state['inventory'][cid]=old
            self.worker._source(self.show,sub)
        self.assertNotIn(key,sub['pending_quality'])
        self.assertIn('test:S01:E0003:',sub['pending_quality'])
        self.assertEqual(old['exclusion_reason'],'non_regular_extra')
        self.assertNotIn(cid,sub['scan_candidates'])
        self.assertEqual(sub['activation']['watermark']['episode'],10)
        self.assertFalse(self.worker.store.state['downloads'])
        self.assertFalse(self.worker.store.state['requests'])
        self.assertFalse(self.aria.calls)

    def test_import_1313_observations_creates_no_request_or_download(self):
        items = [{"show_id": "test", "cloud_path": f"/HomeMediaFollow/Test/{i}.mkv", "size": 100} for i in range(1313)]
        (self.root / "config" / "observed.json").write_text(json.dumps({"observations": items}))
        with self.worker.store.locked():
            self.worker.import_observed()
        self.assertEqual(len(self.worker.store.state["inventory"]), 1313)
        self.assertEqual(self.worker.store.state["requests"], {})
        self.assertEqual(self.worker.store.state["downloads"], {})
        self.assertEqual(self.worker.store.state["subscriptions"], {})
        self.assertEqual(self.provider.verifies, [])
        self.assertEqual(self.aria.jobs, {})

    def test_latest_watermark_adds_new_missed_episode_not_old_backfill(self):
        self.provider.rows = [row(1), row(102)]
        with self.worker.store.locked():
            sub = self.active(); self.worker._process_show(self.show, sub, True)
            self.assertEqual(sub["activation"]["watermark"]["episode"], 102)
            self.provider.rows += [row(101), row(103)]
            self.worker._process_show(self.show, sub, True)
        self.assertEqual({j["candidate"]["episode"] for j in self.worker.store.state["downloads"].values()}, {102, 103})
        self.assertIn(('/Baidu/HomeMediaFollow/Test', True), self.worker.openlist.list_calls,
                      'A selected cloud version must refresh its actual show listing')
        self.assertEqual({entry['name'] for entry in self.worker.openlist.list('/Baidu/HomeMediaFollow/Test', refresh=True)},
                         {item['name'] for item in self.provider.rows})

    def test_latest_variety_keeps_both_published_parts(self):
        self.show["media_kind"] = "variety"
        self.provider.rows = [row(4, part="下"), row(5, part="上"), row(5, part="下")]
        with self.worker.store.locked():
            sub = self.active(); self.worker._process_show(self.show, sub, True)
        jobs = list(self.worker.store.state["downloads"].values())
        self.assertEqual({(j["candidate"]["episode"], j["candidate"]["part"]) for j in jobs}, {(5, "上"), (5, "下")})
        self.assertEqual(len({j["destination"] for j in jobs}), 2)

    def test_unknown_4k_fallback_does_not_retire_unverified_cloud_version(self):
        self.provider.rows = [row(10), row(10, "1080")]
        self.probes[self.provider.rows[0]["name"]] = probe(status="unknown")
        with self.worker.store.locked():
            sub = self.active(); self.worker._process_show(self.show, sub, True)
        jobs = list(self.worker.store.state["downloads"].values())
        self.assertEqual(len(jobs), 1)
        self.assertEqual(resolution_rank(jobs[0]["candidate"])[0], 1)
        for switch in self.cloud.selections:
            previous = switch["previous"]
            self.assertTrue(previous is None or (resolution_rank(previous["candidate"]) is not None and
                            resolution_rank(previous["candidate"])[0] < resolution_rank(switch["selected"])[0]),
                            "Fallback cannot retire an unknown/higher cloud version")

    def test_new_4k_during_fallback_download_never_competes_for_same_target(self):
        self.provider.rows = [row(10), row(10, "1080")]
        four_k_name = self.provider.rows[0]["name"]
        self.probes[four_k_name] = probe(status="unknown")
        with self.worker.store.locked():
            sub = self.active(); self.worker._process_show(self.show, sub, True)
            self.probes[four_k_name] = probe()
            self.worker._process_show(self.show, sub, True)
        inflight = [j for j in self.worker.store.state["downloads"].values() if j["status"] in {"queued", "queue_intent", "queue_unknown", "downloading"}]
        self.assertEqual(len({j["destination"] for j in inflight}), len(inflight), "Two in-flight candidates cannot own one final target")

    def test_managed_local_fallback_can_upgrade_without_retiring_unmanaged_cloud_copy(self):
        self.provider.rows = [row(10), row(10, "1080")]
        high_name = self.provider.rows[0]["name"]
        self.probes[high_name] = probe(status="unknown")
        with self.worker.store.locked():
            sub = self.active(); self.worker._process_show(self.show, sub, True)
            first = next(iter(self.worker.store.state["downloads"].values()))
            first["status"] = "complete"
            path = Path(first["destination"]); path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b"v" * 100)
            key = first["logical_key"]
            sub["assets"][key] = {"candidate": copy.deepcopy(first["candidate"]), "destination": str(path),
                                  "managed_local": True, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            # Reusing a pre-existing cloud observation did not grant cloud retirement.
            self.worker.store.state["inventory"]["selection:" + key]["managed_cloud"] = False
            self.probes[high_name] = probe()
            self.worker.cloud = CloudVersions(self.worker.store, self.provider, self.worker.openlist)
            self.worker._process_show(self.show, sub, True)
        upgrade_jobs = [j for j in self.worker.store.state["downloads"].values() if resolution_rank(j["candidate"])[0] >= 3]
        self.assertEqual(len(upgrade_jobs), 1)
        self.assertEqual(path.read_bytes(), b"v" * 100)

    def test_verified_playback_schema_completes_without_label_integer_compare(self):
        candidate = {"show_id": "test", "season": 1, "episode": 10, "probe": probe(1920, 1080),
                     "candidate_id": "verified-fallback", "size": 100}
        job = {"download_id": "fixture-job", "show_id": "test", "destination": str(self.root / "media" / "10.mkv"),
               "candidate": candidate, "logical_key": logical_key(candidate), "status": "published"}
        path = Path(job["destination"]); path.parent.mkdir(); path.write_bytes(b"v" * 100)
        self.worker.file_probe = lambda _: probe(1920, 1080)
        with self.worker.store.locked():
            sub = self.active()
            sub["assets"][job["logical_key"]] = {"candidate": candidate, "destination": str(path),
                                                  "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            self.worker._verify_jellyfin(job)
        self.assertEqual(job["status"], "complete")

    def test_later_pause_wins_over_older_activation_regardless_of_uuid_sort(self):
        (self.root / "requests").mkdir()
        records = [
            {"request_id": "ffffffff-ffff-4fff-8fff-ffffffffffff", "action": "activate_latest", "created_at": "2026-11-07T10:00:00+00:00"},
            {"request_id": "00000000-0000-4000-8000-000000000001", "action": "pause", "created_at": "2026-11-07T10:01:00+00:00"},
        ]
        for request in records:
            request.update(show_ids=["test"], policy_revision=3)
            (self.root / "requests" / (request["request_id"] + ".json")).write_text(json.dumps(request))
        with self.worker.store.locked():
            self.worker.inbox()
        self.assertEqual(self.worker.store.state["subscriptions"]["test"]["status"], "paused")

    def test_pause_keeps_already_active_download_running(self):
        (self.root / "requests").mkdir()
        request = {"request_id": str(uuid.uuid4()), "action": "pause", "show_ids": ["test"], "policy_revision": 3,
                   "created_at": "2026-11-07T10:01:00+00:00"}
        (self.root / "requests" / (request["request_id"] + ".json")).write_text(json.dumps(request))
        gid = "1" * 16
        self.aria.jobs[gid] = {"gid": gid, "status": "active", "completedLength": "10", "downloadSpeed": "1"}
        with self.worker.store.locked():
            self.active()
            self.worker.store.state["downloads"]["active-job"] = {"show_id": "test", "gid": gid, "status": "downloading"}
            self.worker.inbox()
        self.assertEqual(self.aria.jobs[gid]["status"], "active")
        self.assertFalse(any(method in {"pause", "remove"} for method, _ in self.aria.calls))

    def test_paused_subscription_does_not_submit_unexecuted_queue_intent(self):
        source = row(10)
        candidate = {"show_id": "test", "season": 1, "episode": 10, "part": None, "source_id": source["id"],
                     "name": source["name"], "source_path": source["path"], "size": 100, "probe": probe(),
                     "cloud": {"path": "/HomeMediaFollow/Test/" + source["name"]}}
        candidate["candidate_id"] = candidate_identity(candidate)
        with self.worker.store.locked():
            sub = self.active(); sub["status"] = "paused"
            self.worker.store.state["downloads"]["unexecuted"] = {
                "download_id": "unexecuted", "show_id": "test", "request_id": sub["request_id"],
                "gid": "2" * 16, "logical_key": logical_key(candidate), "status": "queue_intent",
                "candidate": candidate, "stage_file": str(self.root / "staging" / source["name"]),
                "destination": str(self.root / "media" / "10.mkv"),
            }
            self.worker.store.event("fixture_unsubmitted_intent", {})
            self.worker.completions()
        self.assertFalse(any(method == "addUri" for method, _ in self.aria.calls),
                         "Paused subscriptions must not recover a never-submitted queue intent")
        self.assertEqual(self.aria.jobs, {})

    def test_same_size_same_resolution_without_identity_is_not_adopted(self):
        source = row(10)
        candidate = {"show_id": "test", "season": 1, "episode": 10, "part": None, "source_id": source["id"],
                     "name": source["name"], "source_path": source["path"], "size": 100, "probe": probe(),
                     "cloud": {"path": "/HomeMediaFollow/Test/" + source["name"]}}
        candidate["candidate_id"] = candidate_identity(candidate)
        destination = self.worker._destination(self.show, candidate)
        destination.parent.mkdir(); destination.write_bytes(b"x" * 100)
        with self.worker.store.locked():
            sub = self.active()
            with self.assertRaises(ValueError):
                self.worker._queue(self.show, sub, candidate, None)
        self.assertEqual(destination.read_bytes(), b"x" * 100)
        self.assertEqual(self.worker.store.state["downloads"], {})

    def test_same_url_code_rotation_retries_after_failed_validation(self):
        self.catalog_patcher.stop()
        from home_media_follow.catalog import CATALOG_URL
        self.worker.config.update(catalog_mode="yinxiang",catalog_url=CATALOG_URL)
        old = {"short_url": "fixtureShare", "pwd": "A1b2"}
        new = {**old, "pwd": "B2a3"}
        self.show["source"]["catalog_guid"] = "00000000-0000-4000-8000-000000000001"
        doc = {"code": 200, "content": {"status": 1, "html":
            '<p>🌟Test 4K</p><div>BD:<a href="https://pan.baidu.com/s/1fixtureShare?pwd=B2a3">百度</a></div>'}}
        memory = {"extraction_codes": {"test": old["pwd"]}}
        class Secrets:
            def load(self):
                return {"extraction_codes": dict(memory["extraction_codes"])}
            def update(self, change):
                memory.update(change)
        self.worker.secret_store = Secrets()
        attempts = []
        def verify(short, code):
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("fixture transient verification failure")
            return {"short_url": short}
        self.provider.verify_share = verify
        def fixture_catalog(**kwargs):
            return Catalog(fetcher=lambda _: doc, sleeper=lambda _: None, **kwargs)
        with self.worker.store.locked():
            sub = self.active(); sub["catalog"] = {"binding_hash": binding_fingerprint(old)}
            with patch("home_media_follow.worker.Catalog", side_effect=fixture_catalog):
                try:
                    self.worker._catalog([self.show])
                except RuntimeError:
                    self.worker.store.event("fixture_catalog_validation_failed", {})
                self.assertEqual(memory["extraction_codes"]["test"], old["pwd"])
                self.assertNotEqual(sub.get("catalog", {}).get("binding_hash"), binding_fingerprint(new))
                self.worker._catalog([self.show])
        self.assertEqual(len(attempts), 2)
        self.assertEqual(memory["extraction_codes"]["test"], new["pwd"])
        self.assertEqual(sub["catalog"]["binding_hash"], binding_fingerprint(new))

    def test_missed_previous_evening_scan_runs_on_morning_resume(self):
        self.worker.clock = lambda: datetime(2026, 11, 8, 9, tzinfo=timezone(timedelta(hours=8))).timestamp()
        sub = {"status": "active", "last_daily_scan": "2026-11-06", "last_scan_attempt": 0}
        self.assertTrue(self.worker._scan_due(sub), "Yesterday's missed 18:00 scan remains due before today's 18:00")
        sub["last_daily_scan"] = "2026-11-07"
        self.assertFalse(self.worker._scan_due(sub))


if __name__ == "__main__":
    unittest.main()

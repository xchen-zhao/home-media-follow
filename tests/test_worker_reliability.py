"""Independent crash/control tests: local files and fakes, no real services."""
import contextlib
import copy
import json
import os
import importlib.util
from pathlib import Path, PurePosixPath as PathForCloud
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid

from home_media_follow.cloud import CloudPending, CloudVersions
from home_media_follow.baidu import BaiduError
from home_media_follow.core import candidate_identity, logical_key
from home_media_follow.core import activation
from home_media_follow.replacement import Replacement, file_sha256
from home_media_follow.store import Store
from home_media_follow.worker import Worker
from home_media_follow.hook import main as hook_main
from home_media_follow.runtime import write_manifest
from home_media_follow.work_due import candidate_quarantine_holds, source_work_due


class SimulatedCrash(BaseException):
    pass


class FakeAria:
    def __init__(self):
        self.tasks, self.adds, self.controls = {}, [], []
    def status(self, gid):
        return copy.deepcopy(self.tasks.get(gid))
    def add(self, gid, url, stage_dir, filename):
        if not isinstance(stage_dir, str):
            raise ValueError("The real Aria2 client requires a string stage directory")
        self.adds.append(gid)
        self.tasks[gid] = {"gid": gid, "status": "active", "completedLength": "0", "totalLength": "100"}
        return gid
    def call(self, method, params=None):
        self.controls.append((method, params))
        if method in {"pause", "unpause", "remove"}:
            self.tasks[params[0]]["status"] = {"pause": "paused", "unpause": "active", "remove": "removed"}[method]
        if method == "removeDownloadResult":
            self.tasks.pop(params[0], None)
        return "OK"


class WorkerReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "requests").mkdir()
        worker = Worker.__new__(Worker)
        worker.root = self.root
        worker.config = {"media_root": str(self.root / "media"), "staging": str(self.root / "_follow"), "recovery_root": str(self.root / "recovery"), "publisher_lock": str(self.root / "publish.lock"), "min_free_bytes": 0}
        worker.store = Store(self.root / "state")
        worker.aria2 = FakeAria()
        worker.openlist = Mock()
        worker.openlist.download_url.return_value = "http://127.0.0.1:5244/d/Baidu/fixture.mp4?sign=fixture"
        worker.jellyfin = None
        worker.cloud = Mock()
        worker.clock = lambda: 1000
        worker.secret_store = Mock(); worker.secret_store.load.return_value = {}
        worker.file_probe = lambda path: {"status": "verified", "width": 3840, "height": 2160, "duration": 100}
        worker.publisher = Mock(return_value=Mock())
        self.worker = worker
        self.show = {"show_id": "show", "title": "Fixture", "media_kind": "anime", "cloud_path": "/HomeMediaFollow/Fixture"}
        worker.shows = {"show": self.show}
        self.candidate = {"show_id": "show", "season": 1, "episode": 100, "part": None, "source_id": "111", "source_path": "/source/100.mp4", "name": "100.mp4", "size": 100, "cloud": {"id": "222", "path": "/HomeMediaFollow/Fixture/100.mp4", "size": 100}, "probe": {"status": "verified", "width": 3840, "height": 2160, "duration": 100}}
        self.candidate["candidate_id"] = candidate_identity(self.candidate)
        worker.provider = Mock()
        worker.provider.list_target.return_value = [{**self.candidate["cloud"], "name": self.candidate["name"], "is_dir": False}]
        with worker.store.locked():
            worker.store.state["subscriptions"]["show"] = {"show_id": "show", "status": "active", "request_id": str(uuid.uuid4()), "assets": {}}
            worker.store.save()

    def pending_transfer(self, episode=99, *, existing=False):
        candidate = copy.deepcopy(self.candidate)
        candidate.pop("candidate_id"); candidate.pop("cloud")
        candidate.update(record_type="candidate", episode=episode, source_id=str(episode),
            name=f"{episode}.mp4", source_path=f"/source/{episode}.mp4", provider_fingerprint="native-fp", status="transfer_unknown")
        cid = candidate_identity(candidate); candidate["candidate_id"] = cid
        operation_id = f"pending-{episode}"
        candidate["transfer_operation_id"] = operation_id
        row = {"id": str(episode + 1000), "name": candidate["name"], "path": "/HomeMediaFollow/Fixture/" + candidate["name"],
            "size": candidate["size"], "provider_fingerprint": "native-fp", "is_dir": False}
        if existing: candidate["cloud"] = copy.deepcopy(row)
        operation = {"kind": "cloud_transfer", "operation_id": operation_id, "show_id": "show", "candidate_id": cid,
            "stage": "prepared", "target": "/HomeMediaFollow/Fixture", "before_ids": [],
            "files": [{"id": candidate["source_id"], "path": candidate["source_path"], "name": candidate["name"],
                "size": candidate["size"], "provider_fingerprint": "native-fp", "is_dir": False}]}
        self.worker.store.state["inventory"][cid] = candidate
        self.worker.store.state["operations"][operation_id] = operation
        self.worker.store.event("fixture_pending_transfer", {})
        return candidate, operation, row

    def test_reconcile_unselected_history_without_selection_probe_or_scope_changes(self):
        worker = self.worker
        with worker.store.locked():
            candidate, operation, row = self.pending_transfer()
            sub = worker.store.state["subscriptions"]["show"]
            sub["activation"] = activation([self.candidate], "latest-100")
            before = copy.deepcopy((sub, worker.store.state["downloads"], worker.store.state["requests"]))
            def reconcile(intent):
                self.assertGreater(worker.store._depth, 0)
                return {"status": "confirmed", "files": [row]}
            worker.provider.reconcile_transfer.side_effect = reconcile
            result = worker.reconcile_pending_transfers()
            self.assertEqual(result["confirmed"], 1)
            self.assertEqual(operation["status"], "confirmed")
            self.assertEqual(candidate["cloud"], row)
            self.assertTrue(candidate["managed_cloud"])
            self.assertNotIn("status", candidate)
            self.assertEqual((sub, worker.store.state["downloads"], worker.store.state["requests"]), before)
            worker.provider.verify_share.assert_not_called()
            worker.provider.transfer_selected.assert_not_called()
            worker.cloud.select.assert_not_called()
            self.assertEqual(worker.aria2.adds, [])
        recovered = Store(worker.store.root)
        with recovered.locked(): self.assertEqual(recovered.state["operations"][operation["operation_id"]]["status"], "confirmed")

    def test_run_reconciles_pending_even_when_current_4k_makes_source_work_unnecessary(self):
        worker = self.worker
        with worker.store.locked():
            candidate, operation, row = self.pending_transfer()
            sub = worker.store.state["subscriptions"]["show"]
            sub["activation"] = activation([self.candidate], "latest-100")
            sub["scan_candidates"] = [candidate["candidate_id"], self.candidate["candidate_id"]]
            sub["assets"][logical_key(self.candidate)] = {"candidate": copy.deepcopy(self.candidate), "managed_local": True}
            worker.store.state["inventory"][self.candidate["candidate_id"]] = self.candidate
            worker.store.save()
        worker._scan_due = Mock(return_value=False); worker._process_show = Mock()
        worker.provider.reconcile_transfer.return_value = {"status": "confirmed", "files": [row]}
        worker.run()
        worker._process_show.assert_not_called()
        self.assertEqual(worker.store.state["operations"][operation["operation_id"]]["status"], "confirmed")
        self.assertEqual(worker.aria2.adds, [])

    def test_paused_and_cancelled_allow_read_only_reconcile(self):
        worker = self.worker
        for number, status in enumerate(("paused", "cancelled")):
            with self.subTest(status=status):
                with worker.store.locked():
                    candidate, operation, row = self.pending_transfer(98 + number)
                    worker.store.state["subscriptions"]["show"]["status"] = status
                    worker.store.save()
                worker._scan_due = Mock(return_value=False); worker._process_show = Mock()
                worker.provider.reconcile_transfer.return_value = {"status": "confirmed", "files": [row]}
                worker.run()
                self.assertEqual(worker.store.state["operations"][operation["operation_id"]]["status"], "confirmed")
                self.assertEqual(worker.store.state["subscriptions"]["show"]["status"], status)
                worker._process_show.assert_not_called()
        worker.provider.transfer_selected.assert_not_called()
        self.assertEqual(worker.aria2.adds, [])

    def test_matching_current_pointer_can_close_intent_without_overwriting_metadata(self):
        worker = self.worker
        with worker.store.locked():
            candidate, operation, row = self.pending_transfer(existing=True)
            candidate["cloud"]["extra_receipt"] = "retain"
            worker.store.state["inventory"]["selection:current"] = {"record_type": "cloud_selection", "candidate": copy.deepcopy(candidate), "cloud": copy.deepcopy(candidate["cloud"])}
            before = copy.deepcopy(candidate["cloud"])
            worker.provider.reconcile_transfer.return_value = {"status": "confirmed", "files": [row]}
            self.assertEqual(worker.reconcile_pending_transfers()["confirmed"], 1)
            self.assertEqual(candidate["cloud"], before)
            self.assertEqual(operation["status"], "confirmed")

    def test_changed_current_pointer_is_never_overwritten_by_old_transfer_result(self):
        worker = self.worker
        with worker.store.locked():
            candidate, operation, row = self.pending_transfer(existing=True)
            candidate["cloud"]["id"] = "99999"
            before = copy.deepcopy(candidate["cloud"])
            worker.provider.reconcile_transfer.return_value = {"status": "confirmed", "files": [row]}
            self.assertEqual(worker.reconcile_pending_transfers()["pending"], 1)
            self.assertEqual(candidate["cloud"], before)
            self.assertEqual(operation["status"], "unknown")

    def test_reconcile_rejects_cross_bound_intents_before_any_provider_read(self):
        worker = self.worker
        for field, value in (("operation_id", "other"), ("candidate_id", "wrong"), ("show_id", "other"),
                             ("target", "/HomeMediaFollow/Other"), ("files", [])):
            with self.subTest(field=field):
                with worker.store.locked():
                    candidate, operation, row = self.pending_transfer()
                    operation[field] = value
                    worker.provider.reconcile_transfer.reset_mock()
                    worker.reconcile_pending_transfers()
                    self.assertNotEqual(operation.get("status"), "confirmed")
                    self.assertNotIn("cloud", candidate)
                    worker.provider.reconcile_transfer.assert_not_called()

    def test_reconcile_requires_one_exact_live_native_file_and_original_source_binding(self):
        worker = self.worker
        mutations = [("id", "not-native"), ("is_dir", True), ("path", "/HomeMediaFollow/Other/99.mp4"),
                     ("name", "other.mp4"), ("size", 101), ("provider_fingerprint", "changed")]
        for field, value in mutations:
            with self.subTest(field=field):
                with worker.store.locked():
                    candidate, operation, row = self.pending_transfer()
                    row[field] = value
                    worker.provider.reconcile_transfer.return_value = {"status": "confirmed", "files": [row]}
                    self.assertEqual(worker.reconcile_pending_transfers()["pending"], 1)
                    self.assertNotIn("cloud", candidate)
                    self.assertEqual(operation["status"], "unknown")
        for kind in ("multiple", "before_id", "wrong_receipt", "source_fp", "source_id", "candidate_op"):
            with self.subTest(kind=kind):
                with worker.store.locked():
                    candidate, operation, row = self.pending_transfer()
                    result = {"status": "confirmed", "files": [row]}
                    if kind == "multiple": result["files"] = [row, row]
                    elif kind == "before_id": operation["before_ids"] = [row["id"]]
                    elif kind == "wrong_receipt": operation["receipt"] = {"items": [{"path": row["path"], "id": "99999"}]}
                    elif kind == "source_fp": operation["files"][0]["provider_fingerprint"] = "other"
                    elif kind == "source_id": operation["files"][0]["id"] = "other"
                    elif kind == "candidate_op": candidate["transfer_operation_id"] = "different"
                    worker.provider.reconcile_transfer.return_value = result
                    self.assertEqual(worker.reconcile_pending_transfers()["pending"], 1)
                    self.assertNotIn("cloud", candidate)

    def test_reconcile_skips_terminal_quarantine_and_precisely_retired_candidates(self):
        worker = self.worker
        with worker.store.locked():
            candidate, operation, row = self.pending_transfer(existing=True)
            for field, value in (("status", "confirmed"), ("phase", "confirmed"), ("stage", "failed"), ("terminal", True)):
                operation[field] = value
                self.assertEqual(worker.reconcile_pending_transfers()["attempted"], 0)
                operation.pop(field)
            for hold_phase in ("prepared", "move_intent", "unknown"):
                worker.store.state["operations"]["hold"] = {"kind": "candidate_quarantine", "show_id": "show", "candidate_id": candidate["candidate_id"], "phase": hold_phase}
                self.assertEqual(worker.reconcile_pending_transfers()["skipped"], 1)
            worker.store.state["operations"].pop("hold")
            candidate["retired_by"] = {"operation_id": "retired"}
            self.assertEqual(worker.reconcile_pending_transfers()["skipped"], 1)
            candidate.pop("retired_by")
            worker.store.state["operations"]["retired"] = {"kind": "cloud_replacement", "show_id": "show", "phase": "confirmed", "old": copy.deepcopy(row)}
            self.assertEqual(worker.reconcile_pending_transfers()["skipped"], 1)
            self.assertEqual(candidate["cloud"], row)
            worker.provider.reconcile_transfer.assert_not_called()

    def test_reconcile_429_stops_batch_and_persists_global_backoff(self):
        worker = self.worker
        with worker.store.locked():
            candidate, operation, row = self.pending_transfer(98)
            self.pending_transfer(99)
            worker.provider.reconcile_transfer.side_effect = BaiduError("sensitive-response-must-not-persist", code=429, retry_after=3600)
            result = worker.reconcile_pending_transfers()
            self.assertEqual((result["attempted"], result["pending"], result["deferred"]), (1, 1, True))
            worker.provider.reconcile_transfer.assert_called_once()
            self.assertEqual(worker.store.state["operations"]["provider-backoff"]["until"], 4600)
        worker.store = Store(worker.store.root)
        worker.provider.reconcile_transfer.reset_mock()
        self.assertTrue(worker.reconcile_pending_transfers()["deferred"])
        worker.provider.reconcile_transfer.assert_not_called()
        self.assertNotIn("sensitive-response-must-not-persist", worker.store.journal_path.read_text(encoding="utf-8"))

    def test_reconcile_bounded_batch_does_not_starve_other_intents_after_unknown(self):
        worker = self.worker
        worker.config["pending_transfer_reconcile_limit"] = 1
        with worker.store.locked():
            first, first_op, first_row = self.pending_transfer(98)
            second, second_op, second_row = self.pending_transfer(99)
            worker.provider.reconcile_transfer.return_value = {"status": "unknown"}
            self.assertEqual(worker.reconcile_pending_transfers()["pending"], 1)
            worker.provider.reconcile_transfer.return_value = {"status": "confirmed", "files": [second_row]}
            self.assertEqual(worker.reconcile_pending_transfers()["confirmed"], 1)
            self.assertEqual(second_op["status"], "confirmed")
            self.assertEqual(first_op["status"], "unknown")

    def test_reconcile_crash_before_journal_can_repeat_only_read_without_transfer(self):
        worker = self.worker
        with worker.store.locked(): candidate, operation, row = self.pending_transfer()
        worker.provider.reconcile_transfer.return_value = {"status": "confirmed", "files": [row]}
        original_event = worker.store.event
        def crash(kind, payload):
            if kind == "pending_transfer_reconciled": raise SimulatedCrash()
            return original_event(kind, payload)
        with patch.object(worker.store, "event", side_effect=crash):
            with self.assertRaises(SimulatedCrash): worker.reconcile_pending_transfers()
        worker.store = Store(worker.store.root)
        self.assertEqual(worker.reconcile_pending_transfers()["confirmed"], 1)
        self.assertEqual(worker.provider.reconcile_transfer.call_count, 2)
        worker.provider.transfer_selected.assert_not_called()
        self.assertEqual(worker.aria2.adds, [])

    def test_queue_manifest_crash_is_reconciled_into_one_submission(self):
        worker = self.worker
        with worker.store.locked():
            sub = worker.store.state["subscriptions"]["show"]
            with patch("home_media_follow.worker.write_manifest", side_effect=SimulatedCrash()):
                with self.assertRaises(SimulatedCrash):
                    worker._queue(self.show, sub, self.candidate, None)
        worker.store = Store(self.root / "state")
        with worker.store.locked():
            worker.completions()
            worker.completions()
            self.assertEqual(len(worker.aria2.adds), 1)
            self.assertEqual(len(worker.store.state["downloads"]), 1)

    def test_paused_unsubmitted_intent_does_not_create_new_aria_task(self):
        worker = self.worker
        with worker.store.locked():
            sub = worker.store.state["subscriptions"]["show"]
            with patch("home_media_follow.worker.write_manifest", side_effect=SimulatedCrash()):
                with self.assertRaises(SimulatedCrash):
                    worker._queue(self.show, sub, self.candidate, None)
            sub["status"] = "paused"
            worker.store.event("subscription_paused", {"show_id": "show"})
        worker.store = Store(self.root / "state")
        with worker.store.locked():
            worker.completions()
            self.assertEqual(worker.aria2.adds, [])

    def test_pause_subscription_does_not_pause_existing_download(self):
        worker = self.worker
        request = {"request_id": str(uuid.uuid4()), "action": "pause", "show_ids": ["show"], "policy_revision": 3, "created_at": "2026-11-07T10:00:00+00:00"}
        (self.root / "requests" / (request["request_id"] + ".json")).write_text(json.dumps(request))
        with worker.store.locked():
            worker.store.state["downloads"]["one"] = {"download_id": "one", "show_id": "show", "gid": "1234567890abcdef", "status": "downloading"}
            worker.aria2.tasks["1234567890abcdef"] = {"gid": "1234567890abcdef", "status": "active"}
            worker.inbox()
            self.assertEqual(worker.store.state["subscriptions"]["show"]["status"], "paused")
            self.assertEqual(worker.aria2.tasks["1234567890abcdef"]["status"], "active")
            self.assertFalse(any(method in {"pause", "remove"} for method, _ in worker.aria2.controls))

    def test_existing_same_size_file_without_identity_receipt_is_not_adopted(self):
        worker = self.worker
        destination = worker._destination(self.show, self.candidate)
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"x" * self.candidate["size"])
        with worker.store.locked():
            sub = worker.store.state["subscriptions"]["show"]
            with self.assertRaises(ValueError):
                worker._queue(self.show, sub, self.candidate, None)
            self.assertFalse(sub["assets"])
            self.assertEqual(destination.read_bytes(), b"x" * self.candidate["size"])

    def test_missing_manifest_uppercase_managed_path_does_not_call_legacy(self):
        (self.root / "config").mkdir()
        (self.root / "config/shows.json").write_text(json.dumps({"staging": "D:/Media/Downloads/_follow"}))
        with patch("home_media_follow.hook.subprocess.run", return_value=Mock(returncode=0)) as legacy:
            code = hook_main(["1234567890abcdef", "1", "D:/Media/Downloads/_FOLLOW/item/one.mp4"], root=self.root)
            self.assertNotEqual(code, 0)
            legacy.assert_not_called()

    def test_mismatched_manifest_digest_is_rejected_before_media_probe(self):
        gid = "1234567890abcdef"
        source = self.root / "_follow" / "one" / "one.mp4"
        source.parent.mkdir(parents=True); source.write_bytes(b"n" * 100)
        manifest = {"gid": gid, "download_id": "one", "stage_file": str(source), "candidate": self.candidate}
        for name in ("manifests", "gids"):
            (self.root / "state" / name).mkdir()
        (self.root / "state/manifests" / (gid + ".json")).write_text(json.dumps(manifest))
        (self.root / "state/gids" / (gid + ".json")).write_text(json.dumps({"gid": gid, "download_id": "one", "manifest_sha256": "0" * 64}))
        with patch("home_media_follow.hook.gate", return_value={"status": "verified"}) as media_gate:
            code = hook_main([gid, "1", str(source)], root=self.root)
            self.assertNotEqual(code, 0)
            media_gate.assert_not_called()

    def test_paused_subscription_still_reconciles_published_job(self):
        worker = self.worker
        worker._verify_jellyfin = Mock()
        with worker.store.locked():
            worker.store.state["subscriptions"]["show"]["status"] = "paused"
            worker.store.state["downloads"]["one"] = {"download_id": "one", "show_id": "show", "gid": "1234567890abcdef", "status": "published"}
            worker.completions()
            worker._verify_jellyfin.assert_called_once()

    def test_later_pause_wins_even_when_uuid_sorts_before_activation(self):
        earlier = {"request_id": "ffffffff-ffff-ffff-ffff-ffffffffffff", "action": "activate_latest", "show_ids": ["show"], "policy_revision": 3, "created_at": "2026-11-07T10:00:00+00:00"}
        later = {"request_id": "00000000-0000-0000-0000-000000000001", "action": "pause", "show_ids": ["show"], "policy_revision": 3, "created_at": "2026-11-07T11:00:00+00:00"}
        for request in (earlier, later):
            (self.root / "requests" / (request["request_id"] + ".json")).write_text(json.dumps(request))
            os.utime(self.root / 'requests' / (request['request_id']+'.json'),ns=(1000000000,1000000000))
        with self.worker.store.locked():
            self.worker.inbox()
            self.assertEqual(self.worker.store.state["subscriptions"]["show"]["status"], "paused")

    def test_gateway_acceptance_order_controls_worker_despite_reverse_created_at(self):
        from home_media_follow.gateway import submit
        (self.root/'config').mkdir(exist_ok=True)
        (self.root/'config/shows.json').write_text(json.dumps({'shows':[self.show]}),encoding='utf-8')
        first={'request_id':'ffffffff-ffff-ffff-ffff-ffffffffffff','action':'activate_latest','show_ids':['show'],'policy_revision':3,'created_at':'2026-11-07T12:00:00+00:00'}
        later={**first,'request_id':'00000000-0000-0000-0000-000000000001','action':'pause','created_at':'2026-11-07T10:00:00+00:00'}
        submit(self.root,first);submit(self.root,later);submit(self.root,first)
        for request in (first,later):
            os.utime(self.root/'requests'/(request['request_id']+'.json'),ns=(1000000000,1000000000))
        with self.worker.store.locked():
            self.worker.inbox()
            self.assertEqual(self.worker.store.state['subscriptions']['show']['status'],'paused')

    def test_new_version_does_not_move_cloud_while_same_episode_is_downloading(self):
        worker = self.worker
        worker.provider = Mock(); worker.provider.list_target.return_value = []
        worker._source = Mock(return_value=({}, [self.candidate]))
        worker._probe_candidate = Mock()
        worker.cloud.select.return_value = None
        with worker.store.locked():
            sub = worker.store.state["subscriptions"]["show"]
            sub["activation"] = activation([self.candidate], "complete")
            worker.store.state["downloads"]["old-version"] = {"download_id": "old-version", "show_id": "show", "logical_key": logical_key(self.candidate), "status": "downloading", "candidate": copy.deepcopy(self.candidate)}
            worker._process_show(self.show, sub, True)
            worker.cloud.select.assert_not_called()

    def test_rejected_version_cannot_hide_another_verified_upgrade(self):
        worker = self.worker
        failed = copy.deepcopy(self.candidate)
        alternative = copy.deepcopy(self.candidate)
        alternative.update(source_id="222", source_path="/source/alternate.mp4", name="100-new.mp4")
        alternative.pop("candidate_id")
        alternative["candidate_id"] = candidate_identity(alternative)
        alternative["cloud"] = {"id": "333", "path": "/HomeMediaFollow/Fixture/100-new.mp4", "size": 100}
        fallback = copy.deepcopy(self.candidate)
        fallback.update(source_id="fallback", source_path="/source/1080.mp4", name="100-1080.mp4")
        fallback["probe"] = {"status": "verified", "width": 1920, "height": 1080, "duration": 100}
        fallback.pop("candidate_id")
        fallback["candidate_id"] = candidate_identity(fallback)
        worker.provider = Mock(); worker.provider.list_target.return_value = []
        worker._source = Mock(return_value=({}, [failed, alternative]))
        worker._probe_candidate = Mock()
        worker.cloud.select.return_value = None
        worker._queue = Mock()
        with worker.store.locked():
            sub = worker.store.state["subscriptions"]["show"]
            sub["activation"] = activation([self.candidate], "complete")
            sub["assets"][logical_key(failed)] = {"candidate": fallback, "managed_local": True}
            worker.store.state["downloads"]["failed"] = {"download_id": "failed", "show_id": "show", "logical_key": logical_key(failed), "status": "rejected", "candidate": failed}
            for candidate in (failed, alternative):
                worker.store.state["inventory"][candidate["candidate_id"]] = candidate
            worker._process_show(self.show, sub, True)
            self.assertEqual(worker._queue.call_args.args[2]["candidate_id"], alternative["candidate_id"])

    def test_configured_source_fsid_cannot_fall_back_to_same_path(self):
        worker = self.worker
        show = {**self.show, "source": {"short_url": "fixture", "source_root": "/media", "source_root_id": "111"}}
        worker.provider = Mock()
        worker.provider.list_share.return_value = [{"id": "999", "path": "/media", "is_dir": True}]
        with worker.store.locked():
            with self.assertRaises(ValueError):
                worker._source(show, worker.store.state["subscriptions"]["show"])
            worker.provider.walk_share.assert_not_called()

    def test_same_source_fsid_can_canonicalize_native_parent_path(self):
        worker = self.worker
        show = {**self.show, "source": {"short_url": "fixture", "source_root": "/media", "source_root_id": "111"}}
        worker.provider = Mock()
        worker.provider.list_share.return_value = [{"id": "111", "path": "/native/media", "is_dir": True}]
        worker.provider.walk_share.return_value = [{"id": "444", "path": "/native/media/100.mp4", "name": "100.mp4", "is_dir": False, "size": 100}]
        with worker.store.locked():
            sub = worker.store.state["subscriptions"]["show"]
            _, candidates = worker._source(show, sub)
            self.assertEqual(sub["source_override"]["source_root"], "/native/media")
            self.assertEqual(sub["source_override"]["source_root_id"], "111")
            self.assertEqual(candidates[0]["source_id"], "444")

    def test_imported_cloud_ownership_requires_original_native_fingerprint(self):
        worker = self.worker
        candidate = copy.deepcopy(self.candidate)
        candidate.pop("cloud")
        candidate["provider_fingerprint"] = "new-native-fingerprint"
        target = {"id": "222", "path": "/HomeMediaFollow/Fixture/100.mp4", "name": "100.mp4", "size": 100, "is_dir": False, "provider_fingerprint": "new-native-fingerprint"}
        with worker.store.locked():
            worker.store.state["inventory"]["import:old"] = {"record_type": "import", "show_id": "show", "cloud_path": target["path"], "source_path": candidate["source_path"], "source_id": candidate["source_id"], "size": 100, "provider_fingerprint": "old-native-fingerprint"}
            self.assertTrue(worker._ensure_cloud(self.show, candidate, {}, [target]))
            self.assertFalse(candidate.get("managed_cloud"))
            self.assertEqual(candidate["cloud"]["id"], "222")

    def queued_job(self):
        sub = self.worker.store.state["subscriptions"]["show"]
        self.worker._queue(self.show, sub, self.candidate, None)
        return sub, next(iter(self.worker.store.state["downloads"].values()))

    def test_download_retries_are_bounded_and_reuse_exact_gid(self):
        worker = self.worker
        with worker.store.locked():
            sub, job = self.queued_job()
            for attempt in range(1, 6):
                worker.aria2.tasks[job["gid"]]["status"] = "error"
                job["next_retry_at"] = 0.5
                worker._retry_download(job, sub, {"status": "error"})
                self.assertEqual(job["retry_attempts"], attempt)
            job["next_retry_at"] = 0.5
            worker._retry_download(job, sub, {"status": "error"})
            self.assertEqual(len(worker.aria2.adds), 6)  # Original plus five retries.
            self.assertEqual(set(worker.aria2.adds), {job["gid"]})

    def test_download_retry_respects_pause_and_resume_control_identity(self):
        worker = self.worker
        with worker.store.locked():
            sub, job = self.queued_job()
            job["next_retry_at"] = 0.5
            sub["status"] = "paused"
            worker._retry_download(job, sub, {"status": "error"})
            self.assertEqual(len(worker.aria2.adds), 1)
            self.assertNotIn("retry_attempts", job)
            sub["status"] = "active"
            Path(job["stage_file"]).write_bytes(b"partial")
            with self.assertRaises(ValueError):
                worker._retry_download(job, sub, {"status": "error"})
            self.assertEqual(len(worker.aria2.adds), 1)

    def test_retry_response_loss_reconciles_active_task_without_another_add(self):
        worker = self.worker
        with worker.store.locked():
            sub, job = self.queued_job()
            job["next_retry_at"] = 0.5
            actual_add = worker.aria2.add
            def accepted_but_response_lost(*args):
                actual_add(*args)
                raise RuntimeError("simulated response loss")
            with patch.object(worker.aria2, "add", side_effect=accepted_but_response_lost):
                with self.assertRaises(RuntimeError):
                    worker._retry_download(job, sub, {"status": "error"})
            worker.completions()
            self.assertEqual(len(worker.aria2.adds), 2)
            self.assertEqual(job["status"], "downloading")

    def test_retry_does_not_resume_another_cloud_file_at_same_path(self):
        worker = self.worker
        with worker.store.locked():
            sub, job = self.queued_job()
            job["next_retry_at"] = 0.5
            source = Path(job["stage_file"])
            source.write_bytes(b"partial-original")
            source.with_name(source.name + ".aria2").write_bytes(b"original-control")
            worker.provider.list_target.return_value = [{**job["candidate"]["cloud"], "id": "different-file", "name": job["candidate"]["name"], "is_dir": False}]
            with contextlib.suppress(ValueError, RuntimeError):
                worker._retry_download(job, sub, {"status": "error"})
            self.assertEqual(len(worker.aria2.adds), 1)
            self.assertFalse(any(method == "removeDownloadResult" for method, _ in worker.aria2.controls))

    def test_historical_pending_transfer_reconciles_even_when_target_has_appeared(self):
        worker = self.worker
        show = {**self.show, "source": {"short_url": "fixture", "source_root": "/source"}}
        candidate = copy.deepcopy(self.candidate)
        candidate.pop("candidate_id"); candidate.pop("cloud")
        candidate.update(episode=99, name="99.mp4", source_path="/source/99.mp4", provider_fingerprint="native-fp", probe={"status": "unknown"})
        candidate["candidate_id"] = candidate_identity(candidate)
        candidate["transfer_operation_id"] = "history-transfer"
        target = {"id": "222", "path": "/HomeMediaFollow/Fixture/99.mp4", "name": "99.mp4", "size": 100, "is_dir": False, "provider_fingerprint": "native-fp"}
        worker._scan_due = Mock(return_value=False)
        worker.provider.list_share.return_value = []
        worker.provider.list_target.return_value = [target]
        worker.provider.reconcile_transfer.return_value = {"status": "confirmed", "files": [target]}
        worker.openlist.get.return_value = {"size": 100}
        worker.openlist.probe_url.return_value = "http://127.0.0.1/fixture"
        worker.url_probe = lambda *args, **kwargs: {"status": "verified", "width": 3840, "height": 2160, "duration": 100}
        worker.cloud.select.return_value = None
        worker._queue = Mock()
        with worker.store.locked():
            sub = worker.store.state["subscriptions"]["show"]
            sub["activation"] = activation([self.candidate], "activation-at-100")
            sub["scan_candidates"] = [candidate["candidate_id"]]
            worker.store.state["inventory"][candidate["candidate_id"]] = candidate
            operation = {"operation_id": "history-transfer", "kind": "cloud_transfer", "show_id": "show", "candidate_id": candidate["candidate_id"], "stage": "write_unknown", "status": "unknown"}
            worker.store.state["operations"]["history-transfer"] = operation
            worker._process_show(show, sub, False)
            worker.provider.reconcile_transfer.assert_called_once()
            self.assertEqual(operation["status"], "confirmed")
            worker._queue.assert_not_called()
            self.assertEqual(sub["activation"]["watermark"]["episode"], 100)

    def test_worker_cannot_submit_stale_root_path_after_quarantine_response_loss(self):
        from home_media_follow import quarantine as repair
        worker = self.worker
        show = {**self.show, "source": {"short_url": "fixture", "source_root": "/source"}}
        candidate = copy.deepcopy(self.candidate)
        candidate.pop("candidate_id")
        candidate.update(record_type="candidate", managed_cloud=True, provider_fingerprint="native-fp", transfer_operation_id="old-transfer")
        candidate["cloud"]["provider_fingerprint"] = "native-fp"
        candidate["cloud"]["name"] = candidate["name"]
        candidate["cloud"]["is_dir"] = False
        candidate["candidate_id"] = candidate_identity(candidate)
        rows = {candidate["cloud"]["path"]: copy.deepcopy(candidate["cloud"])}
        moves = []
        worker.provider.list_target.side_effect = lambda path: [copy.deepcopy(row) for row in rows.values() if str(PathForCloud(row["path"]).parent) == path]
        worker.provider.list_share.return_value = []
        def fs(operation, body):
            if operation == "mkdir":
                path = body["path"].removeprefix("/Baidu")
                rows[path] = {"id": "dir-" + path, "path": path, "name": PathForCloud(path).name, "size": 0, "is_dir": True}
            elif operation == "move":
                old = body["src_dir"].removeprefix("/Baidu") + "/" + body["names"][0]
                new = body["dst_dir"].removeprefix("/Baidu") + "/" + body["names"][0]
                row = rows.pop(old); row["path"] = new; rows[new] = row
                moves.append((old, new))
                raise OSError("simulated committed move with lost response")
            else:
                raise AssertionError("No deletion permitted")
        worker.openlist.fs.side_effect = fs
        worker._scan_due = Mock(return_value=False)
        worker.cloud.select.return_value = None
        with worker.store.locked():
            sub = worker.store.state["subscriptions"]["show"]
            sub["activation"] = activation([candidate], "complete")
            sub["scan_candidates"] = [candidate["candidate_id"]]
            worker.store.state["inventory"][candidate["candidate_id"]] = candidate
            worker.store.state["operations"]["old-transfer"] = {"kind": "cloud_transfer", "status": "confirmed", "show_id": "show", "candidate_id": candidate["candidate_id"], "before_ids": [], "files": [{"id": candidate["source_id"], "size": candidate["size"], "provider_fingerprint": "native-fp"}]}
            first = repair.apply_plan(worker.store, worker.provider, worker.openlist, {"show": show})
            self.assertEqual(first["status"], "pending")
        worker.store = Store(worker.store.root)
        with worker.store.locked():
            sub = worker.store.state["subscriptions"]["show"]
            worker._process_show(show, sub, False)
            self.assertEqual(worker.aria2.adds, [])
            self.assertEqual(sub["candidate_quarantine_holds"][candidate["candidate_id"]]["phase"], "move_intent")
            for job in worker.store.state["downloads"].values():
                cloud = job["candidate"]["cloud"]
                self.assertIn(cloud["path"], rows)
                self.assertEqual(rows[cloud["path"]]["id"], cloud["id"])
            second = repair.apply_plan(worker.store, worker.provider, worker.openlist, {"show": show})
            self.assertEqual(second["status"], "complete")
            self.assertEqual(len(moves), 1)
            self.assertEqual(worker._quarantine_holds(show, sub), {})
            self.assertNotIn("candidate_quarantine_holds", sub)

    def test_pending_quarantine_blocks_direct_enqueue_and_cloud_work_until_confirmed(self):
        worker = self.worker
        with worker.store.locked():
            sub = worker.store.state["subscriptions"]["show"]
            sub["activation"] = activation([self.candidate], "complete")
            sub["scan_candidates"] = [self.candidate["candidate_id"]]
            worker.store.state["inventory"][self.candidate["candidate_id"]] = self.candidate
            operation = {"kind": "candidate_quarantine", "show_id": "show", "candidate_id": self.candidate["candidate_id"]}
            worker.store.state["operations"]["repair"] = operation
            for phase in (None, "prepared", "move_intent", "unknown"):
                operation["phase"] = phase
                self.assertFalse(source_work_due(worker.store.state, sub, worker.clock()))
                worker._queue(self.show, sub, self.candidate, None)
                self.assertFalse(worker._ensure_cloud(self.show, self.candidate, {}, []))
                self.assertEqual(worker.aria2.adds, [])
                self.assertEqual(worker.store.state["downloads"], {})
                worker.provider.list_target.assert_not_called()
            # Neither an unrelated show's operation nor its confirmation controls this hold.
            worker.store.state["operations"]["other"] = {**operation, "show_id": "other", "phase": "confirmed"}
            self.assertIn(self.candidate["candidate_id"], candidate_quarantine_holds(worker.store.state, "show"))
            operation["phase"] = "confirmed"
            self.assertTrue(source_work_due(worker.store.state, sub, worker.clock()))
            worker._queue(self.show, sub, self.candidate, None)
            self.assertEqual(len(worker.aria2.adds), 1)

    def test_daily_scan_can_select_other_candidate_but_never_pending_quarantine(self):
        worker = self.worker
        alternate = copy.deepcopy(self.candidate)
        alternate.pop("candidate_id")
        alternate.update(source_id="112", name="100-1080.mp4", source_path="/source/100-1080.mp4")
        alternate["cloud"] = {"id": "333", "path": "/HomeMediaFollow/Fixture/100-1080.mp4", "size": 100}
        alternate["probe"].update(width=1920, height=1080)
        alternate["candidate_id"] = candidate_identity(alternate)
        worker._scan_due = Mock(return_value=True)
        worker._source = Mock(return_value=({}, [self.candidate, alternate]))
        worker._queue = Mock()
        worker.cloud.select.return_value = None
        with worker.store.locked():
            sub = worker.store.state["subscriptions"]["show"]
            sub["activation"] = activation([self.candidate], "complete")
            operation = {"kind": "candidate_quarantine", "show_id": "show", "candidate_id": self.candidate["candidate_id"], "phase": "prepared"}
            worker.store.state["operations"]["repair"] = operation
            worker._process_show(self.show, sub, True)
            self.assertEqual(worker._queue.call_args.args[2]["candidate_id"], alternate["candidate_id"])
            self.assertEqual(operation["phase"], "prepared")
            operation["phase"] = "confirmed"
            worker._process_show(self.show, sub, True)
            self.assertEqual(worker._queue.call_args.args[2]["candidate_id"], self.candidate["candidate_id"])

    def test_completed_local_transaction_survives_aria_result_loss(self):
        worker = self.worker
        source, target = self.root / "new.mp4", self.root / "published.mp4"
        source.write_bytes(b"new-video-content")
        candidate = copy.deepcopy(self.candidate); candidate["size"] = source.stat().st_size
        job = {"download_id": "already-moved", "gid": "1234567890abcdef", "show_id": "show", "request_id": "request", "logical_key": logical_key(candidate), "status": "queued", "candidate": candidate, "stage_file": str(source), "destination": str(target), "cloud_operation_id": None}
        operation_id = "local-" + job["download_id"][:32]
        with worker.store.locked():
            worker.store.state["downloads"][job["download_id"]] = job
            write_manifest(self.root, job)
            replacement = Replacement(worker.store, operation_id)
            replacement.prepare(source, target, metadata={"backup_root": str(self.root / "recovery")})
            replacement.run(lambda path: True)
        worker.store = Store(self.root / "state")
        with worker.store.locked():
            self.assertIsNone(worker.aria2.status(job["gid"]))
            worker.completions()
            self.assertIn(worker.store.state["downloads"][job["download_id"]]["status"], {"published", "complete"})
            self.assertEqual(worker.store.state["downloads"][job["download_id"]]["completed_bytes"], candidate["size"])
            self.assertFalse(worker.aria2.adds)

    def test_cloud_move_unknown_never_reissues_move(self):
        worker = self.worker
        provider, openlist = Mock(), Mock()
        source = {"id": "old-id", "path": "/HomeMediaFollow/Fixture/old.mp4", "size": 100}
        provider.list_target.side_effect = lambda parent: [dict(source)] if parent == "/HomeMediaFollow/Fixture" else []
        cloud = CloudVersions(worker.store, provider, openlist)
        with worker.store.locked():
            operation = {"operation_id": "op", "phase": "old_move_intent", "old": dict(source)}
            worker.store.state["operations"]["op"] = operation
            for _ in range(2):
                with self.assertRaises(CloudPending):
                    cloud._move(operation, "old", "/HomeMediaFollow/Fixture/_follow_recovery/op")
            openlist.fs.assert_not_called()

    def published_job(self):
        target = self.root / "published.mp4"
        target.write_bytes(b"n" * self.candidate["size"])
        candidate = copy.deepcopy(self.candidate)
        key = logical_key(candidate)
        job = {"download_id": "published", "gid": "1234567890abcdef", "show_id": "show", "logical_key": key, "status": "published", "candidate": candidate, "destination": str(target), "cloud_operation_id": "cloud-op"}
        self.worker.store.state["subscriptions"]["show"]["assets"][key] = {"candidate": candidate, "destination": str(target), "sha256": file_sha256(target), "managed_local": True}
        self.worker.store.state["downloads"][job["download_id"]] = job
        self.worker.jellyfin = Mock()
        self.worker.jellyfin.verify.return_value = {"status": "verified", "playable": True, "quality_tier": "4k_or_higher", "width": 3840, "height": 2160, "item_id": "local-fixture"}
        return job, target

    def test_actual_jellyfin_result_shape_can_complete_job(self):
        with self.worker.store.locked():
            job, _ = self.published_job()
            job.update(completed_bytes=0, download_speed=100, last_error={"type": "OldDownloadError"})
            self.worker._verify_jellyfin(job)
            self.assertEqual(job["status"], "complete")
            self.assertEqual(job["completed_bytes"], job["candidate"]["size"])
            self.assertEqual(job["download_speed"], 0)
            self.assertNotIn("last_error", job)
            self.worker.cloud.confirm.assert_called_once_with("cloud-op")

    def test_trusted_adoption_reports_complete_bytes_without_new_download(self):
        worker = self.worker
        destination = worker._destination(self.show, self.candidate)
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"n" * self.candidate["size"])
        worker._trusted_local_receipt = Mock(return_value={"receipt": "trusted-fixture"})
        with worker.store.locked():
            sub = worker.store.state["subscriptions"]["show"]
            worker._queue(self.show, sub, self.candidate, None)
            job = next(iter(worker.store.state["downloads"].values()))
            self.assertEqual(job["status"], "adopted")
            self.assertEqual(job["completed_bytes"], self.candidate["size"])
            self.assertEqual(job["download_speed"], 0)
            self.assertEqual(worker.aria2.adds, [])

    def test_changed_target_cannot_confirm_cloud_retirement(self):
        with self.worker.store.locked():
            job, target = self.published_job()
            target.write_bytes(b"x" * self.candidate["size"])
            with self.assertRaises(ValueError):
                self.worker._verify_jellyfin(job)
            self.assertNotEqual(job["status"], "complete")
            self.worker.cloud.confirm.assert_not_called()

    def test_recovery_reuses_strict_expected_quality_gate(self):
        worker = self.worker
        source, target = self.root / "new.mp4", self.root / "published.mp4"
        source.write_bytes(b"new-video-content"); target.write_bytes(b"old-video-content")
        old = target.read_bytes()
        candidate = copy.deepcopy(self.candidate); candidate["size"] = source.stat().st_size
        job = {"download_id": "abc", "gid": "1234567890abcdef", "show_id": "show", "request_id": "request", "logical_key": logical_key(candidate), "status": "queued", "candidate": candidate, "stage_file": str(source), "destination": str(target), "cloud_operation_id": None}
        operation_id = "local-" + job["download_id"][:32]
        with worker.store.locked():
            worker.store.state["downloads"]["abc"] = job
            write_manifest(self.root, job)
            transaction = Replacement(worker.store, operation_id)
            transaction.prepare(source, target, file_sha256(target), {"backup_root": str(self.root / "recovery")})
            original = transaction._record
            def interrupt(phase, **updates):
                result = original(phase, **updates)
                if phase == "backed_up":
                    raise SimulatedCrash()
                return result
            with patch.object(transaction, "_record", side_effect=interrupt):
                with self.assertRaises(SimulatedCrash):
                    transaction.run(lambda path: True)
        worker.store = Store(self.root / "state")
        worker.aria2.tasks[job["gid"]] = {"gid": job["gid"], "status": "complete", "completedLength": str(candidate["size"])}
        worker.file_probe = lambda path: {"status": "verified", "width": 1920, "height": 1080, "duration": 100}
        with worker.store.locked():
            worker.completions()
            self.assertEqual(target.read_bytes(), old)
            self.assertEqual(worker.store.state["downloads"]["abc"]["status"], "rejected")


if __name__ == "__main__":
    unittest.main()

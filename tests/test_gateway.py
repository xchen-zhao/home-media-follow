import concurrent.futures
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch
from home_media_follow import gateway

from home_media_follow.gateway import GatewayError, MAX_INPUT_BYTES, encode, status, submit, ordered_inbox


def envelope(**changes):
    value = {"request_id": str(uuid.uuid4()), "action": "activate_latest", "show_ids": ["a"],
             "policy_revision": 3, "created_at": "2026-11-07T10:00:00+08:00"}
    value.update(changes)
    return value


def snapshot(**changes):
    result = {key: {} for key in ("requests", "subscriptions", "inventory", "downloads", "operations")}
    result.update(changes)
    result["_checksum"] = hashlib.sha256(encode(result)).hexdigest()
    return result


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "config").mkdir()
        (self.root / "config" / "shows.json").write_text(json.dumps({"shows": [
            {"show_id": "a", "title": "作品A", "media_kind": "anime", "source": {"share_url": "https://private.example/secret", "cookie": "never-output"}},
            {"show_id": "b", "title": "作品B", "media_kind": "variety"},
        ]}))

    def write_state(self, document):
        (self.root / "state").mkdir(exist_ok=True)
        path = self.root / "state" / "state.json"
        path.write_text(json.dumps(document))
        return path

    def test_submit_only_creates_inbox_not_business_state(self):
        path = self.write_state(snapshot(inventory={str(i): {"show_id": "a"} for i in range(1313)}))
        before = path.read_bytes()
        request = envelope()
        result = submit(self.root, request)
        self.assertTrue(result["ok"])
        self.assertFalse(result["business_state_changed"])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(json.loads((self.root / "requests" / (request["request_id"] + ".json")).read_bytes()), request)
        self.assertFalse((self.root / "state" / "events.jsonl").exists())
        self.assertFalse((self.root / "state" / "follow.lock").exists())
        self.assertEqual(len(list((self.root / "requests").glob("*.json"))), 1)

    def test_submit_before_worker_exists_does_not_create_state_directory(self):
        submit(self.root, envelope())
        self.assertFalse((self.root / "state").exists())

    def test_same_id_same_content_is_idempotent_and_mismatch_rejected(self):
        request = envelope()
        first = submit(self.root, request)
        path = self.root / "requests" / (request["request_id"] + ".json")
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        second = submit(self.root, copy.deepcopy(request))
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["payload_hash"], second["payload_hash"])
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        with self.assertRaisesRegex(GatewayError, "request_id_payload_mismatch"):
            submit(self.root, {**request, "action": "pause"})
        self.assertEqual(path.read_bytes(), before[0])

    def test_acceptance_sequence_wins_equal_mtime_reverse_uuid_and_client_clock(self):
        first=envelope(request_id='ffffffff-ffff-ffff-ffff-ffffffffffff',created_at='2026-11-07T12:00:00+00:00')
        second=envelope(request_id='00000000-0000-0000-0000-000000000001',action='pause',created_at='2026-11-07T10:00:00+00:00')
        submit(self.root,first);submit(self.root,second)
        for request in (first,second):
            os.utime(self.root/'requests'/(request['request_id']+'.json'),ns=(1000000000,1000000000))
        self.assertEqual([p.stem for p in ordered_inbox(self.root,{'a','b'})],[first['request_id'],second['request_id']])
        receipt=self.root/'requests'/'.order'/(first['request_id']+'.json')
        before=receipt.read_bytes()
        self.assertTrue(submit(self.root,first)['duplicate'])
        self.assertEqual(receipt.read_bytes(),before)
        self.assertEqual([p.stem for p in ordered_inbox(self.root,{'a','b'})],[first['request_id'],second['request_id']])

    def test_durable_receipt_recovers_exposure_crash_before_later_control(self):
        first=envelope(request_id='ffffffff-ffff-ffff-ffff-ffffffffffff')
        second=envelope(action='pause')
        target=self.root/'requests'/(first['request_id']+'.json')
        write=gateway._atomic_write
        def interrupted(path,data):
            if path==target: raise OSError('simulated crash after acceptance')
            return write(path,data)
        with patch.object(gateway,'_atomic_write',side_effect=interrupted):
            with self.assertRaises(OSError): submit(self.root,first)
        self.assertFalse(target.exists())
        receipt=self.root/'requests'/'.order'/(first['request_id']+'.json')
        sequence=json.loads(receipt.read_bytes())['sequence']
        submit(self.root,second)
        self.assertEqual(json.loads(target.read_bytes()),first)
        self.assertTrue(submit(self.root,first)['duplicate'])
        self.assertEqual(json.loads(receipt.read_bytes())['sequence'],sequence)
        self.assertEqual([p.stem for p in ordered_inbox(self.root,{'a','b'})],[first['request_id'],second['request_id']])

    def test_modified_sequence_receipt_fails_closed_without_reordering(self):
        first=envelope(action='activate_latest');second=envelope(action='cancel')
        submit(self.root,first);submit(self.root,second)
        target=self.root/'requests'/'.order'/(first['request_id']+'.json')
        receipt=json.loads(target.read_bytes());receipt['sequence']=3
        target.write_bytes(encode(receipt))
        visible={p.name:p.read_bytes() for p in (self.root/'requests').glob('*.json')}
        with self.assertRaisesRegex(GatewayError,'acceptance_receipt_checksum_mismatch'):
            ordered_inbox(self.root,{'a','b'})
        with self.assertRaisesRegex(GatewayError,'acceptance_receipt_checksum_mismatch'):
            submit(self.root,envelope(action='pause'))
        self.assertEqual({p.name:p.read_bytes() for p in (self.root/'requests').glob('*.json')},visible)

    def test_missing_receipt_never_downgrades_new_control_to_legacy(self):
        first=envelope(action='activate_latest');later=envelope(action='cancel')
        submit(self.root,first);submit(self.root,later)
        (self.root/'requests'/'.order'/(later['request_id']+'.json')).unlink()
        visible={p.name:p.read_bytes() for p in (self.root/'requests').glob('*.json')}
        with self.assertRaisesRegex(GatewayError,'unregistered_inbox_request'):
            ordered_inbox(self.root,{'a','b'})
        with self.assertRaisesRegex(GatewayError,'unregistered_inbox_request'):
            submit(self.root,later)
        self.assertEqual({p.name:p.read_bytes() for p in (self.root/'requests').glob('*.json')},visible)

    def test_legacy_baseline_is_frozen_and_only_whitelists_existing_payloads(self):
        first=envelope(request_id='ffffffff-ffff-ffff-ffff-ffffffffffff')
        inbox=self.root/'requests';inbox.mkdir()
        target=inbox/(first['request_id']+'.json');target.write_bytes(encode(first))
        later=envelope(action='pause');submit(self.root,later)
        os.utime(target,ns=(999999999999999999,999999999999999999))
        self.assertEqual([p.stem for p in ordered_inbox(self.root,{'a','b'})],[first['request_id'],later['request_id']])
        orphan=envelope(action='resume');(inbox/(orphan['request_id']+'.json')).write_bytes(encode(orphan))
        with self.assertRaisesRegex(GatewayError,'unregistered_inbox_request'):
            ordered_inbox(self.root,{'a','b'})

    def test_missing_frozen_legacy_request_fails_closed_without_recovery(self):
        legacy=envelope(action='cancel');inbox=self.root/'requests';inbox.mkdir()
        target=inbox/(legacy['request_id']+'.json');target.write_bytes(encode(legacy))
        submit(self.root,envelope(action='activate_latest'))
        target.unlink()
        with self.assertRaisesRegex(GatewayError,'legacy_request_missing'):
            ordered_inbox(self.root,{'a','b'})
        with self.assertRaisesRegex(GatewayError,'legacy_request_missing'):
            submit(self.root,envelope(action='resume'))
        self.assertFalse(target.exists())
        self.assertEqual(len(list(inbox.glob('*.json'))),1)

    def test_changed_frozen_legacy_payload_fails_closed(self):
        legacy=envelope(action='cancel');inbox=self.root/'requests';inbox.mkdir()
        target=inbox/(legacy['request_id']+'.json');target.write_bytes(encode(legacy))
        submit(self.root,envelope(action='activate_latest'))
        target.write_bytes(encode({**legacy,'action':'resume'}))
        with self.assertRaisesRegex(GatewayError,'legacy_request_payload_mismatch'):
            ordered_inbox(self.root,{'a','b'})
        with self.assertRaisesRegex(GatewayError,'legacy_request_payload_mismatch'):
            submit(self.root,envelope(action='pause'))

    def test_modified_legacy_baseline_is_rejected(self):
        submit(self.root,envelope())
        target=self.root/'requests'/'.order'/'legacy.json'
        value=json.loads(target.read_bytes());value['requests']=[{'request_id':str(uuid.uuid4()),'payload_hash':'0'*64}]
        target.write_bytes(encode(value))
        with self.assertRaisesRegex(GatewayError,'legacy_acceptance_checksum_mismatch'):
            ordered_inbox(self.root,{'a','b'})

    def test_reserved_sequence_gap_is_not_reused_after_crash(self):
        first=envelope();second=envelope()
        write=gateway._atomic_write
        def interrupted(path,data):
            if path.name==first['request_id']+'.json': raise OSError('simulated preacceptance crash')
            return write(path,data)
        with patch.object(gateway,'_atomic_write',side_effect=interrupted):
            with self.assertRaises(OSError):submit(self.root,first)
        submit(self.root,second)
        receipt=self.root/'requests'/'.order'/(second['request_id']+'.json')
        self.assertEqual(json.loads(receipt.read_bytes())['sequence'],2)

    def test_worker_recovers_accepted_request_without_gateway_retry(self):
        request=envelope();target=self.root/'requests'/(request['request_id']+'.json')
        write=gateway._atomic_write
        def interrupted(path,data):
            if path==target: raise OSError('simulated exposure crash')
            return write(path,data)
        with patch.object(gateway,'_atomic_write',side_effect=interrupted):
            with self.assertRaises(OSError):submit(self.root,request)
        self.assertEqual([p.stem for p in ordered_inbox(self.root,{'a','b'})],[request['request_id']])
        self.assertEqual(json.loads(target.read_bytes()),request)

    def test_invalid_requests_create_no_inbox(self):
        for changes, code in [
            ({"show_ids": ["unknown"]}, "unknown_show_id"),
            ({"action": "delete"}, "unsupported_action"),
            ({"request_id": "../../outside"}, "invalid_request_id"),
            ({"policy_revision": True}, "unsupported_policy_revision"),
            ({"policy_revision": 2}, "unsupported_policy_revision"),
            ({"show_ids": []}, "invalid_show_ids"),
            ({"show_ids": ["a", "a"]}, "duplicate_show_ids"),
            ({"created_at": "2026-11-07T10:00:00"}, "invalid_created_at"),
            ({"extra": "not-allowed"}, "invalid_request_fields"),
        ]:
            with self.subTest(code=code):
                with self.assertRaisesRegex(GatewayError, code):
                    submit(self.root, envelope(**changes))
        self.assertFalse((self.root / "requests").exists())

    def test_oversized_submit_rejected_before_any_write(self):
        with self.assertRaisesRegex(GatewayError, "input_too_large"):
            submit(self.root, envelope(created_at="x" * MAX_INPUT_BYTES))
        self.assertFalse((self.root / "requests").exists())

    def test_concurrent_different_payloads_same_id_only_one_wins(self):
        first = envelope()
        second = {**first, "action": "pause"}
        code = (
            "import json,sys; from home_media_follow.gateway import submit,GatewayError; "
            "r=json.load(sys.stdin); "
            "\ntry: print(json.dumps(submit(sys.argv[1],r)))"
            "\nexcept GatewayError as e: print(json.dumps({'error':e.code}))"
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        def run(request):
            result = subprocess.run([sys.executable, "-c", code, str(self.root)], input=json.dumps(request),
                                    capture_output=True, text=True, env=env, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(run, [first, second]))
        self.assertEqual(sum(row.get("ok", False) for row in results), 1)
        self.assertEqual(sum(row.get("error") == "request_id_payload_mismatch" for row in results), 1)
        stored = json.loads((self.root / "requests" / (first["request_id"] + ".json")).read_bytes())
        self.assertIn(stored, [first, second])
        self.assertFalse(list((self.root / "requests").glob("*.pending")))

    def test_status_is_read_only_and_filters_show(self):
        request = envelope()
        submit(self.root, request)
        path = self.write_state(snapshot(
            requests={request["request_id"]: {"request": request, "status": "applied"}},
            subscriptions={"a": {"active": True}, "b": {"active": False}},
            downloads={"job-a": {"show_id": "a", "request_id": request["request_id"], "phase": "published"},
                       "job-b": {"show_id": "b", "phase": "downloading"}},
            inventory={"one": {"show_id": "a"}, "two": {"show_id": "b"}},
        ))
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        result = status(self.root, {"show_ids": ["a"]})
        after = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertTrue(result["read_only"])
        self.assertEqual(result["state_status"], "ready")
        self.assertEqual([s["show_id"] for s in result["shows"]], ["a"])
        self.assertEqual(set(result["downloads"]), {"job-a"})
        self.assertEqual(set(result["subscriptions"]), {"a"})
        self.assertEqual(result["inventory"]["observed_count"], 1)

    def test_request_filter_returns_only_linked_jobs_and_matching_shows(self):
        request, unrelated = envelope(), envelope(show_ids=["b"])
        submit(self.root, request); submit(self.root, unrelated)
        self.write_state(snapshot(
            requests={request["request_id"]: {"request": request}, unrelated["request_id"]: {"request": unrelated}},
            downloads={"selected": {"show_id": "a", "request_id": request["request_id"]},
                       "unrelated": {"show_id": "a", "request_id": unrelated["request_id"]}},
        ))
        result = status(self.root, {"request_id": request["request_id"]})
        self.assertEqual(set(result["inbox"]), {request["request_id"]})
        self.assertEqual(set(result["requests"]), {request["request_id"]})
        self.assertEqual(set(result["downloads"]), {"selected"})
        self.assertEqual([s["show_id"] for s in result["shows"]], ["a"])

    def test_status_never_returns_config_secrets_or_state_urls(self):
        self.write_state(snapshot(downloads={"job": {"show_id": "a", "phase": "waiting",
            "source_url": "https://private.example/token", "cookie": "hidden-value",
            "error": "failed at https://private.example/signed?token=hidden"}}))
        result = json.dumps(status(self.root), ensure_ascii=False)
        for forbidden in ["https://", "never-output", "hidden-value", "token=hidden", "share_url", '"cookie"']:
            self.assertNotIn(forbidden, result)
        self.assertIn("waiting", result)

    def test_missing_state_reports_inbox_without_creating_state(self):
        request = envelope(); submit(self.root, request)
        result = status(self.root)
        self.assertEqual(result["state_status"], "missing")
        self.assertIn(request["request_id"], result["inbox"])
        self.assertFalse((self.root / "state").exists())

    def test_corrupt_state_is_reported_never_repaired(self):
        path = self.write_state(snapshot())
        for data in [b'{"incomplete":', b'[]', encode({**snapshot(), "_checksum": "wrong"})]:
            path.write_bytes(data)
            result = status(self.root)
            self.assertFalse(result["ok"])
            self.assertEqual(result["state_status"], "corrupt")
            self.assertEqual(path.read_bytes(), data)
        self.assertFalse((self.root / "state" / "events.jsonl").exists())

    def test_cli_limits_raw_input_and_never_echoes_it(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        result = subprocess.run([sys.executable, "-m", "home_media_follow.gateway", "submit", "--root", str(self.root)],
                                input=b"x" * (MAX_INPUT_BYTES + 1), capture_output=True, env=env)
        self.assertEqual(json.loads(result.stdout)["error_code"], "input_too_large")
        self.assertFalse((self.root / "requests").exists())

    def test_entry_wrapper_imports_package_with_isolated_python(self):
        app = self.root / "app"
        package = app / "home_media_follow"
        package.mkdir(parents=True)
        project = Path(__file__).resolve().parents[1]
        (app / "gateway.py").write_text("import sys\nfrom pathlib import Path\nsys.path.insert(0, str(Path(__file__).parent))\nfrom home_media_follow.gateway import main\nraise SystemExit(main())\n")
        shutil.copyfile(project / "src" / "home_media_follow" / "gateway.py", package / "gateway.py")
        (package / "__init__.py").write_text("")
        result = subprocess.run([sys.executable, "-I", str(app / "gateway.py"), "status", "--root", str(self.root)],
                                input=b'{"show_ids":[]}', capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(json.loads(result.stdout)["shows"]), 2)


if __name__ == "__main__":
    unittest.main()

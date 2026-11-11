from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from home_media_follow.store import Store, StoreCorruption


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name)

    def test_request_retry_and_payload_mismatch(self):
        store = Store(self.path)
        request = {"request_id": "same-action", "action": "activate_latest", "show_ids": ["show"], "policy_revision": 3}
        with store.locked():
            self.assertTrue(store.register_request(request))
            self.assertFalse(store.register_request(dict(request)))
            with self.assertRaises(ValueError):
                store.register_request({**request, "action": "cancel"})
        loaded = Store(self.path)
        self.assertEqual(loaded.state["requests"]["same-action"]["status"], "accepted")

    def test_result_survives_failed_snapshot(self):
        store = Store(self.path)
        with store.locked():
            store.state["operations"]["transfer"] = {"status": "unknown", "source_id": "9007199254740993"}
            store.event("transfer_intent", {"operation_id": "transfer"})
            store.state["operations"]["transfer"]["status"] = "cloud_verified"
            with patch.object(store, "_snapshot", side_effect=OSError("simulated replace failure")):
                with self.assertRaises(OSError):
                    store.event("transfer_result", {"operation_id": "transfer"})
        loaded = Store(self.path)
        self.assertEqual(loaded.state["operations"]["transfer"]["status"], "cloud_verified")
        self.assertEqual(loaded.state["operations"]["transfer"]["source_id"], "9007199254740993")

    def test_partial_tail_isolated_without_losing_durable_intent(self):
        store = Store(self.path)
        with store.locked():
            store.state["operations"]["x"] = {"status": "unknown"}
            store.event("intent", {"operation_id": "x"})
        with store.journal_path.open("ab") as handle:
            handle.write(b'{"seq":2,"kind":"res')
        loaded = Store(self.path)
        self.assertEqual(loaded.state["operations"]["x"]["status"], "unknown")
        self.assertEqual(len(list(self.path.glob("events.corrupt-*.jsonl"))), 1)
        self.assertTrue(loaded.journal_path.read_bytes().endswith(b"\n"))

    def test_corrupt_middle_fails_closed(self):
        store = Store(self.path)
        with store.locked():
            store.event("one", {})
            store.event("two", {})
        data = store.journal_path.read_bytes().splitlines(keepends=True)
        data[0] = b'{"seq":1,"_checksum":"bad"}\n'
        store.journal_path.write_bytes(b"".join(data))
        with self.assertRaises(StoreCorruption):
            Store(self.path)

    def test_corrupt_snapshot_rebuilt_from_journal(self):
        store = Store(self.path)
        with store.locked():
            store.state["subscriptions"]["show"] = {"watermark": {"season": 1, "episode": 199}}
            store.save()
        store.path.write_bytes(b"broken snapshot")
        loaded = Store(self.path)
        self.assertEqual(loaded.state["subscriptions"]["show"]["watermark"]["episode"], 199)

    def test_two_instances_refresh_after_lock(self):
        first = Store(self.path)
        second = Store(self.path)
        with first.locked():
            first.state["inventory"]["old"] = {"local_requested": False}
            first.save()
        with second.locked():
            self.assertIn("old", second.state["inventory"])
            second.state["inventory"]["new"] = {"local_requested": False}
            second.save()
        loaded = Store(self.path)
        self.assertEqual(set(loaded.state["inventory"]), {"old", "new"})
        self.assertEqual(loaded.state["downloads"], {})

    def test_unlocked_event_is_rejected(self):
        with self.assertRaises(RuntimeError):
            Store(self.path).event("unsafe", {})

    def test_os_lock_excludes_another_process(self):
        store = Store(self.path)
        code = "from home_media_follow.store import Store; import sys; Store(sys.argv[1], lock_timeout=0.1)"
        with store.locked():
            blocked = subprocess.run([sys.executable, "-c", code, str(self.path)], capture_output=True, text=True, timeout=5)
            self.assertNotEqual(blocked.returncode, 0)
            self.assertIn("TimeoutError", blocked.stderr)
        available = subprocess.run([sys.executable, "-c", code, str(self.path)], capture_output=True, text=True, timeout=5)
        self.assertEqual(available.returncode, 0, available.stderr)


if __name__ == "__main__":
    unittest.main()

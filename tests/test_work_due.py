import copy
import unittest

from home_media_follow.core import activation, logical_key
from home_media_follow.work_due import pending_cloud_keys, source_work_due


def candidate(cid="current", episode=100, *, status="verified", width=3840, name=None, show="show"):
    return {"record_type": "candidate", "candidate_id": cid, "show_id": show,
            "season": 1, "episode": episode, "part": None,
            "name": name or cid + ".mp4", "source_path": "/source/" + (name or cid + ".mp4"),
            "probe": {"status": status, "width": width, "height": width * 9 // 16}}


def state_and_sub(*candidates):
    state = {"inventory": {c["candidate_id"]: c for c in candidates}, "operations": {}, "downloads": {}}
    sub = {"show_id": "show", "activation": activation([candidate()], "complete"), "assets": {},
           "scan_candidates": [c["candidate_id"] for c in candidates]}
    return state, sub


def install(sub, value, *, managed=True):
    sub["assets"][logical_key(value)] = {"candidate": value, "managed_local": managed}


class WorkDueTests(unittest.TestCase):
    def test_activation_is_due_without_scanning_inventory(self):
        self.assertTrue(source_work_due({}, {}, 100))
        self.assertTrue(source_work_due({}, {"needs_activation": True}, 100))

    def test_pending_cloud_keys_include_history_and_recoverable_phases(self):
        old = candidate("old", 1)
        state, sub = state_and_sub(old)
        state["operations"] = {
            "transfer": {"kind": "cloud_transfer", "show_id": "show", "candidate_id": "old", "stage": "write_unknown"},
            "replacement": {"kind": "cloud_replacement", "show_id": "show", "logical_key": logical_key(candidate("new", 2)), "phase": "old_move_intent"},
        }
        self.assertEqual(pending_cloud_keys(state, "show"), {logical_key(old), logical_key(candidate("new", 2))})
        self.assertTrue(source_work_due(state, sub, 100))

    def test_pending_switched_cloud_waits_for_inflight_local_job_without_source_calls(self):
        value = candidate()
        state, sub = state_and_sub(value)
        key = logical_key(value)
        state["operations"]["cloud"] = {"kind": "cloud_replacement", "show_id": "show",
                                         "logical_key": key, "phase": "switched"}
        for status in ("queued", "downloading", "queue_unknown", "download_error", "published", "adopted"):
            state["downloads"]["job"] = {"show_id": "show", "logical_key": key, "status": status}
            self.assertEqual(pending_cloud_keys(state, "show"), {key})
            self.assertFalse(source_work_due(state, sub, 100), status)
        state["downloads"]["job"]["status"] = "complete"
        self.assertTrue(source_work_due(state, sub, 100))

    def test_paused_cancelled_subscriptions_never_start_source_work(self):
        state, sub = state_and_sub(candidate())
        state["operations"]["transfer"] = {"kind": "cloud_transfer", "show_id": "show", "candidate_id": "current"}
        for status in ("paused", "cancelled"):
            sub.update(status=status, needs_activation=True)
            self.assertFalse(source_work_due(state, sub, 100))
            sub.pop("activation", None)
            self.assertFalse(source_work_due(state, sub, 100))

    def test_confirmed_and_explicitly_terminal_operations_are_excluded(self):
        state, _ = state_and_sub(candidate())
        for field in ("status", "phase", "stage"):
            for terminal in ("confirmed", "cancelled", "rejected", "failed", "rolled_back"):
                state["operations"] = {"op": {"kind": "cloud_transfer", "show_id": "show", "candidate_id": "current", field: terminal}}
                self.assertEqual(pending_cloud_keys(state, "show"), set())
        state["operations"]["op"] = {"kind": "cloud_transfer", "show_id": "show", "candidate_id": "current", "terminal": True}
        self.assertEqual(pending_cloud_keys(state, "show"), set())

    def test_wrong_show_missing_candidate_and_non_cloud_operations_are_ignored(self):
        state, _ = state_and_sub(candidate())
        state["operations"] = {
            "wrong": {"kind": "cloud_transfer", "show_id": "other", "candidate_id": "current"},
            "missing": {"kind": "cloud_transfer", "show_id": "show", "candidate_id": "missing"},
            "local": {"kind": "local_replacement", "show_id": "show", "logical_key": logical_key(candidate())},
            "wrong-key": {"kind": "cloud_replacement", "show_id": "show", "logical_key": logical_key(candidate(show="other"))},
        }
        self.assertEqual(pending_cloud_keys(state, "show"), set())

    def test_completed_4k_assets_do_not_poll_unknown_same_tier_variants(self):
        current, alternate = candidate(), candidate("alternate", status="unknown", name="100-4K.mp4")
        state, sub = state_and_sub(current, alternate)
        install(sub, current)
        state["downloads"]["done"] = {"show_id": "show", "logical_key": logical_key(current), "status": "complete", "candidate": current}
        self.assertFalse(source_work_due(state, sub, 100))

    def test_missing_local_asset_can_use_verified_or_due_unverified_candidate(self):
        for status in ("verified", "unknown", "rejected"):
            value = candidate(status=status)
            state, sub = state_and_sub(value)
            value["next_probe_at"] = 100
            self.assertTrue(source_work_due(state, sub, 100))
            if status != "verified":
                self.assertFalse(source_work_due(state, sub, 99))

    def test_history_and_candidates_outside_latest_scan_do_not_create_work(self):
        historical, removed = candidate("old", 99), candidate("removed", 101)
        state, sub = state_and_sub(historical)
        state["inventory"]["removed"] = removed
        self.assertFalse(source_work_due(state, sub, 100))
        sub["scan_candidates"].append("removed")
        self.assertTrue(source_work_due(state, sub, 100))

    def test_every_inflight_download_status_defers_source_work_for_same_key(self):
        value = candidate()
        state, sub = state_and_sub(value)
        for status in ("queue_intent", "queue_unknown", "queued", "downloading", "download_error", "paused", "published", "adopted"):
            state["downloads"]["job"] = {"show_id": "show", "logical_key": logical_key(value), "status": status, "candidate": value}
            self.assertFalse(source_work_due(state, sub, 100), status)

    def test_inflight_other_episode_does_not_suppress_missed_requested_episode(self):
        state, sub = state_and_sub(candidate("new", 101))
        state["downloads"]["job"] = {"show_id": "show", "logical_key": logical_key(candidate()), "status": "downloading"}
        self.assertTrue(source_work_due(state, sub, 100))

    def test_local_gate_rejected_exact_candidate_is_never_repolled(self):
        value = candidate(status="unknown")
        state, sub = state_and_sub(value)
        state["downloads"]["bad"] = {"show_id": "show", "logical_key": logical_key(value), "status": "rejected", "candidate": value}
        self.assertFalse(source_work_due(state, sub, 100))
        alternate = candidate("different", status="unknown")
        state["inventory"]["different"] = alternate
        sub["scan_candidates"].append("different")
        self.assertTrue(source_work_due(state, sub, 100))

    def test_managed_fallback_rechecks_due_unverified_and_verified_higher_tier(self):
        current = candidate(width=1920)
        alternate = candidate("alternate", status="unknown")
        state, sub = state_and_sub(current, alternate)
        install(sub, current)
        alternate["next_probe_at"] = 101
        self.assertFalse(source_work_due(state, sub, 100))
        self.assertTrue(source_work_due(state, sub, 101))
        alternate["probe"] = {"status": "verified", "width": 1920, "height": 1080}
        self.assertFalse(source_work_due(state, sub, 101))
        alternate["probe"].update(width=3840, height=2160)
        self.assertTrue(source_work_due(state, sub, 100))

    def test_unmanaged_current_is_never_a_source_upgrade_trigger(self):
        state, sub = state_and_sub(candidate("higher", width=7680))
        install(sub, candidate(), managed=False)
        self.assertFalse(source_work_due(state, sub, 100))

    def test_4k_rechecks_only_due_explicit_8k_or_verified_higher_tier(self):
        current, higher = candidate(), candidate("higher", status="unknown", name="100-8K60FPS.mkv")
        state, sub = state_and_sub(current, higher)
        install(sub, current)
        higher["next_probe_at"] = 101
        self.assertFalse(source_work_due(state, sub, 100))
        self.assertTrue(source_work_due(state, sub, 101))
        higher["probe"] = {"status": "verified", "width": 7680, "height": 4320}
        self.assertTrue(source_work_due(state, sub, 100))
        higher["probe"].update(width=3840, height=2160)
        self.assertFalse(source_work_due(state, sub, 101))

    def test_8k_current_does_not_poll_unverified_8k_variant(self):
        current = candidate(width=7680)
        state, sub = state_and_sub(current, candidate("other", status="unknown", name="8K.mkv"))
        install(sub, current)
        self.assertFalse(source_work_due(state, sub, 100))

    def test_4k_current_does_not_recheck_explicit_8k_that_measured_below_minimum(self):
        current = candidate()
        rejected_quality = candidate("fake-8k", status="rejected", width=1280, name="100-8K.mkv")
        state, sub = state_and_sub(current, rejected_quality)
        install(sub, current)
        self.assertFalse(source_work_due(state, sub, 100))

    def test_pure_functions_leave_nested_state_and_subscription_unchanged(self):
        state, sub = state_and_sub(candidate(), candidate("unknown", status="unknown"))
        install(sub, candidate(width=1920))
        before = copy.deepcopy((state, sub))
        self.assertTrue(source_work_due(state, sub, 100))
        pending_cloud_keys(state, "show")
        self.assertEqual((state, sub), before)


if __name__ == "__main__":
    unittest.main()

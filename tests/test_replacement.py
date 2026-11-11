from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from home_media_follow.replacement import Replacement, ReplacementConflict, file_sha256
from home_media_follow.store import Store


class SimulatedCrash(BaseException):
    pass


class ReplacementTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.target = self.root / "published.mp4"
        self.candidate = self.root / "candidate.mp4"
        self.target.write_bytes(b"trusted old 1080p video")
        self.candidate.write_bytes(b"verified new 4K video")
        self.old = self.target.read_bytes()
        self.new = self.candidate.read_bytes()
        self.old_digest = file_sha256(self.target)
        self.store = Store(self.root / "state")

    def prepare(self, operation="upgrade"):
        transaction = Replacement(self.store, operation)
        transaction.prepare(self.candidate, self.target, self.old_digest, {"backup_root": str(self.root / "recovery" / operation)})
        return transaction

    def test_success_keeps_exact_recoverable_old_copy(self):
        with self.store.locked():
            transaction = self.prepare()
            result = transaction.run(lambda path: path.read_bytes() == self.new)
            self.assertEqual(result["phase"], "verified")
            self.assertEqual(self.target.read_bytes(), self.new)
            self.assertEqual(Path(result["backup"]).read_bytes(), self.old)
            transaction.confirm()
            self.assertTrue(Path(result["backup"]).exists())

    def test_rejected_installed_target_restores_old(self):
        with self.store.locked():
            transaction = self.prepare()
            result = transaction.run(lambda path: path != self.target)
            self.assertEqual(result["phase"], "rolled_back")
            self.assertEqual(self.target.read_bytes(), self.old)
            self.assertEqual(self.candidate.read_bytes(), self.new)

    def test_unrelated_target_and_existing_backup_are_never_retired(self):
        with self.store.locked():
            with self.assertRaises(ReplacementConflict):
                Replacement(self.store, "untrusted").prepare(self.candidate, self.target)
            recovery = self.root / "occupied"
            recovery.mkdir()
            (recovery / "previous.mp4").write_bytes(b"other data")
            with self.assertRaises(ReplacementConflict):
                Replacement(self.store, "occupied").prepare(self.candidate, self.target, self.old_digest, {"backup_root": str(recovery)})
        self.assertEqual(self.target.read_bytes(), self.old)
        self.assertEqual((recovery / "previous.mp4").read_bytes(), b"other data")

    def test_crash_after_every_transition_is_recoverable(self):
        for phase in ("backup_intent", "backed_up", "install_intent", "switched", "verified"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                old, new = root / "old.mp4", root / "new.mp4"
                old.write_bytes(self.old); new.write_bytes(self.new)
                store = Store(root / "state")
                with store.locked():
                    transaction = Replacement(store, "upgrade")
                    transaction.prepare(new, old, file_sha256(old), {"backup_root": str(root / "recovery")})
                    original = transaction._record
                    def interrupted(next_phase, **updates):
                        result = original(next_phase, **updates)
                        if next_phase == phase:
                            raise SimulatedCrash()
                        return result
                    with patch.object(transaction, "_record", side_effect=interrupted):
                        with self.assertRaises(SimulatedCrash):
                            transaction.run(lambda path: True)
                reloaded = Store(root / "state")
                with reloaded.locked():
                    result = Replacement(reloaded, "upgrade").recover(lambda path: True)
                    self.assertEqual(result["phase"], "verified")
                    self.assertEqual(old.read_bytes(), self.new)
                    self.assertEqual(Path(result["backup"]).read_bytes(), self.old)

    def test_crash_after_os_replace_before_result_is_reconciled(self):
        import os
        real_replace = os.replace
        for moved_source in (self.target, self.candidate):
            with self.subTest(moved_source=moved_source):
                self.setUp()
                source = self.target if moved_source.name == "published.mp4" else self.candidate
                with self.store.locked():
                    transaction = self.prepare()
                    def interrupted(old, new):
                        real_replace(old, new)
                        if Path(old) == source:
                            raise SimulatedCrash()
                    with patch("home_media_follow.replacement.os.replace", side_effect=interrupted):
                        with self.assertRaises(SimulatedCrash):
                            transaction.run(lambda path: True)
                loaded = Store(self.root / "state")
                with loaded.locked():
                    result = Replacement(loaded, "upgrade").recover(lambda path: True)
                    self.assertEqual(self.target.read_bytes(), self.new)
                    self.assertEqual(Path(result["backup"]).read_bytes(), self.old)

    def test_changed_target_is_not_overwritten(self):
        with self.store.locked():
            transaction = self.prepare()
            self.target.write_bytes(b"unrelated replacement by another owner")
            with self.assertRaises(ReplacementConflict):
                transaction.run(lambda path: True)
            self.assertEqual(self.target.read_bytes(), b"unrelated replacement by another owner")

    def test_install_io_failure_immediately_restores_old(self):
        import os
        original = os.replace
        with self.store.locked():
            transaction = self.prepare()
            def fail_install(source, target):
                if Path(source) == self.candidate:
                    raise OSError("simulated file lock")
                return original(source, target)
            with patch("home_media_follow.replacement.os.replace", side_effect=fail_install):
                with self.assertRaises(OSError):
                    transaction.run(lambda path: True)
            self.assertEqual(transaction.operation["phase"], "rolled_back")
            self.assertEqual(self.target.read_bytes(), self.old)

    def test_recovery_verification_rejection_restores_already_backed_up_old(self):
        with self.store.locked():
            transaction = self.prepare()
            original = transaction._record
            def interrupt(phase, **updates):
                result = original(phase, **updates)
                if phase == "backed_up":
                    raise SimulatedCrash()
                return result
            with patch.object(transaction, "_record", side_effect=interrupt):
                with self.assertRaises(SimulatedCrash):
                    transaction.run(lambda path: True)
        loaded = Store(self.root / "state")
        with loaded.locked():
            transaction = Replacement(loaded, "upgrade")
            # Rejected recovery cannot leave the old viewing copy displaced.
            result = transaction.recover(lambda path: False)
            self.assertEqual(result["phase"], "rolled_back")
            self.assertEqual(self.target.read_bytes(), self.old)

    def test_rollback_interruption_recovers_old(self):
        import os
        original = os.replace
        with self.store.locked():
            transaction = self.prepare()
            transaction.run(lambda path: True)
            def interrupt(source, target):
                original(source, target)
                if Path(source) == self.target:
                    raise SimulatedCrash()
            with patch("home_media_follow.replacement.os.replace", side_effect=interrupt):
                with self.assertRaises(SimulatedCrash):
                    transaction.rollback()
        loaded = Store(self.root / "state")
        with loaded.locked():
            result = Replacement(loaded, "upgrade").recover()
            self.assertEqual(result["phase"], "rolled_back")
            self.assertEqual(self.target.read_bytes(), self.old)

    def test_symlink_candidate_rejected(self):
        alias = self.root / "alias.mp4"
        try:
            alias.symlink_to(self.candidate)
        except OSError:
            self.skipTest("Symlinks unavailable under this account")
        with self.store.locked():
            with self.assertRaises(ReplacementConflict):
                Replacement(self.store, "symlink").prepare(alias, self.target, self.old_digest)

    def test_same_operation_cannot_change_candidate_content(self):
        with self.store.locked():
            transaction = self.prepare()
            self.candidate.write_bytes(b"different candidate")
            with self.assertRaises(ReplacementConflict):
                transaction.prepare(self.candidate, self.target, self.old_digest, {"backup_root": str(self.root / "recovery" / "upgrade")})


if __name__ == "__main__":
    unittest.main()

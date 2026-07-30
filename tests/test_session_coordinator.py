import json
import os
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path

from sql_lab_extractor.auth import BrowserSession, resolve_profile_dir
from sql_lab_extractor.session import (
    CrossProcessSessionLock,
    SessionCoordinator,
    SessionLockTimeout,
    SessionSnapshot,
)


def _fake_session(tag: str) -> BrowserSession:
    return BrowserSession(f"cookie-{tag}", f"csrf-{tag}")


class SessionCoordinatorTests(unittest.TestCase):
    def test_concurrent_invalidations_of_one_generation_refresh_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile_dir = Path(tmp) / "profile"
            calls = 0
            calls_lock = threading.Lock()
            release = threading.Event()
            started = threading.Event()

            def refresh() -> BrowserSession:
                nonlocal calls
                with calls_lock:
                    calls += 1
                # Ignore the bootstrap refresh performed by get_snapshot().
                if calls > 1:
                    started.set()
                    release.wait(timeout=10)
                return _fake_session(f"r{calls}")

            coordinator = SessionCoordinator(profile_dir, refresh=refresh)
            stale = coordinator.get_snapshot()
            barrier = threading.Barrier(6)
            results: list[SessionSnapshot] = []
            errors: list[BaseException] = []

            def worker() -> None:
                try:
                    barrier.wait(timeout=10)
                    results.append(coordinator.invalidate(stale.generation))
                except BaseException as error:  # noqa: BLE001 - surfaced by assertion
                    errors.append(error)

            threads = [threading.Thread(target=worker) for _ in range(5)]
            for thread in threads:
                thread.start()
            barrier.wait(timeout=10)
            self.assertTrue(started.wait(timeout=10), "coordinated refresh was never invoked")
            release.set()
            for thread in threads:
                thread.join(timeout=10)

            self.assertEqual(errors, [])
            self.assertEqual(calls, 2, "bootstrap plus exactly one refresh must run for one stale generation")
            self.assertEqual(len(results), 5)
            self.assertTrue(all(isinstance(snapshot, SessionSnapshot) for snapshot in results))
            self.assertTrue(all(snapshot.generation > stale.generation for snapshot in results))
            self.assertEqual({snapshot.generation for snapshot in results}, {results[0].generation})
            self.assertTrue(all(snapshot.session is results[0].session for snapshot in results))

    def test_invalidate_with_old_generation_does_not_refresh_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = 0

            def refresh() -> BrowserSession:
                nonlocal calls
                calls += 1
                return _fake_session(f"r{calls}")

            coordinator = SessionCoordinator(Path(tmp) / "profile", refresh=refresh)
            first = coordinator.get_snapshot()
            bootstrap_calls = calls

            second = coordinator.invalidate(first.generation)
            third = coordinator.invalidate(first.generation)
            self.assertEqual(calls - bootstrap_calls, 1, "stale generations must not trigger another refresh")
            self.assertEqual(third.generation, second.generation)
            self.assertIs(third.session, second.session)

    def test_default_profile_resolves_to_persistent_artifacts_path(self):
        with unittest.mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(
                resolve_profile_dir(),
                Path("artifacts/session/profile").resolve(),
            )

    def test_generation_metadata_is_non_secret_and_lock_is_released(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile_dir = Path(tmp) / "profile"
            coordinator = SessionCoordinator(profile_dir, refresh=lambda: _fake_session("secret-token"))
            snapshot = coordinator.get_snapshot()

            metadata_path = profile_dir / "generation.json"
            self.assertTrue(metadata_path.is_file())
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(metadata["generation"], snapshot.generation)
            self.assertIn("created_at", metadata)
            self.assertIn("pid", metadata)
            serialized = json.dumps(metadata)
            self.assertNotIn(snapshot.session.cookie_header, serialized)
            self.assertNotIn(snapshot.session.csrf_token, serialized)
            self.assertNotIn("secret-token", serialized)
            self.assertNotIn("cookie", serialized.lower())
            self.assertNotIn("csrf", serialized.lower())
            self.assertFalse(
                (profile_dir / "session.lock").exists(),
                "lock file must be released after refresh",
            )
    def test_load_session_forwards_coordinator_profile_to_browser_bootstrap(self):
        from sql_lab_extractor.__main__ import _load_session

        profile_dir = Path("artifacts/run/browser-profile")
        with unittest.mock.patch("sql_lab_extractor.__main__.bootstrap_browser_session", return_value=_fake_session("browser")) as bootstrap:
            session = _load_session("https://app.example", profile_dir)

        self.assertEqual(session, _fake_session("browser"))
        bootstrap.assert_called_once_with("https://app.example", profile_dir=profile_dir)


class StaleSessionLockTests(unittest.TestCase):
    def _write_lock(self, path: Path, pid: int, age_seconds: float) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"pid={pid} created_at=2020-01-01T00:00:00+00:00\n", encoding="utf-8")
        # Backdate the lock so it appears older than the stale threshold.
        old = time.time() - age_seconds
        os.utime(path, (old, old))

    def test_recovers_stale_lock_with_dead_owner_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "session.lock"
            # A PID that is essentially never alive.
            self._write_lock(lock_path, pid=2_000_000, age_seconds=10_000.0)
            lock = CrossProcessSessionLock(lock_path, stale_after=1.0, poll_interval=0.01)
            with lock:
                self.assertTrue(lock_path.is_file())
            # Lock was acquired (recreated) and then released on exit.
            self.assertFalse(lock_path.exists())

    def test_recovers_stale_lock_when_owner_pid_was_reused_by_dead_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "session.lock"
            # The original owner is long dead, but its PID may have been reused.
            # Recovery must still succeed for a genuinely stale lock: the deciding
            # signal is lock age, with PID liveness only as a guard against
            # deleting a lock that is still actively held.
            self._write_lock(lock_path, pid=os.getpid(), age_seconds=10_000.0)
            lock = CrossProcessSessionLock(lock_path, stale_after=1.0, poll_interval=0.01)
            with lock:
                self.assertTrue(lock_path.is_file())
            self.assertFalse(lock_path.exists())

    def test_does_not_delete_fresh_lock_held_by_reused_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "session.lock"
            # A freshly created lock whose recorded PID happens to match a live
            # process (PID reuse). Recovery must leave it untouched.
            self._write_lock(lock_path, pid=os.getpid(), age_seconds=0.0)
            lock = CrossProcessSessionLock(
                lock_path,
                stale_after=1.0,
                wait_timeout=0.05,
                poll_interval=0.01,
            )
            # The fresh lock should block acquisition within the bounded timeout.
            with self.assertRaises(SessionLockTimeout):
                with lock:
                    self.fail("must not acquire a lock that is still fresh and held")
            # The fresh lock file must still exist and still belong to its owner.
            self.assertTrue(lock_path.is_file())
            self.assertIn("pid=", lock_path.read_text(encoding="utf-8"))

    def test_stale_takeover_preserves_fresh_replacement_created_during_quarantine(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "session.lock"
            self._write_lock(lock_path, pid=2_000_000, age_seconds=10_000.0)
            lock = CrossProcessSessionLock(lock_path, stale_after=1.0)
            original_replace = os.replace

            def quarantine_then_replace(source, destination):
                original_replace(source, destination)
                lock_path.write_text("pid=123 created_at=2030-01-01T00:00:00+00:00\n", encoding="utf-8")

            with unittest.mock.patch("sql_lab_extractor.session.os.replace", side_effect=quarantine_then_replace):
                lock._recover_stale_lock()

            self.assertEqual(lock_path.read_text(encoding="utf-8"), "pid=123 created_at=2030-01-01T00:00:00+00:00\n")

if __name__ == "__main__":
    unittest.main()

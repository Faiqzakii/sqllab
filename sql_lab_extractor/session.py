from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .auth import BrowserSession, DEFAULT_PROFILE_DIR

logger = logging.getLogger(__name__)

DEFAULT_SESSION_DIR = DEFAULT_PROFILE_DIR.parent
DEFAULT_LOCK_PATH = DEFAULT_SESSION_DIR / "session.lock"
GENERATION_METADATA_NAME = "generation.json"
LOCK_STALE_AFTER_SECONDS = 600.0
LOCK_WAIT_POLL_SECONDS = 0.05
LOCK_WAIT_TIMEOUT_SECONDS = 120.0


class SessionLockTimeout(RuntimeError):
    pass


@dataclass(frozen=True)
class SessionSnapshot:
    generation: int
    session: BrowserSession


class CrossProcessSessionLock:
    """Bounded cross-process mutex built on an atomic lock file."""

    def __init__(
        self,
        path: Path,
        stale_after: float = LOCK_STALE_AFTER_SECONDS,
        wait_timeout: float = LOCK_WAIT_TIMEOUT_SECONDS,
        poll_interval: float = LOCK_WAIT_POLL_SECONDS,
    ) -> None:
        self._path = Path(path)
        self._stale_after = stale_after
        self._wait_timeout = wait_timeout
        self._poll_interval = poll_interval

    def __enter__(self) -> "CrossProcessSessionLock":
        self._path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self._wait_timeout
        while True:
            try:
                fd = os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                self._recover_stale_lock()
                if time.monotonic() >= deadline:
                    raise SessionLockTimeout("Session lock wait timed out")
                time.sleep(self._poll_interval)
                continue
            try:
                payload = f"pid={os.getpid()} created_at={datetime.now(timezone.utc).isoformat()}\n"
                os.write(fd, payload.encode("utf-8"))
            finally:
                os.close(fd)
            return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        try:
            self._path.unlink()
        except FileNotFoundError:
            logger.warning("Session lock %s already removed", self._path)

    def _recover_stale_lock(self) -> None:
        snapshot = self._lock_snapshot()
        if snapshot is None:
            return
        stat, content = snapshot
        age = time.time() - stat.st_mtime
        if age <= self._stale_after:
            return
        owner_pid, created_at = self._read_owner_metadata(content)
        if owner_pid is not None and created_at is not None and not _pid_started_after(owner_pid, created_at):
            return
        if self._lock_snapshot() != snapshot:
            return
        quarantine = self._path.with_name(f".{self._path.name}.{os.getpid()}.{time.monotonic_ns()}.stale")
        try:
            os.replace(self._path, quarantine)
        except FileNotFoundError:
            return
        except OSError:
            logger.warning("Could not quarantine stale session lock %s", self._path, exc_info=True)
            return
        try:
            quarantined = self._lock_snapshot_at(quarantine)
            if quarantined != snapshot:
                if not self._path.exists():
                    os.replace(quarantine, self._path)
                return
            # A replacement lock may have appeared after the atomic quarantine.
            # Never unlink it; only delete the quarantined stale inode.
            replacement = self._lock_snapshot()
            if replacement is not None:
                logger.warning("Preserved replacement session lock %s during stale recovery", self._path)
                return
            logger.warning(
                "Recovered stale session lock %s (age=%.0fs pid=%s)",
                self._path,
                age,
                owner_pid if owner_pid is not None else "unknown",
            )
        finally:
            quarantine.unlink(missing_ok=True)

    def _lock_snapshot(self) -> tuple[os.stat_result, str] | None:
        return self._lock_snapshot_at(self._path)

    @staticmethod
    def _lock_snapshot_at(path: Path) -> tuple[os.stat_result, str] | None:
        try:
            with path.open("r", encoding="utf-8") as lock_file:
                content = lock_file.read()
            return path.stat(), content
        except (FileNotFoundError, OSError):
            return None

    @staticmethod
    def _read_owner_metadata(content: str) -> tuple[int | None, datetime | None]:
        values = dict(token.split("=", 1) for token in content.split() if "=" in token)
        try:
            pid = int(values["pid"])
        except (KeyError, ValueError):
            pid = None
        try:
            created_at = datetime.fromisoformat(values["created_at"])
        except (KeyError, ValueError):
            created_at = None
        return pid, created_at



def _pid_started_after(pid: int, created_at: datetime) -> bool:
    """Return whether a live PID started after this lock was created."""
    if not _pid_alive(pid):
        return True
    if os.name != "nt":
        try:
            start_ticks = int(Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()[21])
            boot_time = datetime.fromtimestamp(time.time() - time.clock_gettime(time.CLOCK_BOOTTIME), timezone.utc)
            return boot_time.timestamp() + start_ticks / os.sysconf("SC_CLK_TCK") > created_at.timestamp()
        except (OSError, ValueError, IndexError, AttributeError):
            return False
    import ctypes

    handle = ctypes.windll.kernel32.OpenProcess(0x0400, False, pid)
    if not handle:
        return True
    try:
        created = ctypes.c_ulonglong()
        exit_time = ctypes.c_ulonglong()
        kernel_time = ctypes.c_ulonglong()
        user_time = ctypes.c_ulonglong()
        if not ctypes.windll.kernel32.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        ):
            return False
        # FILETIME counts 100 ns since 1601-01-01 UTC.
        started_at = datetime.fromtimestamp(created.value / 10_000_000 - 11_644_473_600, timezone.utc)
        return started_at > created_at
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)
def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        synchronize = 0x00100000
        handle = ctypes.windll.kernel32.OpenProcess(synchronize, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class SessionCoordinator:
    """Owns one persistent Camoufox profile and publishes immutable session generations."""

    def __init__(
        self,
        profile_dir: Path | None = None,
        lock_path: Path | None = None,
        refresh: Callable[[], BrowserSession] | None = None,
    ) -> None:
        self._profile_dir = Path(profile_dir) if profile_dir is not None else DEFAULT_PROFILE_DIR
        self._lock_path = Path(lock_path) if lock_path is not None else self._default_lock_path()
        if refresh is None:
            raise ValueError("refresh callable wajib diisi")
        self._refresh = refresh
        self._condition = threading.Condition()
        self._snapshot: SessionSnapshot | None = None

    @property
    def profile_dir(self) -> Path:
        return self._profile_dir

    @property
    def lock_path(self) -> Path:
        return self._lock_path

    def get_snapshot(self) -> SessionSnapshot:
        with self._condition:
            if self._snapshot is None:
                self._refresh_locked()
            assert self._snapshot is not None
            return self._snapshot

    def invalidate(self, generation: int) -> SessionSnapshot:
        """Refresh only when the caller's generation is still the current one."""
        with self._condition:
            if self._snapshot is None or self._snapshot.generation == generation:
                self._refresh_locked(generation)
            assert self._snapshot is not None
            return self._snapshot

    def _refresh_locked(self, stale_generation: int | None = None) -> None:
        """Run one refresh under cross-process lock; Condition already held."""
        with CrossProcessSessionLock(self._lock_path):
            # Re-check under the cross-process lock: another worker may have
            # already replaced the stale generation while we waited.
            if (
                stale_generation is not None
                and self._snapshot is not None
                and self._snapshot.generation != stale_generation
            ):
                return
            session = self._refresh()
            snapshot = SessionSnapshot(
                generation=(self._snapshot.generation + 1) if self._snapshot else 1,
                session=session,
            )
            self._write_generation_metadata(snapshot)
            self._snapshot = snapshot
            self._condition.notify_all()
            logger.info(
                "Session generation %d published for profile %s",
                snapshot.generation,
                self._profile_dir,
            )

    def _default_lock_path(self) -> Path:
        if self._profile_dir.parent.name == "session":
            return self._profile_dir.parent / "session.lock"
        return self._profile_dir / "session.lock"

    def _write_generation_metadata(self, snapshot: SessionSnapshot) -> None:
        """Persist non-secret generation metadata atomically; never cookies or CSRF."""
        metadata = {
            "generation": snapshot.generation,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "pid": os.getpid(),
        }
        self._profile_dir.mkdir(parents=True, exist_ok=True)
        target = self._profile_dir / GENERATION_METADATA_NAME
        fd, temp_name = tempfile.mkstemp(
            dir=str(self._profile_dir),
            prefix=".generation-",
            suffix=".partial",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(metadata, handle, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, target)
        except BaseException:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise

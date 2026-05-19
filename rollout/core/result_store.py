"""
Result-file abstractions for the rollout pipeline.

`ResultStore` owns a single ``results_*.jsonl`` file and provides:

- Atomic append (``append_line``) with `flush + fsync` so a hard kill
  leaves only fully-written lines on disk (also enforced by Phase 0's
  `_save_result`).
- An advisory exclusive ``fcntl`` lock (``acquire_lock`` / ``release_lock``)
  so two concurrent rollout processes pointing at the same stable
  output file fail fast instead of silently interleaving bytes.
- A streaming ``iter_lines`` helper for Phase 3's resume scan.

The lock is intentionally process-level (advisory POSIX flock on a
sidecar ``.lock`` file). It is *not* a substitute for the in-process
``asyncio.Lock`` used by `RolloutPipeline._save_result`; the two work
together (asyncio.Lock for coroutine safety inside a run, fcntl for
two-process safety across runs).
"""

from __future__ import annotations

import os
import sys
from typing import Iterable, Iterator, Optional


# Windows has no fcntl. We degrade to a no-op lock with a clear warning;
# users on Windows still get atomic appends, just not cross-process
# rejection. portalocker is an optional drop-in for production windows
# deployments.
try:
    import fcntl  # type: ignore[attr-defined]
    _HAS_FCNTL = True
except ImportError:  # pragma: no cover - Windows path
    fcntl = None  # type: ignore[assignment]
    _HAS_FCNTL = False


class ResultStoreLockError(RuntimeError):
    """Raised when another process holds the result-file lock."""


class ResultStore:
    """Lock-protected, atomic JSONL writer + reader.

    Usage::

        store = ResultStore(pipeline.results_file)
        store.acquire_lock()
        try:
            store.append_line(json.dumps(payload) + "\n")
            for line in store.iter_lines():
                ...
        finally:
            store.release_lock()
    """

    def __init__(self, path: str):
        self.path = path
        self._lock_path = path + ".lock"
        self._lock_fd: Optional[int] = None

    # ------------------------------------------------------------------
    # Lock management
    # ------------------------------------------------------------------

    @property
    def lock_path(self) -> str:
        return self._lock_path

    @property
    def locked(self) -> bool:
        return self._lock_fd is not None

    def acquire_lock(self) -> None:
        """Take an exclusive non-blocking fcntl lock on the sidecar.

        Raises :class:`ResultStoreLockError` if another process holds
        the lock so the caller can present a clear "refuse to write"
        message instead of silently interleaving.
        """
        if self._lock_fd is not None:
            return  # already held by this instance

        os.makedirs(os.path.dirname(self._lock_path) or ".", exist_ok=True)
        fd = os.open(self._lock_path, os.O_WRONLY | os.O_CREAT, 0o644)
        if not _HAS_FCNTL:
            # Windows fallback: keep the file descriptor so close() can
            # release the (no-op) "lock" symmetrically, but warn loudly
            # so users know cross-process safety isn't enforced.
            sys.stderr.write(
                "WARNING: fcntl unavailable on this platform; "
                f"ResultStore is operating without cross-process safety on {self.path}\n"
            )
            self._lock_fd = fd
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            os.close(fd)
            raise ResultStoreLockError(
                f"another process holds {self._lock_path}; refuse to write to {self.path}"
            ) from e
        except OSError as e:
            os.close(fd)
            raise ResultStoreLockError(
                f"failed to flock {self._lock_path}: {e}"
            ) from e
        self._lock_fd = fd

    def release_lock(self) -> None:
        if self._lock_fd is None:
            return
        try:
            if _HAS_FCNTL:
                try:
                    fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                except OSError:
                    # Best-effort: ignore unlock errors; the fd close
                    # below releases the lock anyway.
                    pass
        finally:
            try:
                os.close(self._lock_fd)
            except OSError:
                pass
            self._lock_fd = None

    def __enter__(self) -> "ResultStore":
        self.acquire_lock()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.release_lock()

    # ------------------------------------------------------------------
    # IO
    # ------------------------------------------------------------------

    def append_line(self, line: str) -> None:
        """Append a single JSON line with `flush + fsync`."""
        if not line.endswith("\n"):
            line = line + "\n"
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())

    def append_lines(self, lines: Iterable[str]) -> None:
        """Bulk-append (batched flush+fsync) for resume-time bulk writes."""
        any_written = False
        with open(self.path, "a", encoding="utf-8") as f:
            for line in lines:
                if not line.endswith("\n"):
                    line = line + "\n"
                f.write(line)
                any_written = True
            if any_written:
                f.flush()
                os.fsync(f.fileno())

    def iter_lines(self) -> Iterator[str]:
        """Yield raw lines (stripped of trailing newline)."""
        if not os.path.exists(self.path):
            return
        with open(self.path, "r", encoding="utf-8") as f:
            for raw in f:
                yield raw.rstrip("\n")


__all__ = ["ResultStore", "ResultStoreLockError"]

"""
Per-task trajectory checkpoint store (Phase 3 / commit 3.3, optional).

Saves the latest mid-task `Trajectory` snapshot to
``<checkpoint_dir>/<task_id>.json`` after each turn so a kill -9 /
SIGKILL halfway through a long task leaves a complete, auditable
trajectory on disk instead of just an aborted log line.

**Scope (intentionally minimal):**

- Writes are atomic: ``write_atomic`` serialises to a temp file then
  ``os.replace``-s it onto the target. A hard kill mid-write leaves
  either the previous checkpoint or the new one - never a torn JSON.
- When a task completes (success or failure that gets saved into the
  results jsonl), the pipeline calls :meth:`clear_completed` to drop
  the checkpoint, keeping the directory small.
- The "replay from mid-task" runner integration is deliberately out
  of scope; the plan's §6.4 detail is still unwritten and the
  Phase 3.2 `resume + resume_retry_failed=True` already re-runs
  unfinished tasks from scratch. The value here is **operator-side
  artifact recovery** ("what did the model do before it died?").

Disabled by default. Opt in via ``RolloutConfig.checkpoint_enabled``.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from typing import Any, Dict, Iterator, Optional


logger = logging.getLogger("rollout.checkpoint")


class CheckpointStore:
    """Per-task trajectory snapshots on local disk.

    Usage::

        store = CheckpointStore("checkpoints/run_abc")
        store.write_atomic("task_001", trajectory.to_dict())
        ...
        store.clear_completed("task_001")
    """

    def __init__(self, checkpoint_dir: str):
        self.checkpoint_dir = checkpoint_dir
        os.makedirs(self.checkpoint_dir, exist_ok=True)

    def _path_for(self, task_id: str) -> str:
        # Keep filenames safe across filesystems.
        safe = "".join(c if (c.isalnum() or c in ("-", "_", ".")) else "_"
                       for c in str(task_id))[:200]
        if not safe:
            safe = "task"
        return os.path.join(self.checkpoint_dir, f"{safe}.json")

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def write_atomic(self, task_id: str, payload: Dict[str, Any]) -> None:
        """Write the latest snapshot atomically.

        Failure paths are logged at WARNING and *not* re-raised: a
        checkpoint failure must never kill the run (checkpoints are
        an aid, not a hard dependency).
        """
        target = self._path_for(task_id)
        try:
            # NamedTemporaryFile in the same directory so os.replace
            # is atomic (same filesystem).
            tmp_dir = self.checkpoint_dir
            with tempfile.NamedTemporaryFile(
                "w",
                dir=tmp_dir,
                prefix=".chkpt-",
                suffix=".tmp",
                delete=False,
                encoding="utf-8",
            ) as tmp:
                json.dump(payload, tmp, ensure_ascii=False)
                tmp.flush()
                os.fsync(tmp.fileno())
                tmp_path = tmp.name
            os.replace(tmp_path, target)
        except OSError as e:
            logger.warning(
                "checkpoint write failed for task=%s: %r", task_id, e,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "checkpoint write crashed for task=%s: %r", task_id, e,
            )

    # ------------------------------------------------------------------
    # Reads / lifecycle
    # ------------------------------------------------------------------

    def load(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Load a single checkpoint, or None if missing / unreadable."""
        path = self._path_for(task_id)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(
                "checkpoint read failed for task=%s (path=%s): %r",
                task_id, path, e,
            )
            return None

    def load_index(self) -> Dict[str, str]:
        """Return ``{task_id: absolute_path}`` for every checkpoint on disk.

        Use this on resume to discover which tasks have mid-flight
        artifacts ready for inspection.
        """
        out: Dict[str, str] = {}
        if not os.path.isdir(self.checkpoint_dir):
            return out
        for name in os.listdir(self.checkpoint_dir):
            if not name.endswith(".json") or name.startswith("."):
                continue
            task_id = name[:-len(".json")]
            out[task_id] = os.path.join(self.checkpoint_dir, name)
        return out

    def iter_checkpoints(self) -> Iterator[Dict[str, Any]]:
        """Yield each loadable checkpoint payload (broken files skipped)."""
        for task_id, _ in self.load_index().items():
            payload = self.load(task_id)
            if payload is not None:
                yield payload

    def clear_completed(self, task_id: str) -> bool:
        """Drop the checkpoint for a finished task.

        Returns True if a file was removed. Soft-fail: missing files
        are not an error (already-cleared task).
        """
        path = self._path_for(task_id)
        if not os.path.exists(path):
            return False
        try:
            os.remove(path)
            return True
        except OSError as e:
            logger.warning(
                "checkpoint clear failed for task=%s (path=%s): %r",
                task_id, path, e,
            )
            return False


__all__ = ["CheckpointStore"]

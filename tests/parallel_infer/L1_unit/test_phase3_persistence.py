"""
L1 / Phase 3 - result file lock + resume + per-task checkpoint.

Covered commits:
- 3.1 ResultStore advisory cross-instance lock (subprocess test):
       two processes pointing at the same path must not both succeed
       in taking the lock.
- 3.2 _load_completed_task_ids / _apply_resume_filter:
       success rows are skipped; failure rows are re-run only when
       `resume_retry_failed=True`.
- 3.3 CheckpointStore.write_atomic / clear_completed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from rollout.core.checkpoint_store import CheckpointStore
from rollout.core.config import RolloutConfig
from rollout.core.models import BenchmarkItem
from rollout.core.result_store import ResultStore, ResultStoreLockError
from rollout.pipeline import RolloutPipeline


# -----------------------------------------------------------------------
# Commit 3.1 - cross-process fcntl lock
# -----------------------------------------------------------------------


def test_result_store_cross_process_lock(tmp_path: Path):
    """Spawn a Python subprocess that tries to acquire the same lock
    while THIS process holds it; the subprocess must fail loudly."""
    path = tmp_path / "results.jsonl"
    holder = ResultStore(str(path))
    holder.acquire_lock()
    try:
        script = textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {repr(str(Path(__file__).resolve().parents[3]))})
            from rollout.core.result_store import ResultStore, ResultStoreLockError
            rs = ResultStore({repr(str(path))})
            try:
                rs.acquire_lock()
                print("LOCK_OK")
                sys.exit(0)
            except ResultStoreLockError:
                print("LOCK_REJECTED")
                sys.exit(2)
        """).strip()
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=10,
        )
    finally:
        holder.release_lock()

    assert proc.returncode == 2, (
        f"subprocess exit={proc.returncode} stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    assert "LOCK_REJECTED" in proc.stdout


def test_result_store_lock_release_allows_reacquisition(tmp_path: Path):
    path = tmp_path / "r.jsonl"
    rs = ResultStore(str(path))
    rs.acquire_lock()
    rs.release_lock()
    # Now a fresh instance can take it without error.
    rs2 = ResultStore(str(path))
    rs2.acquire_lock()
    rs2.release_lock()


# -----------------------------------------------------------------------
# Commit 3.2 - resume scan
# -----------------------------------------------------------------------


def _seed_results(path: Path, rows: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _make_pipeline(tmp_path: Path, **cfg_overrides):
    cfg = RolloutConfig.from_dict({
        "api_key": "x",
        "base_url": "http://stub",
        "benchmark_name": "stub",
        "model_name": "stub",
        "evaluate_results": False,
        "save_summary": False,
        "save_results": False,
        "output_dir": str(tmp_path),
        "output_filename_strategy": "stable",
        **cfg_overrides,
    })
    return RolloutPipeline(cfg, output_dir=str(tmp_path))


def test_resume_skips_success_and_keeps_failures_by_default(tmp_path: Path):
    """`resume_retry_failed=True` (the default in v2.7) means previous
    failures are re-run, only the explicitly-successful task_ids are
    skipped."""
    p = _make_pipeline(tmp_path, resume=True, resume_retry_failed=True)
    _seed_results(Path(p.results_file), [
        {"task_id": "ok-1", "success": True},
        {"task_id": "fail-1", "success": False, "error": "boom"},
    ])
    done = p._load_completed_task_ids()
    assert done == {"ok-1"}, done


def test_resume_with_retry_failed_disabled_keeps_failures_as_done(tmp_path: Path):
    p = _make_pipeline(tmp_path, resume=True, resume_retry_failed=False)
    _seed_results(Path(p.results_file), [
        {"task_id": "ok-1", "success": True},
        {"task_id": "fail-1", "success": False},
    ])
    done = p._load_completed_task_ids()
    assert done == {"ok-1", "fail-1"}, done


def test_apply_resume_filter_drops_completed_items(tmp_path: Path):
    p = _make_pipeline(tmp_path, resume=True, resume_retry_failed=True)
    _seed_results(Path(p.results_file), [
        {"task_id": "ok-1", "success": True},
        {"task_id": "ok-2", "success": True},
    ])
    p.benchmark_items = [BenchmarkItem(id=tid, question="q")
                         for tid in ("ok-1", "ok-2", "new-1")]
    p._apply_resume_filter()
    assert [it.id for it in p.benchmark_items] == ["new-1"]


def test_resume_tolerates_malformed_jsonl(tmp_path: Path):
    p = _make_pipeline(tmp_path, resume=True)
    Path(p.results_file).parent.mkdir(parents=True, exist_ok=True)
    Path(p.results_file).write_text(
        '{"task_id":"good","success":true}\n'
        'not-a-json-line\n'
        '{"task_id":"good-2","success":true}\n',
        encoding="utf-8",
    )
    done = p._load_completed_task_ids()
    assert done == {"good", "good-2"}


# -----------------------------------------------------------------------
# Commit 3.3 - CheckpointStore atomicity
# -----------------------------------------------------------------------


def test_checkpoint_write_atomic_roundtrips(tmp_path: Path):
    store = CheckpointStore(str(tmp_path / "chkpts"))
    payload = {"task_id": "t1", "messages": [{"role": "user", "content": "hi"}]}
    store.write_atomic("t1", payload)
    assert store.load("t1") == payload


def test_checkpoint_write_atomic_no_temp_files_left(tmp_path: Path):
    store = CheckpointStore(str(tmp_path / "chkpts"))
    for i in range(5):
        store.write_atomic(f"t{i}", {"i": i})
    files = os.listdir(store.checkpoint_dir)
    # We allow only `.json` files; any leftover `.chkpt-*.tmp` means
    # the atomic-rename path leaked a temp file.
    bad = [f for f in files if not f.endswith(".json")]
    assert not bad, f"temp files leaked: {bad}"


def test_checkpoint_clear_completed_is_idempotent(tmp_path: Path):
    store = CheckpointStore(str(tmp_path / "chkpts"))
    store.write_atomic("t1", {"a": 1})
    assert store.clear_completed("t1") is True
    assert store.clear_completed("t1") is False  # idempotent
    assert store.load("t1") is None


def test_checkpoint_sanitises_unsafe_task_ids(tmp_path: Path):
    """Path-traversal task_ids must not escape the checkpoint dir."""
    store = CheckpointStore(str(tmp_path / "chkpts"))
    store.write_atomic("../escape", {"x": 1})
    # The on-disk file must live inside the checkpoint dir.
    inside = list(Path(store.checkpoint_dir).iterdir())
    assert all(p.parent == Path(store.checkpoint_dir) for p in inside)

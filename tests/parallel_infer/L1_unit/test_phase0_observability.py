"""
L1 / Phase 0 - observability & cancel-safe primitives.

Each test pins the invariant of exactly one commit in
`feat/parallel-infer-v2`. No external dependencies beyond pytest.
Async tests use ``asyncio.run`` directly to avoid requiring
pytest-asyncio.

Covered commits:
- 0.1 (logging_utils.py): context follows await; per-worker file filter
- 0.2 (shutdown.py): first signal sets event; force_exit_after rules
- 0.3 (result_store.ResultStore): atomic append + cross-instance lock
- 0.4 (runner._make_trace_id): format + uniqueness
- 0.5 (RolloutConfig + async_chat_completion + _resolve_tool_timeout):
       three-tier timeouts
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import signal as _signal
import time
import uuid
from pathlib import Path
from typing import List

import pytest

from rollout.core.logging_utils import (
    attach_worker_file_handler,
    clear_context,
    detach_handler,
    get_context,
    install_root_handler,
    set_context,
)
from rollout.core.result_store import ResultStore, ResultStoreLockError
from rollout.core.runner import _make_trace_id
from rollout.core.shutdown import ShutdownManager


# Helper so we can write "@async_test" instead of repeating asyncio.run.
# functools.wraps preserves __wrapped__ which pytest's fixture
# resolution follows when inspecting parameter names like `tmp_path`.
def async_test(coro_fn):
    @functools.wraps(coro_fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(coro_fn(*args, **kwargs))
    return wrapper


# -----------------------------------------------------------------------
# Commit 0.1 - structured logging context
# -----------------------------------------------------------------------


@async_test
async def test_context_vars_isolated_across_tasks():
    """Two parallel asyncio.Tasks must see independent context values."""

    async def worker(worker_id: str, seen: List[str], barrier: asyncio.Event):
        tokens = set_context(worker_id=worker_id, task_id=f"t-{worker_id}")
        try:
            await asyncio.sleep(0.01)
            seen.append(get_context()["worker_id"])
            barrier.set()
            await asyncio.sleep(0.01)
            seen.append(get_context()["worker_id"])
        finally:
            clear_context(tokens)

    seen: List[str] = []
    barrier = asyncio.Event()
    await asyncio.gather(
        asyncio.create_task(worker("A", seen, barrier)),
        asyncio.create_task(worker("B", seen, barrier)),
    )
    assert seen.count("A") == 2, seen
    assert seen.count("B") == 2, seen


@async_test
async def test_context_resets_to_dash_default_after_clear():
    tokens = set_context(worker_id="x")
    assert get_context()["worker_id"] == "x"
    clear_context(tokens)
    assert get_context()["worker_id"] == "-"


def test_set_context_ignores_none_values():
    tokens = set_context(worker_id="w1", task_id=None)
    try:
        assert get_context()["worker_id"] == "w1"
        assert get_context()["task_id"] == "-"
    finally:
        clear_context(tokens)


def test_install_root_handler_idempotent():
    install_root_handler("INFO")
    initial = list(logging.getLogger().handlers)
    install_root_handler("DEBUG")
    assert len(logging.getLogger().handlers) == len(initial)


def test_attach_worker_file_handler_filters_by_worker_id(tmp_path: Path):
    """Per-worker file handler must only accept records whose
    contextual ``worker_id`` matches the handler's worker."""
    install_root_handler("INFO")
    h_a = attach_worker_file_handler("A", str(tmp_path), level="INFO")
    h_b = attach_worker_file_handler("B", str(tmp_path), level="INFO")
    logger = logging.getLogger(f"per_worker_log_{uuid.uuid4().hex[:6]}")
    logger.setLevel(logging.INFO)
    try:
        tokens = set_context(worker_id="A")
        logger.warning("hello-from-A")
        clear_context(tokens)
        tokens = set_context(worker_id="B")
        logger.warning("hello-from-B")
        clear_context(tokens)
    finally:
        detach_handler(h_a)
        detach_handler(h_b)

    log_a = (tmp_path / "rollout.worker.A.log").read_text(encoding="utf-8")
    log_b = (tmp_path / "rollout.worker.B.log").read_text(encoding="utf-8")
    assert "hello-from-A" in log_a, log_a
    assert "hello-from-B" not in log_a, log_a
    assert "hello-from-B" in log_b, log_b
    assert "hello-from-A" not in log_b, log_b


# -----------------------------------------------------------------------
# Commit 0.2 - ShutdownManager
# -----------------------------------------------------------------------


@async_test
async def test_shutdown_first_signal_sets_event():
    sm = ShutdownManager(force_exit_after=3)
    sm.install(asyncio.get_running_loop())
    assert not sm.triggered
    sm._on_signal(_signal.SIGINT)
    # When invoked from inside the running loop, ShutdownManager
    # schedules `event.set` via `call_soon_threadsafe`, so we need
    # one tick (or `event.wait`) before the flag flips.
    await asyncio.wait_for(sm.event.wait(), timeout=0.5)
    assert sm.triggered


@async_test
async def test_shutdown_repeated_signals_only_warn_until_force():
    sm = ShutdownManager(force_exit_after=3)
    sm.install(asyncio.get_running_loop())
    sm._on_signal(_signal.SIGINT)
    sm._on_signal(_signal.SIGINT)
    # We do NOT exercise count >= force_after here; that path calls
    # os._exit(130) which would kill pytest. The shutdown source
    # itself has only one branch that does that, gated by this check.
    assert sm._count == 2


def test_shutdown_rejects_too_small_force_after():
    with pytest.raises(ValueError):
        ShutdownManager(force_exit_after=1)


# -----------------------------------------------------------------------
# Commit 0.3 - cancel-safe atomic append via ResultStore
# -----------------------------------------------------------------------


def test_result_store_append_atomic(tmp_path: Path):
    """64 appenders inside one process produce 64 valid JSON lines."""
    path = tmp_path / "results.jsonl"
    rs = ResultStore(str(path))
    rs.acquire_lock()
    try:
        for i in range(64):
            rs.append_line(json.dumps({"i": i}))
    finally:
        rs.release_lock()
    lines = [l for l in path.read_text().splitlines() if l]
    assert len(lines) == 64
    assert {json.loads(l)["i"] for l in lines} == set(range(64))


def test_result_store_lock_blocks_other_instance(tmp_path: Path):
    """A second ResultStore on the same path must fail loudly so two
    runs cannot silently interleave bytes."""
    path = tmp_path / "results.jsonl"
    a = ResultStore(str(path))
    b = ResultStore(str(path))
    a.acquire_lock()
    try:
        with pytest.raises(ResultStoreLockError):
            b.acquire_lock()
    finally:
        a.release_lock()


# -----------------------------------------------------------------------
# Commit 0.4 - trace_id format and uniqueness
# -----------------------------------------------------------------------


def test_make_trace_id_format_and_components():
    t = _make_trace_id("run123", "w0", "task_5", 7, "abc")
    parts = t.split(":")
    assert parts == ["run123", "w0", "task_5", "t7", "abc"]


def test_make_trace_id_unique_when_suffix_missing():
    """Empty suffix => auto uuid; 100 calls all unique."""
    seen = {_make_trace_id("r", "w", "t", 0, "") for _ in range(100)}
    assert len(seen) == 100


# -----------------------------------------------------------------------
# Commit 0.5 - three-tier timeouts
# -----------------------------------------------------------------------


def test_resolve_tool_timeout_honours_overrides():
    """Per-tool override beats default."""
    from rollout.core.config import RolloutConfig
    from rollout.core.runner import AgentRunner

    cfg = RolloutConfig.from_dict(
        {
            "api_key": "x",
            "base_url": "http://stub",
            "tool_default_timeout": 7.5,
            "tool_timeout_overrides": {"browser_open": 42.0},
        }
    )
    runner = AgentRunner(cfg, worker_id="w-test", run_id="r-test")
    try:
        assert runner._resolve_tool_timeout("browser_open") == 42.0
        assert runner._resolve_tool_timeout("rag_search") == 7.5
    finally:
        try:
            asyncio.run(runner.client.close())
        except Exception:
            pass


@async_test
async def test_async_chat_completion_respects_llm_timeout():
    from rollout.core.utils import async_chat_completion

    class _StubCompletions:
        async def create(self, **_):
            await asyncio.sleep(5.0)

    class _StubChat:
        completions = _StubCompletions()

    class _StubClient:
        chat = _StubChat()

    t0 = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        await async_chat_completion(
            _StubClient(),
            max_retries=0,
            llm_timeout=0.1,
            model="dummy",
            messages=[{"role": "user", "content": "hi"}],
        )
    assert time.monotonic() - t0 < 1.5, "wait_for must abort well before 5s"


@async_test
async def test_async_chat_completion_retries_then_raises():
    from rollout.core.utils import async_chat_completion

    attempts: List[int] = []

    class _StubCompletions:
        async def create(self, **_):
            attempts.append(1)
            raise RuntimeError("nope")

    class _StubChat:
        completions = _StubCompletions()

    class _StubClient:
        chat = _StubChat()

    with pytest.raises(RuntimeError):
        await async_chat_completion(
            _StubClient(),
            max_retries=2,
            retry_wait=0.01,
            retry_backoff=1.0,
            model="dummy",
            messages=[{"role": "user", "content": "hi"}],
        )
    assert len(attempts) == 3

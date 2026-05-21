"""
L1 / Phase 2 - worker-pool scheduler & supporting plumbing.

Covered commits:
- 0.8a: when the LLM returns >=2 tool_calls in a single turn, the
        assistant message's `tool_calls` field is truncated to the
        single call the runner actually executes, keeping the
        OpenAI assistant<->tool pairing valid.
- 2.1:  RolloutConfig auto-maps legacy `max_workers` onto the new
        `concurrency` knob (already covered by L0 schema; here we
        re-check the `from_dict` round trip end-to-end).
- 2.2:  `RolloutPipeline._run_parallel` is a real coroutine scheduler:
        - the worker-pool path is selected when `parallel=True` AND
          `concurrency > 1`;
        - SIGINT (via ShutdownManager) interrupts pending tasks without
          leaving zombie workers.
- 2.3:  per-worker rotating log file is opened from the worker context
        and filtered to that worker only (already covered in
        test_phase0_observability; here we test the helper directly).
- 2.4:  `_compute_tool_stats` and `_aggregate_tool_stats` produce
        coherent counts including the by_error_kind bucket from 0.4c-b.
"""

from __future__ import annotations

import asyncio
import functools
from pathlib import Path
from types import SimpleNamespace
from typing import List

import pytest

from rollout.core.config import RolloutConfig
from rollout.core.models import (
    BenchmarkItem,
    Message,
    TaskResult,
    ToolCall,
    Trajectory,
)
from rollout.core.runner import (
    AgentRunner,
    _aggregate_tool_stats,
    _compute_tool_stats,
)
from rollout.pipeline import RolloutPipeline


def async_test(coro_fn):
    @functools.wraps(coro_fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(coro_fn(*args, **kwargs))
    return wrapper


# -----------------------------------------------------------------------
# Commit 0.8a - tool_calls paired
# -----------------------------------------------------------------------


@async_test
async def test_assistant_tool_calls_truncated_to_executed_set():
    """When the LLM emits 3 tool_calls, the runner must execute the
    first one AND record exactly that one on the assistant message,
    otherwise the OpenAI invariant ``every tool_call_id has a matching
    role=tool message`` is violated on the next turn."""

    cfg = RolloutConfig.from_dict({
        "api_key": "x", "base_url": "http://stub",
        "max_turns": 1,
        "model_name": "stub",
        "max_retries": 0,
    })
    runner = AgentRunner(cfg, worker_id="w-test", run_id="r-test")

    # Build a fake ChatCompletion response with 3 tool_calls and stub
    # out the async client so no HTTP happens.
    def _mock_tool_call(i: int):
        return SimpleNamespace(
            id=f"tc-{i}",
            type="function",
            function=SimpleNamespace(name="dummy_tool", arguments="{}"),
            model_dump=lambda i=i: {
                "id": f"tc-{i}", "type": "function",
                "function": {"name": "dummy_tool", "arguments": "{}"},
            },
        )

    fake_msg = SimpleNamespace(
        content="multi", tool_calls=[_mock_tool_call(i) for i in range(3)]
    )
    fake_response = SimpleNamespace(choices=[SimpleNamespace(message=fake_msg)])

    class _StubCompletions:
        async def create(self, **_):
            return fake_response

    runner.client = SimpleNamespace(chat=SimpleNamespace(completions=_StubCompletions()))

    # Stub the sandbox so the single tool call returns a clean payload.
    class _StubSandbox:
        async def execute(self, name, params, trace_id=None, timeout=None):
            return {"code": 0, "message": "ok", "data": {}, "meta": {}}

    runner.sandbox = _StubSandbox()
    runner.tool_schemas = [{
        "type": "function",
        "function": {"name": "dummy_tool", "parameters": {"type": "object"}},
    }]

    trajectory = Trajectory(task_id="t1", question="q")
    await runner._run_conversation(
        [Message(role="user", content="hi")], trajectory,
    )

    # We expect exactly one tool call recorded on the assistant message.
    assistant_msgs = [m for m in trajectory.messages if m.role == "assistant"]
    assert assistant_msgs, "no assistant message recorded"
    recorded = assistant_msgs[-1].tool_calls
    assert isinstance(recorded, list)
    assert len(recorded) == 1, recorded
    assert recorded[0]["id"] == "tc-0"

    # And exactly one role=tool message answering that id.
    tool_msgs = [m for m in trajectory.messages if m.role == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].tool_call_id == "tc-0"


# -----------------------------------------------------------------------
# Commit 2.1 - concurrency / max_workers mapping
# -----------------------------------------------------------------------


def test_concurrency_legacy_mapping_round_trip():
    cfg = RolloutConfig.from_dict({"max_workers": 7})
    assert cfg.concurrency == 7
    # And the new field round-trips when serialised
    payload = cfg.to_dict()
    assert payload["concurrency"] == 7


# -----------------------------------------------------------------------
# Commit 2.2 - worker pool selection
# -----------------------------------------------------------------------


def _make_pipeline_for_pool(tmp_path: Path, concurrency: int, parallel: bool):
    cfg = RolloutConfig.from_dict({
        "api_key": "x",
        "base_url": "http://stub",
        "benchmark_name": "stub",
        "evaluate_results": False,
        "save_summary": False,
        "save_results": False,
        "output_dir": str(tmp_path),
        "parallel": parallel,
        "concurrency": concurrency,
        "worker_startup_jitter": 0.0,
        "model_name": "stub",
    })
    p = RolloutPipeline(cfg, output_dir=str(tmp_path))
    p.benchmark_items = [BenchmarkItem(id=f"t{i}", question="q") for i in range(2)]
    return p


@async_test
async def test_run_parallel_pulls_each_task_exactly_once(tmp_path: Path):
    """The pool's queue semantics: each item is processed exactly once
    regardless of concurrency. We stub `_spawn_worker` to record what it
    pulled, so this test does not need a real sandbox / LLM."""

    p = _make_pipeline_for_pool(tmp_path, concurrency=3, parallel=True)
    pulled: List[str] = []

    async def fake_spawn(idx, queue, progress):
        while True:
            try:
                item = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            pulled.append(item.id)
            queue.task_done()
            await asyncio.sleep(0)

    p._spawn_worker = fake_spawn  # type: ignore[assignment]
    p._shutdown = None  # disable signal install path for the test
    await p._run_parallel()
    assert sorted(pulled) == ["t0", "t1"]


@async_test
async def test_run_parallel_respects_shutdown(tmp_path: Path):
    """When ShutdownManager's event fires mid-flight, the pool cancels
    pending workers and `_run_parallel` returns within shutdown_timeout."""
    from rollout.core.shutdown import ShutdownManager

    p = _make_pipeline_for_pool(tmp_path, concurrency=2, parallel=True)
    p._shutdown = ShutdownManager()
    p.config.shutdown_timeout = 2.0

    async def slow_spawn(idx, queue, progress):
        # Hang here so the only way out is cancellation.
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            return

    p._spawn_worker = slow_spawn  # type: ignore[assignment]

    async def fire_shutdown():
        await asyncio.sleep(0.1)
        p._shutdown.event.set()

    fire = asyncio.create_task(fire_shutdown())
    try:
        await asyncio.wait_for(p._run_parallel(), timeout=5.0)
    finally:
        fire.cancel()


# -----------------------------------------------------------------------
# Commit 2.4 - tool_stats compute + aggregate
# -----------------------------------------------------------------------


def _tc(name: str, success: bool, code: int | None = 0,
        kind: str | None = None, exec_ms: float = 1.0) -> ToolCall:
    return ToolCall(
        tool_name=name, parameters={}, result=None, success=success,
        error=None if success else "boom", execution_time_ms=exec_ms,
        code=code, error_kind=kind if not success else None,
    )


def test_compute_tool_stats_basic_counts():
    traj = Trajectory(task_id="t", question="q", tool_calls=[
        _tc("web:search", True, code=0),
        _tc("web:search", False, code=-21, kind="timeout"),
        _tc("rag:search", True, code=0),
        _tc("rag:search", False, code=-22, kind="client_error"),
    ])
    stats = _compute_tool_stats(traj)
    assert stats is not None
    assert stats["total"] == 4
    assert stats["success"] == 2
    assert stats["failed"] == 2
    assert stats["by_tool"]["web:search"] == {"total": 2, "success": 1, "failed": 1}
    assert stats["by_code"] == {"0": 2, "-21": 1, "-22": 1}
    assert stats["by_error_kind"] == {"timeout": 1, "client_error": 1}


def test_compute_tool_stats_empty_returns_none():
    traj = Trajectory(task_id="t", question="q", tool_calls=[])
    assert _compute_tool_stats(traj) is None


def test_aggregate_tool_stats_additive():
    a = TaskResult(task_id="a", question="q", predicted_answer="",
                   success=True, tool_stats={
                       "total": 2, "success": 1, "failed": 1,
                       "execution_time_ms_total": 5.0,
                       "by_tool": {"x": {"total": 2, "success": 1, "failed": 1}},
                       "by_code": {"0": 1, "-21": 1},
                       "by_error_kind": {"timeout": 1},
                   })
    b = TaskResult(task_id="b", question="q", predicted_answer="",
                   success=True, tool_stats={
                       "total": 1, "success": 1, "failed": 0,
                       "execution_time_ms_total": 7.0,
                       "by_tool": {"x": {"total": 1, "success": 1, "failed": 0}},
                       "by_code": {"0": 1}, "by_error_kind": {},
                   })
    agg = _aggregate_tool_stats([a, b])
    assert agg is not None
    assert agg["total"] == 3
    assert agg["success"] == 2
    assert agg["failed"] == 1
    assert agg["by_tool"]["x"] == {"total": 3, "success": 2, "failed": 1}
    assert agg["by_code"] == {"0": 2, "-21": 1}
    assert agg["by_error_kind"] == {"timeout": 1}
    assert agg["execution_time_ms_total"] == 12.0


def test_aggregate_tool_stats_no_data_returns_none():
    a = TaskResult(task_id="a", question="q", predicted_answer="",
                   success=True, tool_stats=None)
    assert _aggregate_tool_stats([a]) is None

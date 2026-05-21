"""
L2 integration: drive `RolloutPipeline._run_parallel` end-to-end with
a stub LLM client and a stub `Sandbox` so we hit the real worker-pool
scheduler, the real `_save_result` path, the real per-worker context
plumbing, and the real summary aggregator - WITHOUT requiring an
OpenAI key, a sandbox HTTP server, or any network.

This is the highest-fidelity test we can run fully offline.
"""

from __future__ import annotations

import asyncio
import functools
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List

import pytest

from rollout.core.config import RolloutConfig
from rollout.core.models import BenchmarkItem
from rollout.core.runner import AgentRunner
from rollout.pipeline import RolloutPipeline


def async_test(coro_fn):
    @functools.wraps(coro_fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(coro_fn(*args, **kwargs))
    return wrapper


# -----------------------------------------------------------------------
# Stubs
# -----------------------------------------------------------------------


class StubSandbox:
    """Stand-in for the real `Sandbox`. Only the methods touched by
    `AgentRunner.start` / `AgentRunner._execute_tool` need to exist."""

    def __init__(self, *_args, **_kwargs):
        self.calls = 0

    async def start(self): return None
    async def close(self, destroy_sessions: bool = True): return None
    async def destroy_session(self, *_a, **_kw): return {}
    async def create_session(self, *_a, **_kw): return {"status": "success"}
    async def execute(self, name, params=None, *, trace_id=None, timeout=None):
        self.calls += 1
        await asyncio.sleep(0.01)
        return {
            "code": 0, "message": "ok",
            "data": {"result": f"{name}-result"},
            "meta": {"trace_id": trace_id or "auto", "tool": name},
        }
    async def list_tools(self): return []
    async def get_tool_schemas(self): return []


def _build_stub_completion(content: str | None = None,
                           tool_call: dict | None = None):
    if tool_call is None:
        msg = SimpleNamespace(content=content or "", tool_calls=None)
    else:
        tc = SimpleNamespace(
            id=tool_call["id"], type="function",
            function=SimpleNamespace(
                name=tool_call["name"],
                arguments=json.dumps(tool_call.get("args", {})),
            ),
            model_dump=lambda: {
                "id": tool_call["id"], "type": "function",
                "function": {"name": tool_call["name"],
                             "arguments": json.dumps(tool_call.get("args", {}))},
            },
        )
        msg = SimpleNamespace(content=content or "", tool_calls=[tc])
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


class StubLLMClient:
    """Stateless 2-turn LLM:
    - If the conversation has no role='tool' message yet -> emit a tool call.
    - Otherwise -> emit the final answer.
    This keeps the stub safe to reuse across many tasks served by the
    same `AgentRunner` (which is exactly what the worker-pool does).
    """

    def __init__(self):
        self.calls = 0

    @property
    def chat(self):
        outer = self

        class _Chat:
            class completions:
                @staticmethod
                async def create(*, messages, **_kw):
                    outer.calls += 1
                    has_tool_response = any(
                        (m.get("role") if isinstance(m, dict) else None) == "tool"
                        for m in messages or []
                    )
                    if not has_tool_response:
                        return _build_stub_completion(
                            content="thinking",
                            tool_call={"id": "tc1", "name": "dummy_tool",
                                       "args": {"q": "test"}},
                        )
                    return _build_stub_completion(content="**Answer**: 42")

        return _Chat()

    async def close(self):
        return None


# -----------------------------------------------------------------------
# Tests
# -----------------------------------------------------------------------


@async_test
async def test_run_parallel_end_to_end(tmp_path: Path, monkeypatch):
    """End-to-end smoke through the real worker-pool scheduler:
    - 6 tasks, 3 workers
    - per-task file appended to the real results.jsonl
    - summary aggregates tool_stats correctly
    """
    # Patch the sandbox the runner imports so AgentRunner.start uses our stub.
    import rollout.core.runner as runner_mod
    monkeypatch.setattr(runner_mod, "Sandbox", StubSandbox)

    # Patch the LLM client factory to hand out our stub.
    monkeypatch.setattr(
        runner_mod, "create_async_openai_client",
        lambda **_kw: StubLLMClient(),
    )

    # Patch tool schema loading so the LLM call has at least one tool.
    async def _stub_schemas(self):
        self.tool_schemas = [{
            "type": "function",
            "function": {
                "name": "dummy_tool",
                "description": "stub",
                "parameters": {"type": "object", "properties": {}},
            },
        }]
        self._local_tool_schemas = self.tool_schemas
    monkeypatch.setattr(AgentRunner, "_load_tool_schemas", _stub_schemas)

    cfg = RolloutConfig.from_dict({
        "api_key": "x",
        "base_url": "http://stub",
        "benchmark_name": "stub",
        "model_name": "stub",
        "evaluate_results": False,
        "save_results": True,
        "save_summary": False,
        "save_trajectories": True,
        "output_dir": str(tmp_path),
        "output_filename_strategy": "stable",
        "parallel": True,
        "concurrency": 3,
        "worker_startup_jitter": 0.0,
        "max_turns": 3,
        "task_max_seconds": 30.0,
        "llm_timeout": 5.0,
        "tool_default_timeout": 5.0,
        "shutdown_timeout": 5.0,
        "per_worker_log": False,
        "checkpoint_enabled": False,
        "max_retries": 0,
    })
    p = RolloutPipeline(cfg, output_dir=str(tmp_path))
    p.benchmark_items = [
        BenchmarkItem(id=f"t{i}", question=f"Question {i}", answer="42")
        for i in range(6)
    ]

    summary = await p.run_async()

    # Every task must have produced exactly one row.
    assert summary.total_tasks == 6
    assert summary.successful_tasks == 6
    assert summary.failed_tasks == 0

    rows = [json.loads(l) for l in Path(p.results_file).read_text().splitlines() if l]
    assert len(rows) == 6
    assert sorted(r["task_id"] for r in rows) == [f"t{i}" for i in range(6)]
    # Each task drove exactly one tool call, so the aggregate must report 6.
    assert summary.tool_stats is not None
    assert summary.tool_stats["total"] == 6
    assert summary.tool_stats["success"] == 6


@async_test
async def test_resume_skips_completed_in_end_to_end_run(tmp_path: Path, monkeypatch):
    """First run completes 3 tasks; second run with resume must only run
    the new ones."""
    import rollout.core.runner as runner_mod
    monkeypatch.setattr(runner_mod, "Sandbox", StubSandbox)
    monkeypatch.setattr(
        runner_mod, "create_async_openai_client",
        lambda **_kw: StubLLMClient(),
    )

    async def _stub_schemas(self):
        self.tool_schemas = []
    monkeypatch.setattr(AgentRunner, "_load_tool_schemas", _stub_schemas)

    def make_pipeline(resume=False):
        cfg = RolloutConfig.from_dict({
            "api_key": "x", "base_url": "http://stub", "benchmark_name": "stub",
            "model_name": "stub",
            "evaluate_results": False,
            "save_results": True, "save_summary": False,
            "output_dir": str(tmp_path),
            "output_filename_strategy": "stable",
            "parallel": False,  # sequential path for determinism
            "concurrency": 1,
            "max_turns": 1, "max_retries": 0,
            "task_max_seconds": 10.0, "llm_timeout": 5.0,
            "shutdown_timeout": 5.0,
            "resume": resume, "resume_retry_failed": True,
        })
        return RolloutPipeline(cfg, output_dir=str(tmp_path))

    # First run: 3 tasks.
    p1 = make_pipeline()
    p1.benchmark_items = [
        BenchmarkItem(id=f"t{i}", question="q", answer="a") for i in range(3)
    ]
    s1 = await p1.run_async()
    assert s1.total_tasks == 3

    # Second run: resume with 5 tasks; the first 3 are already done.
    p2 = make_pipeline(resume=True)
    p2.benchmark_items = [
        BenchmarkItem(id=f"t{i}", question="q", answer="a") for i in range(5)
    ]
    s2 = await p2.run_async()
    # Pipeline only processes the NEW items.
    assert s2.total_tasks == 2
    # On disk the file has the full 5 unique rows now.
    rows = [json.loads(l) for l in Path(p2.results_file).read_text().splitlines() if l]
    assert sorted({r["task_id"] for r in rows}) == [f"t{i}" for i in range(5)]

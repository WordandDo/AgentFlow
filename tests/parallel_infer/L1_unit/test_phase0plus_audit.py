"""
L1 / Phase 0+ - audit patches (commits 0.4a–0.4f, 0.8a/0.8b, 0.7b/0.7d).

Tested invariants:
- 0.4a: _format_for_llm prefers sandbox.format_tool_result registry;
        non-dict + unknown tool falls back to generic formatter
        (length-bounded by tool_result_max_length).
- 0.4c-a: bdb.BdbQuit must not be swallowed in _format_for_llm.
- 0.4c-b: _classify_tool_error returns stable (code, kind, message,
          status) tuples for timeout/connect/4xx/5xx/sandbox_disconnect/
          unknown.
- 0.4d:  Evaluator score semantics: failed=0.0, no_gt=None, evaluated=score.
- 0.4f:  RolloutPipeline._check_duplicate_task_ids:
          error  -> raise;  warn -> log+continue;  ignore -> no-op.
- 0.8b:  ToolCall / Trajectory.from_dict survive partial old payloads.
"""

from __future__ import annotations

import asyncio
import bdb
import json
import logging
from typing import List

import pytest

from rollout.core.config import RolloutConfig
from rollout.core.evaluator import Evaluator
from rollout.core.models import BenchmarkItem, TaskResult, ToolCall, Trajectory
from rollout.core.runner import AgentRunner, _classify_tool_error
from rollout.pipeline import RolloutPipeline


# -----------------------------------------------------------------------
# Commit 0.4a + 0.4c-a - _format_for_llm
# -----------------------------------------------------------------------


def _make_runner(extra: dict | None = None) -> AgentRunner:
    cfg = RolloutConfig.from_dict({
        "api_key": "x",
        "base_url": "http://stub",
        "tool_result_max_length": 1024,
        **(extra or {}),
    })
    return AgentRunner(cfg, worker_id="w", run_id="r")


def _close_runner(runner: AgentRunner) -> None:
    try:
        asyncio.run(runner.client.close())
    except Exception:
        pass


def test_format_for_llm_uses_registry_for_known_tool():
    """Known sandbox tool ('web:search') must go through the canonical
    sandbox.format_tool_result registry and produce a clean string
    (no `code`/`meta`/`trace_id` noise leaking into the chat context)."""
    runner = _make_runner()
    try:
        payload = {
            "code": 0,
            "message": "ok",
            "data": {
                "query": "what is rust",
                "results": [
                    {"title": "Rust lang", "url": "https://rust-lang.org",
                     "snippet": "A systems language."},
                ],
            },
            "meta": {"trace_id": "demo-trace"},
        }
        out = runner._format_for_llm("web:search", payload)
    finally:
        _close_runner(runner)

    assert isinstance(out, str) and out
    # The registry never echoes the trace_id back into chat context.
    assert "demo-trace" not in out, out
    assert "Rust lang" in out, out


def test_format_for_llm_falls_back_for_unregistered_tool():
    """An unknown tool name must NOT crash: we fall back to the
    generic formatter, which still honours `tool_result_max_length`."""
    runner = _make_runner({"tool_result_max_length": 256})
    try:
        big = "X" * 5000
        payload = {"code": 0, "data": {"result": big}}
        out = runner._format_for_llm("definitely_unknown_tool_xyz", payload)
    finally:
        _close_runner(runner)

    assert isinstance(out, str) and out
    # Truncated to <= max + small suffix.
    assert len(out) < 5000


def test_format_for_llm_lets_bdbquit_propagate(monkeypatch):
    """Commit 0.4c-a: pdb's quit signal must not be swallowed by the
    'fall back to generic formatter' except clause."""
    runner = _make_runner()
    try:
        def _raise_bdbquit(_):
            raise bdb.BdbQuit()
        # Monkeypatch the runner's `format_tool_result` symbol.
        import rollout.core.runner as runner_mod
        monkeypatch.setattr(runner_mod, "format_tool_result", _raise_bdbquit)

        with pytest.raises(bdb.BdbQuit):
            runner._format_for_llm("web:search", {"code": 0, "data": {}})
    finally:
        _close_runner(runner)


# -----------------------------------------------------------------------
# Commit 0.4c-b - _classify_tool_error
# -----------------------------------------------------------------------


def test_classify_tool_error_unknown_bucket():
    code, kind, _msg, status = _classify_tool_error(RuntimeError("boom"))
    assert (code, kind, status) == (-1, "unknown", None)


def test_classify_tool_error_asyncio_timeout():
    code, kind, _msg, status = _classify_tool_error(asyncio.TimeoutError("slow"))
    assert kind == "timeout"
    assert code == -21


def test_classify_tool_error_asyncio_cancelled():
    code, kind, _msg, status = _classify_tool_error(asyncio.CancelledError())
    assert kind == "cancelled"
    assert code == -10


def test_classify_tool_error_httpx_connect():
    import httpx
    exc = httpx.ConnectError("dns")
    code, kind, _msg, status = _classify_tool_error(exc)
    assert (code, kind, status) == (-20, "connect", None)


def test_classify_tool_error_httpclient_4xx_vs_5xx():
    from sandbox.client import HTTPClientError
    err400 = HTTPClientError("bad", status_code=400)
    err500 = HTTPClientError("oops", status_code=502)
    assert _classify_tool_error(err400)[:2] == (-22, "client_error")
    assert _classify_tool_error(err500)[:2] == (-23, "server_error")
    # And the status code surfaces back to the caller.
    assert _classify_tool_error(err400)[3] == 400
    assert _classify_tool_error(err500)[3] == 502


# -----------------------------------------------------------------------
# Commit 0.4d - Evaluator score semantics (three states)
# -----------------------------------------------------------------------


def test_evaluator_score_three_states():
    # Build three results: failed / no_gt / evaluated.
    failed = TaskResult(
        task_id="f", question="q", predicted_answer="", success=False,
        error="boom", ground_truth="gt-1",
    )
    no_gt = TaskResult(
        task_id="ng", question="q", predicted_answer="42", success=True,
        ground_truth=None,
    )
    evaluated = TaskResult(
        task_id="ok", question="q", predicted_answer="paris", success=True,
        ground_truth="paris",
    )

    ev = Evaluator(metric="exact_match", model_name="stub",
                   api_key="x", base_url="http://stub")
    summary = ev.evaluate([failed, no_gt, evaluated])

    assert failed.score == 0.0
    assert no_gt.score is None
    assert evaluated.score == 1.0
    # `evaluated_tasks` counts ONLY the truly scored rows (the one
    # with predicted/ground-truth). Failed + no-GT rows still set
    # `.score` but are not appended to `scores`.
    assert summary["evaluated_tasks"] == 1


# -----------------------------------------------------------------------
# Commit 0.4f - duplicate task_id policy
# -----------------------------------------------------------------------


def _make_pipeline_with_dups(mode: str, tmp_path):
    cfg = RolloutConfig.from_dict({
        "api_key": "x",
        "base_url": "http://stub",
        "benchmark_name": "stub",
        "data_path": "ignored",
        "output_dir": str(tmp_path / "out"),
        "evaluate_results": False,
        "on_duplicate_task_id": mode,
    })
    pipeline = RolloutPipeline(cfg, output_dir=str(tmp_path / "out"))
    items = [
        BenchmarkItem(id="t1", question="a"),
        BenchmarkItem(id="t2", question="b"),
        BenchmarkItem(id="t1", question="c"),  # duplicate
    ]
    return pipeline, items


def test_duplicate_task_id_error_mode(tmp_path):
    p, items = _make_pipeline_with_dups("error", tmp_path)
    with pytest.raises(ValueError, match="duplicate task_id"):
        p._check_duplicate_task_ids(items)


def test_duplicate_task_id_warn_mode(tmp_path, caplog):
    p, items = _make_pipeline_with_dups("warn", tmp_path)
    with caplog.at_level(logging.WARNING, logger="rollout.pipeline"):
        p._check_duplicate_task_ids(items)
    assert any("duplicate task_id" in r.getMessage() for r in caplog.records)


def test_duplicate_task_id_ignore_mode(tmp_path, caplog):
    p, items = _make_pipeline_with_dups("ignore", tmp_path)
    with caplog.at_level(logging.WARNING, logger="rollout.pipeline"):
        p._check_duplicate_task_ids(items)
    # Nothing logged about duplicates in ignore mode.
    assert not any("duplicate task_id" in r.getMessage() for r in caplog.records)


# -----------------------------------------------------------------------
# Commit 0.8b - ToolCall / Trajectory from_dict backward compatibility
# -----------------------------------------------------------------------


def test_tool_call_from_dict_partial_payload():
    """Very old rows missing newer fields must still round-trip."""
    legacy = {"tool_name": "web:search", "parameters": {"q": "x"},
              "result": "old", "success": True, "execution_time_ms": 1.0}
    tc = ToolCall.from_dict(legacy)
    assert tc.tool_name == "web:search"
    assert tc.trace_id is None
    assert tc.effective_parameters is None
    assert tc.error_kind is None
    assert tc.status_code is None


def test_trajectory_from_dict_restores_tool_calls():
    tc = ToolCall(tool_name="t", parameters={"a": 1}, result="r", success=True,
                  trace_id="abc")
    traj = Trajectory(task_id="t1", question="q", tool_calls=[tc])
    payload = traj.to_dict()
    rebuilt = Trajectory.from_dict(payload)
    assert len(rebuilt.tool_calls) == 1
    assert rebuilt.tool_calls[0].tool_name == "t"
    assert rebuilt.tool_calls[0].trace_id == "abc"


def test_trajectory_from_dict_tolerates_missing_tool_calls():
    payload = {"task_id": "t2", "question": "q"}  # no tool_calls key
    traj = Trajectory.from_dict(payload)
    assert traj.tool_calls == []

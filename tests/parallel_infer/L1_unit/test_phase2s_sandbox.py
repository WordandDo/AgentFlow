"""
L1 / Phase 2S - server-side sandbox primitives + client retry/heartbeat.

Covered commits:
- 2S.1 (ResourceRouter): split lock + singleflight.
        * a 1s "slow init" must NOT block sibling create_session for a
          DIFFERENT (worker_id, resource_type);
        * 5 concurrent callers for the SAME (worker_id, resource_type)
          must observe the leader's init exactly once.
- 2S.2 (Backpressure): Bound.acquire_or_429 rejects above queue_max
        with a clean OverloadedError; the JSONResponse sets Retry-After.
- 2S.3 (ToolExecutor): _serial_guard serialises same `(worker, type)`
        for resource types in DEFAULT_SERIAL_RESOURCE_TYPES, but lets
        different workers proceed in parallel.
- 2S.4 (websearch shared pool): _get_or_create_executor returns the
        same ThreadPoolExecutor instance on repeated calls.
- 2S.5 + heartbeat (0.4b): Sandbox.close defaults to destroy_sessions=True;
        ResourceRouter.refresh_session actually extends `expires_at`.
- 0.7b (client): _request raises on 4xx (no retry) and treats
        timeouts/5xx as retryable with exponential backoff.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import time
from datetime import datetime, timedelta
from typing import List

import pytest

from sandbox.client import HTTPClientConfig, HTTPServiceClient, HTTPClientError
from sandbox.sandbox import Sandbox, SandboxConfig
from sandbox.server.core.backpressure import (
    Bound,
    LaneGroup,
    OverloadedError,
    build_default_limiter,
    overloaded_response,
)
from sandbox.server.core.resource_router import ResourceRouter
from sandbox.server.core.tool_executor import (
    DEFAULT_SERIAL_RESOURCE_TYPES,
    ToolExecutor,
)


def async_test(coro_fn):
    @functools.wraps(coro_fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(coro_fn(*args, **kwargs))
    return wrapper


# -----------------------------------------------------------------------
# Commit 2S.1 - ResourceRouter split lock + singleflight
# -----------------------------------------------------------------------


@async_test
async def test_resource_router_slow_init_does_not_block_other_worker():
    """A 1.0s init for ('w1', 'vm') must NOT delay ('w2', 'vm')."""
    router = ResourceRouter(session_ttl=60)

    async def slow_vm(worker_id, _config):
        await asyncio.sleep(0.5)
        return {"booted": True, "worker": worker_id}

    router.register_resource_type("vm", initializer=slow_vm)

    t0 = time.monotonic()
    sessions = await asyncio.gather(
        router.get_or_create_session("w1", "vm"),
        router.get_or_create_session("w2", "vm"),
    )
    elapsed = time.monotonic() - t0

    # Parallel init means total wall-time must be ~0.5s, NOT 1.0s.
    assert elapsed < 0.9, f"parallel init took {elapsed:.2f}s (expected ~0.5)"
    assert sessions[0]["data"]["worker"] == "w1"
    assert sessions[1]["data"]["worker"] == "w2"


@async_test
async def test_resource_router_singleflight_dedups_concurrent_callers():
    """5 simultaneous create_session callers for SAME (w, type) must
    only invoke the initializer ONCE."""
    router = ResourceRouter(session_ttl=60)
    call_count = 0

    async def counting_vm(worker_id, _config):
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.2)
        return {"n": call_count}

    router.register_resource_type("vm", initializer=counting_vm)

    sessions = await asyncio.gather(*[
        router.get_or_create_session("w1", "vm") for _ in range(5)
    ])
    assert call_count == 1, f"initializer ran {call_count} times (expected 1)"
    # All 5 callers must see the SAME session_id.
    ids = {s["session_id"] for s in sessions}
    assert len(ids) == 1, ids


# -----------------------------------------------------------------------
# Commit 2S.2 - Backpressure Bound / LaneGroup / OverloadedError
# -----------------------------------------------------------------------


@async_test
async def test_bound_rejects_when_waiters_exceed_queue_max():
    bound = Bound("lane-x", capacity=1, queue_max=1)

    # Hold the only slot.
    cm = bound.acquire_or_429(retry_after_s=0.5)
    holder = await cm.__aenter__()
    assert holder["lane"] == "lane-x"

    # Make ONE waiter (this fills the queue to queue_max=1).
    async def first_waiter():
        async with bound.acquire_or_429():
            return "got-it"
    waiter_task = asyncio.create_task(first_waiter())
    # Give the scheduler one tick so the waiter actually enters the
    # `_waiters += 1` region.
    await asyncio.sleep(0.05)
    assert bound.waiters == 1

    # Any NEW caller now must be rejected immediately.
    with pytest.raises(OverloadedError) as exc:
        async with bound.acquire_or_429(retry_after_s=2.0):
            pass
    assert exc.value.retry_after_s == 2.0
    assert exc.value.queue_max == 1

    # Release the holder so the waiter can complete.
    await cm.__aexit__(None, None, None)
    assert await waiter_task == "got-it"


def test_overloaded_response_sets_retry_after_header():
    err = OverloadedError(lane="vm", retry_after_s=2.7, waiters=5, queue_max=2)
    resp = overloaded_response(err)
    assert resp.status_code == 429
    assert resp.headers["retry-after"] == "3"  # ceil/round to int seconds


def test_build_default_limiter_has_expected_lanes():
    mgr = build_default_limiter()
    assert mgr.global_inflight.capacity > 0
    assert mgr.health.capacity > 0
    assert mgr.status.capacity > 0
    # Each LaneGroup must have at least the 'default' fallback.
    assert isinstance(mgr.tool, LaneGroup)
    assert mgr.tool.get("vm").capacity == 1
    assert mgr.tool.get("rag").capacity > 1
    # Unknown lane key -> falls back to default.
    assert mgr.tool.get("does-not-exist").name == "tool.default"


# -----------------------------------------------------------------------
# Commit 2S.3 - per-(worker, resource_type) serial lock in ToolExecutor
# -----------------------------------------------------------------------


def _make_tool_executor() -> ToolExecutor:
    return ToolExecutor(
        tools={},
        tool_name_index={},
        tool_resource_types={},
        resource_router=ResourceRouter(session_ttl=60),
    )


def test_default_serial_resource_types_match_plan():
    assert DEFAULT_SERIAL_RESOURCE_TYPES == {"vm", "browser", "bash", "code", "mcp"}


@async_test
async def test_serial_guard_serialises_same_worker_same_type():
    te = _make_tool_executor()
    entry_log: List[str] = []

    async def critical(worker, kind):
        async with te._serial_guard(worker, kind):
            entry_log.append(f"enter:{worker}:{kind}")
            await asyncio.sleep(0.1)
            entry_log.append(f"exit:{worker}:{kind}")

    t0 = time.monotonic()
    await asyncio.gather(critical("w1", "vm"), critical("w1", "vm"))
    elapsed = time.monotonic() - t0
    # Two SERIAL 100ms critical sections => ~0.2s, not 0.1s.
    assert elapsed >= 0.18, f"same worker should serialise: {elapsed:.2f}s"

    # And we never observe enter/enter interleaved within "w1:vm".
    starts = [i for i, e in enumerate(entry_log) if e.startswith("enter:")]
    exits = [i for i, e in enumerate(entry_log) if e.startswith("exit:")]
    assert starts[0] < exits[0] < starts[1] < exits[1]


@async_test
async def test_serial_guard_does_not_serialise_different_workers():
    te = _make_tool_executor()

    async def critical(worker):
        async with te._serial_guard(worker, "vm"):
            await asyncio.sleep(0.1)

    t0 = time.monotonic()
    await asyncio.gather(critical("w1"), critical("w2"))
    elapsed = time.monotonic() - t0
    # Different workers => parallel ~0.1s, NOT 0.2s.
    assert elapsed < 0.18, f"different workers should NOT serialise: {elapsed:.2f}s"


@async_test
async def test_serial_guard_skips_for_non_serial_resource_types():
    te = _make_tool_executor()

    async def critical():
        async with te._serial_guard("w1", "rag"):
            await asyncio.sleep(0.1)

    t0 = time.monotonic()
    await asyncio.gather(critical(), critical())
    elapsed = time.monotonic() - t0
    # rag is NOT in DEFAULT_SERIAL_RESOURCE_TYPES, so calls run in parallel.
    assert elapsed < 0.18, f"rag should not be serialised: {elapsed:.2f}s"


def test_drop_session_lock_releases_state():
    te = _make_tool_executor()
    # Force lock creation
    asyncio.run(te._get_session_lock("w1", "vm"))
    assert te.drop_session_lock("w1", "vm") is True
    assert te.drop_session_lock("w1", "vm") is False  # gone, idempotent


# -----------------------------------------------------------------------
# Commit 2S.4 - websearch shared thread pool
# -----------------------------------------------------------------------


def test_websearch_executor_is_shared_singleton():
    from sandbox.server.backends.tools import websearch as ws

    # Drop any pool created by a previous test so this assertion is clean.
    ws._shutdown_shared_executors(wait=False)
    a = ws._get_or_create_executor("search", 4)
    b = ws._get_or_create_executor("search", 99)  # second call reuses
    assert a is b
    c = ws._get_or_create_executor("visit", 2)
    assert c is not a  # different slot
    ws._shutdown_shared_executors(wait=False)


# -----------------------------------------------------------------------
# Commit 2S.5 - Sandbox.close defaults to destroy_sessions=True
# -----------------------------------------------------------------------


def test_sandbox_close_default_destroys_sessions():
    sig = inspect.signature(Sandbox.close)
    default = sig.parameters["destroy_sessions"].default
    assert default is True, (
        "Sandbox.close must default to destroy_sessions=True so a "
        "Ctrl+C does not leak server-side sessions (Phase 2S / 2S.5)"
    )


def test_sandbox_config_exposes_heartbeat_jitter():
    cfg = SandboxConfig()
    assert hasattr(cfg, "heartbeat_jitter_ratio")
    # Default keeps the ±20% spread documented in the plan.
    assert 0.0 <= cfg.heartbeat_jitter_ratio <= 1.0


# -----------------------------------------------------------------------
# Commit 0.4b - heartbeat actually refreshes session TTL
# -----------------------------------------------------------------------


@async_test
async def test_refresh_session_extends_expires_at():
    router = ResourceRouter(session_ttl=60)
    router.register_resource_type("vm")
    s = await router.get_or_create_session("w1", "vm")
    first = datetime.fromisoformat(s["expires_at"])

    # Force a small delay so a refresh has visible effect.
    await asyncio.sleep(0.05)
    refreshed = await router.refresh_session("w1", "vm")
    assert refreshed is True

    s2 = await router.get_session("w1", "vm")
    second = datetime.fromisoformat(s2["expires_at"])
    assert second > first, f"expires_at not extended: {first} -> {second}"


@async_test
async def test_refresh_session_returns_false_for_unknown():
    router = ResourceRouter(session_ttl=60)
    assert await router.refresh_session("does-not-exist", "vm") is False


# -----------------------------------------------------------------------
# Commit 0.7b - client _request retry policy
# -----------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status: int, body: dict):
        self.status_code = status
        self._body = body
    def json(self):
        return self._body


class _ScriptedHttpx:
    """Minimal httpx.AsyncClient stand-in that hands out scripted responses."""

    def __init__(self, sequence: list[_FakeResponse]):
        self._sequence = list(sequence)
        self.calls = 0

    async def get(self, *_a, **_kw):
        return self._next()

    async def post(self, *_a, **_kw):
        return self._next()

    def _next(self) -> _FakeResponse:
        self.calls += 1
        if not self._sequence:
            raise RuntimeError("ScriptedHttpx ran out of responses")
        return self._sequence.pop(0)


def _build_client(http: _ScriptedHttpx) -> HTTPServiceClient:
    client = HTTPServiceClient(config=HTTPClientConfig(
        base_url="http://stub",
        max_retries=3,
        retry_delay=0.01,
        retry_backoff=1.0,  # constant base
        retry_jitter=0.0,   # deterministic
        auto_heartbeat=False,
    ))
    client._client = http  # type: ignore[assignment]
    return client


@async_test
async def test_client_400_is_terminal_no_retry():
    http = _ScriptedHttpx([
        _FakeResponse(400, {"message": "bad request"}),
    ])
    client = _build_client(http)
    with pytest.raises(HTTPClientError) as e:
        await client._request("POST", "/api/v1/execute", data={"x": 1})
    assert e.value.status_code == 400
    assert http.calls == 1, "4xx must NOT retry"


@async_test
async def test_client_429_is_retryable():
    http = _ScriptedHttpx([
        _FakeResponse(429, {"message": "slow down"}),
        _FakeResponse(200, {"code": 0, "data": {}}),
    ])
    client = _build_client(http)
    out = await client._request("POST", "/api/v1/execute", data={"x": 1})
    assert out["code"] == 0
    assert http.calls == 2


@async_test
async def test_client_500_retries_then_succeeds():
    http = _ScriptedHttpx([
        _FakeResponse(503, {"message": "overloaded"}),
        _FakeResponse(503, {"message": "still"}),
        _FakeResponse(200, {"code": 0, "data": {}}),
    ])
    client = _build_client(http)
    out = await client._request("POST", "/api/v1/execute", data={"x": 1})
    assert out["code"] == 0
    assert http.calls == 3


@async_test
async def test_client_500_eventually_gives_up():
    http = _ScriptedHttpx([
        _FakeResponse(503, {"message": "boom"}),
        _FakeResponse(503, {"message": "boom"}),
        _FakeResponse(503, {"message": "boom"}),
    ])
    client = _build_client(http)
    with pytest.raises(HTTPClientError) as e:
        await client._request("POST", "/api/v1/execute", data={"x": 1})
    assert e.value.status_code == 503
    assert http.calls == 3

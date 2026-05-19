# sandbox/server/core/backpressure.py
"""
Tiered backpressure helpers for the sandbox HTTP server.

The goal is twofold:

1. **Isolation**: ``/health`` and ``/ready`` must never share a queue
   with the (potentially slow) ``session:create`` or ``execute`` lanes,
   so a saturated tool lane cannot make liveness probes flap.
2. **Fast failure under overload**: instead of unbounded queueing, each
   lane is wrapped in a small ``asyncio.Semaphore`` with an optional
   FIFO waiter-count cap; over the cap we surface an
   :class:`OverloadedError` that the route layer maps to a
   ``429 Too Many Requests`` plus a ``Retry-After`` header so clients
   can back off cleanly.

Usage from a route::

    async with server.backpressure.tool_for("vm").acquire_or_429(
        retry_after_s=1.0,
    ) as info:
        return await server.execute(...)

Or, equivalently::

    try:
        async with limiter.acquire_or_429(1.0) as info:
            ...
    except OverloadedError as e:
        return overloaded_response(e)

The lane definitions are configured at server startup; see
:func:`build_default_limiter` for the defaults that ship today.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, Mapping, Optional

from fastapi.responses import JSONResponse


logger = logging.getLogger("Backpressure")


class OverloadedError(Exception):
    """Raised by :meth:`Bound.acquire_or_429` when a lane is over its
    waiter cap. The route layer converts this into a 429 response."""

    def __init__(self, lane: str, retry_after_s: float, waiters: int, queue_max: int):
        super().__init__(
            f"backpressure lane {lane!r} overloaded: "
            f"waiters={waiters} >= queue_max={queue_max}"
        )
        self.lane = lane
        self.retry_after_s = retry_after_s
        self.waiters = waiters
        self.queue_max = queue_max


def overloaded_response(err: OverloadedError) -> JSONResponse:
    """Build the standard 429 response for an :class:`OverloadedError`."""
    retry_after = max(1, int(round(err.retry_after_s)))
    return JSONResponse(
        status_code=429,
        headers={"Retry-After": str(retry_after)},
        content={
            "code": 429,
            "message": f"overloaded:{err.lane}",
            "data": None,
            "meta": {
                "retry_after_s": err.retry_after_s,
                "lane": err.lane,
                "waiters": err.waiters,
                "queue_max": err.queue_max,
            },
        },
    )


@dataclass
class _Stats:
    """In-process counters per lane; useful for observability/asserts."""
    accepted: int = 0
    rejected: int = 0
    wait_ms_total: float = 0.0
    wait_ms_max: float = 0.0


class Bound:
    """A single backpressure lane: capacity + optional waiter cap.

    The lane is intentionally simple - we use an ``asyncio.Semaphore``
    rather than a richer queue because we do not need priority or
    fairness beyond FIFO. The ``queue_max`` knob is the only "real"
    overload signal: if more than ``queue_max`` callers are already
    blocked waiting for the semaphore, the next caller is rejected
    immediately instead of joining an unbounded queue.

    ``queue_max=0`` (the default) disables the rejection path entirely
    so the lane behaves as a plain bounded concurrency limiter, which
    is the safe default for non-critical lanes.
    """

    def __init__(self, name: str, capacity: int, queue_max: int = 0):
        if capacity < 1:
            raise ValueError(f"capacity must be >= 1 (lane={name!r})")
        if queue_max < 0:
            raise ValueError(f"queue_max must be >= 0 (lane={name!r})")
        self.name = name
        self.capacity = capacity
        self.queue_max = queue_max
        self._sem = asyncio.Semaphore(capacity)
        self._waiters = 0
        self.stats = _Stats()

    @property
    def waiters(self) -> int:
        """Approximate number of callers currently blocked on acquire."""
        return self._waiters

    @asynccontextmanager
    async def acquire_or_429(
        self,
        retry_after_s: float = 1.0,
    ) -> AsyncIterator[Dict[str, Any]]:
        """Acquire a slot or raise :class:`OverloadedError`.

        Yields a small ``info`` dict with the actual queue wait time so
        callers can log it.
        """
        if self.queue_max > 0 and self._waiters >= self.queue_max:
            self.stats.rejected += 1
            raise OverloadedError(
                lane=self.name,
                retry_after_s=retry_after_s,
                waiters=self._waiters,
                queue_max=self.queue_max,
            )

        self._waiters += 1
        wait_start = time.monotonic()
        try:
            await self._sem.acquire()
            wait_ms = (time.monotonic() - wait_start) * 1000.0
        finally:
            self._waiters -= 1

        self.stats.accepted += 1
        self.stats.wait_ms_total += wait_ms
        if wait_ms > self.stats.wait_ms_max:
            self.stats.wait_ms_max = wait_ms

        try:
            yield {"queue_wait_ms": wait_ms, "lane": self.name}
        finally:
            self._sem.release()


class LaneGroup:
    """A keyed family of lanes sharing a default capacity.

    Used for the per-resource-type lanes (``session_create``, ``tool``)
    where we want, e.g., a small budget for ``vm`` but a larger one for
    ``rag``, with a fallback ``default`` lane that handles unknown
    resource types without dropping requests on the floor.
    """

    def __init__(self, name: str, defaults: Mapping[str, int],
                 default_key: str = "default", queue_max: int = 0):
        self.name = name
        self.default_key = default_key
        self.queue_max = queue_max
        self._lanes: Dict[str, Bound] = {}
        for key, cap in defaults.items():
            self._lanes[key] = Bound(f"{name}.{key}", cap, queue_max=queue_max)
        if default_key not in self._lanes:
            # Always have a fallback so unknown resource types can flow.
            self._lanes[default_key] = Bound(
                f"{name}.{default_key}", 16, queue_max=queue_max
            )

    def get(self, key: Optional[str]) -> Bound:
        """Resolve a lane by key, falling back to the group default."""
        if key and key in self._lanes:
            return self._lanes[key]
        return self._lanes[self.default_key]

    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        return {
            k: {
                "capacity": b.capacity,
                "queue_max": b.queue_max,
                "waiters": b.waiters,
                "accepted": b.stats.accepted,
                "rejected": b.stats.rejected,
                "wait_ms_total": b.stats.wait_ms_total,
                "wait_ms_max": b.stats.wait_ms_max,
            }
            for k, b in self._lanes.items()
        }


@dataclass
class BackpressureManager:
    """Top-level container exposed on ``server.backpressure``.

    Layout::

        manager.global_inflight      Bound       - server-wide cap
        manager.health               Bound       - cheap probes
        manager.status               Bound       - cheap status reads
        manager.session_create       LaneGroup   - per resource_type
        manager.tool                 LaneGroup   - per resource_type

    All lanes are optional in the sense that the caller picks which one
    is appropriate for the endpoint; uninstalled lanes simply aren't
    referenced. Default capacities come from :func:`build_default_limiter`.
    """

    global_inflight: Bound
    health: Bound
    status: Bound
    session_create: LaneGroup
    tool: LaneGroup

    def snapshot(self) -> Dict[str, Any]:
        return {
            "global_inflight": {
                "capacity": self.global_inflight.capacity,
                "waiters": self.global_inflight.waiters,
                "accepted": self.global_inflight.stats.accepted,
                "rejected": self.global_inflight.stats.rejected,
            },
            "health": {
                "capacity": self.health.capacity,
                "accepted": self.health.stats.accepted,
                "rejected": self.health.stats.rejected,
            },
            "status": {
                "capacity": self.status.capacity,
                "accepted": self.status.stats.accepted,
                "rejected": self.status.stats.rejected,
            },
            "session_create": self.session_create.snapshot(),
            "tool": self.tool.snapshot(),
        }


# ---------------------------------------------------------------------------
# Defaults
#
# Tuned for "one server in front of ~100 rollout workers". Numbers can
# be overridden via `server_config.limits` (see config_loader; the
# manager builder is also called from tests with explicit overrides).
# ---------------------------------------------------------------------------

DEFAULT_LIMITS: Dict[str, Any] = {
    "global_inflight": 512,
    "global_queue_max": 1024,
    "health_inflight": 256,
    "status_inflight": 128,
    "session_create": {
        "vm": 2,
        "browser": 4,
        "rag": 8,
        "default": 16,
    },
    "session_create_queue_max": 32,
    "tool": {
        "vm": 1,
        "browser": 1,
        "rag": 64,
        "websearch": 16,
        "default": 32,
    },
    "tool_queue_max": 0,  # 0 = no rejection; tool lanes just throttle.
}


def build_default_limiter(
    overrides: Optional[Mapping[str, Any]] = None,
) -> BackpressureManager:
    """Materialise the default :class:`BackpressureManager`.

    ``overrides`` may shallow-merge any of the top-level keys in
    :data:`DEFAULT_LIMITS`. Per-key dict overrides (e.g.
    ``session_create``) are *merged* rather than replaced so users can
    override a single resource type without losing the rest.
    """
    cfg = dict(DEFAULT_LIMITS)
    if overrides:
        for k, v in overrides.items():
            if k in ("session_create", "tool") and isinstance(v, Mapping):
                merged = dict(cfg.get(k, {}))
                merged.update(v)
                cfg[k] = merged
            else:
                cfg[k] = v

    return BackpressureManager(
        global_inflight=Bound(
            "global_inflight",
            int(cfg["global_inflight"]),
            queue_max=int(cfg["global_queue_max"]),
        ),
        health=Bound("health", int(cfg["health_inflight"])),
        status=Bound("status", int(cfg["status_inflight"])),
        session_create=LaneGroup(
            "session_create",
            cfg["session_create"],
            queue_max=int(cfg["session_create_queue_max"]),
        ),
        tool=LaneGroup("tool", cfg["tool"], queue_max=int(cfg["tool_queue_max"])),
    )


__all__ = [
    "Bound",
    "LaneGroup",
    "BackpressureManager",
    "OverloadedError",
    "overloaded_response",
    "build_default_limiter",
    "DEFAULT_LIMITS",
]

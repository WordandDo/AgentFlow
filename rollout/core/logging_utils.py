"""
Structured logging utilities for the rollout pipeline.

Provides:
- A root logger handler with a uniform format that includes
  per-request context fields (``run_id``, ``worker_id``, ``task_id``,
  ``trace_id``) carried by ``contextvars`` so the context follows
  coroutine switches without manual plumbing.
- ``get_logger`` / ``set_context`` / ``clear_context`` helpers.
- A small async-safe ``Progress`` wrapper around ``tqdm.asyncio``.

The goal is to replace ad-hoc ``print(...)`` calls so that high
concurrency runs can be grepped per ``run_id`` / ``worker_id`` /
``task_id`` / ``trace_id``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from contextvars import ContextVar
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, Optional


_ctx_run_id: ContextVar[str] = ContextVar("run_id", default="-")
_ctx_worker_id: ContextVar[str] = ContextVar("worker_id", default="-")
_ctx_task_id: ContextVar[str] = ContextVar("task_id", default="-")
_ctx_trace_id: ContextVar[str] = ContextVar("trace_id", default="-")


_CTX_VARS = {
    "run_id": _ctx_run_id,
    "worker_id": _ctx_worker_id,
    "task_id": _ctx_task_id,
    "trace_id": _ctx_trace_id,
}


class _ContextFilter(logging.Filter):
    """Inject context fields into every LogRecord."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = _ctx_run_id.get()
        record.worker_id = _ctx_worker_id.get()
        record.task_id = _ctx_task_id.get()
        record.trace_id = _ctx_trace_id.get()
        return True


def install_root_handler(level: str = "INFO") -> None:
    """Install the rollout structured handler on the root logger.

    Safe to call multiple times; subsequent calls are no-ops. Uses an
    attribute marker on the root logger so re-imports under pytest /
    notebooks do not pile up duplicate handlers.
    """
    root = logging.getLogger()
    if getattr(root, "_agentflow_installed", False):
        # Allow level upgrades on repeat calls.
        root.setLevel(getattr(logging, level.upper(), logging.INFO))
        return

    handler = logging.StreamHandler(sys.stderr)
    fmt = (
        "%(asctime)s [%(levelname)s] %(name)s "
        "run=%(run_id)s w=%(worker_id)s task=%(task_id)s trace=%(trace_id)s | %(message)s"
    )
    handler.setFormatter(logging.Formatter(fmt, datefmt="%H:%M:%S"))
    handler.addFilter(_ContextFilter())
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    # Mark to avoid double-install on re-imports.
    root._agentflow_installed = True  # type: ignore[attr-defined]


def get_logger(name: str) -> logging.Logger:
    """Return a logger by name (thin wrapper for symmetry)."""
    return logging.getLogger(name)


def set_context(**kwargs: Any) -> Dict[str, Any]:
    """Set one or more context fields, returning reset tokens.

    Pass the returned dict to :func:`clear_context` to restore the
    previous values. ``None`` values are ignored, so callers can safely
    pass optional kwargs without sprinkling ``if x is not None``.
    """
    tokens: Dict[str, Any] = {}
    for key, value in kwargs.items():
        if value is None:
            continue
        var = _CTX_VARS.get(key)
        if var is None:
            continue
        tokens[key] = var.set(str(value))
    return tokens


def clear_context(tokens: Dict[str, Any]) -> None:
    """Restore context fields set by :func:`set_context`."""
    for key, token in tokens.items():
        var = _CTX_VARS.get(key)
        if var is not None:
            try:
                var.reset(token)
            except ValueError:
                # Token was created in a different context (e.g. across
                # task boundaries); fall back to clearing the slot.
                var.set("-")


def get_context() -> Dict[str, str]:
    """Snapshot of the current logging context (useful for debugging)."""
    return {name: var.get() for name, var in _CTX_VARS.items()}


def attach_worker_file_handler(
    worker_id: str,
    log_dir: str,
    *,
    level: str = "INFO",
    max_bytes: int = 100 * 1024 * 1024,
    backup_count: int = 3,
) -> logging.Handler:
    """Attach a rotating per-worker file handler to the root logger.

    The same `_ContextFilter` is installed so each line carries
    ``run/worker/task/trace`` fields. The handler also filters on
    ``record.worker_id == worker_id`` so the per-worker file only
    receives lines emitted from contexts that own that worker (the
    pool's `set_context(worker_id=...)` already covers that, including
    nested per-task context).

    Returns the handler so the caller can detach it via
    :func:`detach_handler` in their `finally` block; without this,
    long-running processes leak file descriptors.
    """
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, f"rollout.worker.{worker_id}.log")

    handler = RotatingFileHandler(
        path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8",
    )
    fmt = (
        "%(asctime)s [%(levelname)s] %(name)s "
        "run=%(run_id)s w=%(worker_id)s task=%(task_id)s trace=%(trace_id)s | %(message)s"
    )
    handler.setFormatter(logging.Formatter(fmt))
    handler.addFilter(_ContextFilter())

    # Restrict this handler to records that belong to this worker, so
    # the per-worker file is grep-clean even though all handlers share
    # the root logger.
    def _worker_filter(record: logging.LogRecord, target: str = worker_id) -> bool:
        return getattr(record, "worker_id", "-") == target

    handler.addFilter(_worker_filter)
    handler.setLevel(getattr(logging, level.upper(), logging.INFO))
    logging.getLogger().addHandler(handler)
    return handler


def detach_handler(handler: logging.Handler) -> None:
    """Remove and close a handler previously attached to the root."""
    if handler is None:
        return
    root = logging.getLogger()
    try:
        root.removeHandler(handler)
    finally:
        try:
            handler.close()
        except Exception:
            pass


class Progress:
    """Async-safe wrapper around ``tqdm.asyncio.tqdm``.

    The wrapper degrades gracefully when ``tqdm`` is not installed; in
    that case ``update`` / ``close`` are no-ops so the pipeline still
    works without a progress bar.
    """

    def __init__(self, total: int, desc: str = ""):
        self._pbar = None
        try:
            from tqdm.asyncio import tqdm  # type: ignore

            self._pbar = tqdm(total=total, desc=desc, dynamic_ncols=True, mininterval=0.5)
        except ImportError:
            self._pbar = None
        self._lock = asyncio.Lock()

    async def update(self, n: int = 1, postfix: Optional[Dict[str, Any]] = None) -> None:
        async with self._lock:
            if self._pbar is None:
                return
            if postfix:
                self._pbar.set_postfix(postfix, refresh=False)
            self._pbar.update(n)

    def close(self) -> None:
        if self._pbar is not None:
            self._pbar.close()
            self._pbar = None


__all__ = [
    "install_root_handler",
    "get_logger",
    "set_context",
    "clear_context",
    "get_context",
    "attach_worker_file_handler",
    "detach_handler",
    "Progress",
]

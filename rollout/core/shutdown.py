"""
Cooperative shutdown coordination for the rollout pipeline.

Provides :class:`ShutdownManager` which installs ``SIGINT`` / ``SIGTERM``
handlers on the running event loop. The first signal sets a shared
``asyncio.Event`` (graceful shutdown), subsequent signals warn the user
that cleanup is still in progress, and after ``force_exit_after``
signals the process is hard-killed via ``os._exit(130)``.

The class is intentionally minimal: it does not orchestrate cleanup
itself. Callers should await ``manager.event`` (or
``wait_for_shutdown``) and then run their cancel-safe cleanup logic.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal as _signal
from typing import Optional


log = logging.getLogger("rollout.shutdown")


class ShutdownManager:
    """Cooperative shutdown coordinator.

    Usage::

        sm = ShutdownManager()
        sm.install(asyncio.get_running_loop())
        ...
        if sm.triggered:
            ...  # graceful exit branch
    """

    def __init__(self, force_exit_after: int = 3):
        if force_exit_after < 2:
            raise ValueError("force_exit_after must be >= 2 (first signal is graceful)")
        self.event = asyncio.Event()
        self._count = 0
        self._force_after = force_exit_after
        self._installed = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def install(self, loop: asyncio.AbstractEventLoop) -> None:
        """Register SIGINT/SIGTERM handlers on ``loop``.

        Falls back to ``signal.signal`` on platforms (Windows) or threads
        where ``add_signal_handler`` is not supported.
        """
        if self._installed:
            return
        self._loop = loop
        for sig in (_signal.SIGINT, _signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._on_signal, sig)
            except (NotImplementedError, RuntimeError, ValueError):
                # add_signal_handler requires main thread on Unix and is
                # unavailable on Windows; fall back to the classic
                # signal.signal API.
                try:
                    _signal.signal(sig, lambda s, _f: self._on_signal(s))
                except (ValueError, OSError):
                    # Some embedding contexts (e.g. pytest with capture)
                    # disallow signal installation. Skip silently.
                    log.debug("could not install handler for signal %s", sig)
        self._installed = True

    def _on_signal(self, sig) -> None:
        self._count += 1
        if self._count == 1:
            log.warning(
                "Received signal %s; graceful shutdown started "
                "(send the signal again to warn, %dx total to force-exit)",
                sig, self._force_after,
            )
            # Setting the event must be done from the loop thread; on the
            # ``add_signal_handler`` path we are already on it, on the
            # fallback ``signal.signal`` path we schedule it.
            if self._loop is not None and self._loop.is_running():
                try:
                    self._loop.call_soon_threadsafe(self.event.set)
                except RuntimeError:
                    self.event.set()
            else:
                self.event.set()
        elif self._count < self._force_after:
            log.warning(
                "Signal %s received again (%d/%d); cleanup still in progress, "
                "press once more to force-exit",
                sig, self._count, self._force_after,
            )
        else:
            log.error("Force exit on signal %s (count=%d)", sig, self._count)
            os._exit(130)

    @property
    def triggered(self) -> bool:
        return self.event.is_set()

    async def wait(self) -> None:
        """Convenience wrapper for ``await manager.event.wait()``."""
        await self.event.wait()


__all__ = ["ShutdownManager"]

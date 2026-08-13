"""Listener interface and reconnect policy.

Every backend is an async iterator of :class:`LaunchEvent`. Reconnects are the
listener's own problem — the consumer in `main` never sees a disconnect, it
just sees a gap in events — because a sniper that stops on a dropped socket is
a sniper that misses the launch.
"""

from __future__ import annotations

import abc
import asyncio
import random
import time
from typing import AsyncIterator

from ..config import GeyserConfig
from ..logging_setup import get_logger
from ..pumpfun import LaunchEvent

log = get_logger("sniper.listener")


class Listener(abc.ABC):
    """Base class for a push-based launch feed."""

    def __init__(self, cfg: GeyserConfig) -> None:
        self.cfg = cfg
        self._stopped = asyncio.Event()

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Short backend name, used in logs."""

    @abc.abstractmethod
    def _connect_and_stream(self) -> AsyncIterator[LaunchEvent]:
        """Open one connection and yield until it drops.

        Implementations raise on disconnect; :meth:`stream` handles the retry.
        """

    def stop(self) -> None:
        self._stopped.set()

    async def stream(self) -> AsyncIterator[LaunchEvent]:
        """Yield launches forever, reconnecting with jittered backoff."""
        delay = self.cfg.reconnect_min_s
        while not self._stopped.is_set():
            started = time.monotonic()
            try:
                async for event in self._connect_and_stream():
                    if self._stopped.is_set():
                        return
                    # A connection that delivered anything was healthy, so the
                    # next drop starts over at the minimum delay rather than
                    # inheriting backoff from an outage minutes ago.
                    delay = self.cfg.reconnect_min_s
                    yield event
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                uptime = time.monotonic() - started
                log.warning(
                    "listener_disconnected",
                    extra={
                        "backend": self.name,
                        "error": repr(exc),
                        "uptime_s": round(uptime, 2),
                        "retry_in_s": round(delay, 2),
                    },
                )
            if self._stopped.is_set():
                return
            # Full jitter: spreads reconnects out if the endpoint restarts and
            # every client comes back at once.
            await asyncio.sleep(random.uniform(0, delay))
            delay = min(delay * 2, self.cfg.reconnect_max_s)


def create_listener(cfg: GeyserConfig) -> Listener:
    """Build the listener named by `cfg.backend`.

    Imports are deferred so a missing optional dependency (grpcio for the
    yellowstone backend) only matters if that backend is actually selected.
    """
    if cfg.backend == "logs":
        from .logs_ws import LogsListener

        return LogsListener(cfg)
    if cfg.backend == "helius_atlas":
        from .atlas_ws import AtlasListener

        return AtlasListener(cfg)
    if cfg.backend == "yellowstone":
        from .yellowstone import YellowstoneListener

        return YellowstoneListener(cfg)
    raise ValueError(f"unknown geyser backend: {cfg.backend!r}")

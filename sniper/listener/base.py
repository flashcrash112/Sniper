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
from contextlib import suppress
from dataclasses import dataclass
from typing import AsyncIterator, Optional

from ..config import GeyserConfig
from ..logging_setup import get_logger
from ..pumpfun import LaunchEvent

log = get_logger("sniper.listener")


class Listener(abc.ABC):
    """Base class for a push-based launch feed."""

    def __init__(self, cfg: GeyserConfig) -> None:
        self.cfg = cfg
        self._stopped = asyncio.Event()
        self.subscribed = False
        """Set once the feed has acknowledged our subscription.

        Distinguishes "your plan does not allow this subscription" from
        "connected fine, the feed is just quiet" — which look identical from
        the outside and have completely different fixes.
        """

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


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """What one connection attempt actually achieved."""

    backend: str
    subscribed: bool
    events_seen: int
    first_event_ms: Optional[float] = None
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.subscribed and self.events_seen > 0

    def explain(self) -> str:
        """A diagnosis, not a status code."""
        if self.error and not self.subscribed:
            return (
                f"could not subscribe: {self.error}\n"
                f"    Most likely your plan does not include this subscription "
                f"type, or the endpoint/API key is wrong."
            )
        if not self.subscribed:
            return "connected but the subscription was never acknowledged"
        if self.events_seen == 0:
            return (
                "subscribed, but no pump.fun launches arrived in the probe "
                "window.\n    That is unusual on mainnet — the feed may be "
                "filtered or lagging."
            )
        return (
            f"subscribed and receiving — {self.events_seen} launches, first "
            f"after {self.first_event_ms:.0f} ms"
        )


async def probe_listener(cfg: GeyserConfig, timeout_s: float = 20.0) -> ProbeResult:
    """Connect once, without retrying, and report what happened.

    Deliberately bypasses :meth:`Listener.stream`'s reconnect loop: for a
    diagnostic, an authentication failure is the answer, not something to back
    off and retry.
    """
    listener = create_listener(cfg)
    started = time.monotonic()
    events = 0
    first_ms: Optional[float] = None
    error: Optional[str] = None

    stream = listener._connect_and_stream()
    try:

        async def consume() -> None:
            nonlocal events, first_ms
            async for _event in stream:
                events += 1
                if first_ms is None:
                    first_ms = (time.monotonic() - started) * 1000
                if events >= 3:
                    return

        await asyncio.wait_for(consume(), timeout=timeout_s)
    except asyncio.TimeoutError:
        pass  # Not a failure by itself — the feed may simply be quiet.
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        error = repr(exc)
    finally:
        with suppress(Exception):
            await stream.aclose()
        listener.stop()

    return ProbeResult(
        backend=listener.name,
        subscribed=listener.subscribed,
        events_seen=events,
        first_event_ms=first_ms,
        error=error,
    )


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

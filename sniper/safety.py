"""Kill switch and spend accounting.

Both are checked immediately before a transaction is signed, and both are
designed so that check costs nothing: the kill switch is an attribute read
against a flag maintained by a background poller, and the ledger is plain
integer arithmetic. No syscalls, no locks, no I/O on the hot path.
"""

from __future__ import annotations

import asyncio
import os
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .config import SafetyConfig
from .constants import LAMPORTS_PER_SOL
from .logging_setup import get_logger

log = get_logger("sniper.safety")


class KillSwitch:
    """Halts trading via a flag file or a signal.

    Two independent triggers, because they fail in different ways:

    * **File flag** — ``touch KILL`` from any shell, any user with write access
      to the directory, no need to find the PID. Polled on a background task.
    * **Signals** — SIGINT/SIGTERM for a normal stop, SIGUSR1 for "stop trading
      but keep running and reporting".

    Once engaged it never disengages; restarting the bot is the only way back,
    which is the right default for something that spends money.
    """

    POLL_INTERVAL_S = 0.2

    def __init__(self, flag_file: Path) -> None:
        self.flag_file = flag_file
        self._engaged = False
        self._reason: Optional[str] = None
        self._shutdown = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

    @property
    def engaged(self) -> bool:
        """Hot-path check: a single attribute read."""
        return self._engaged

    @property
    def reason(self) -> Optional[str]:
        return self._reason

    @property
    def shutdown_requested(self) -> asyncio.Event:
        """Set when the process should exit, as opposed to just stop trading."""
        return self._shutdown

    def engage(self, reason: str, shutdown: bool = True) -> None:
        if not self._engaged:
            self._engaged = True
            self._reason = reason
            log.warning("kill_switch_engaged", extra={"reason": reason})
        if shutdown:
            self._shutdown.set()

    def install_signal_handlers(self, loop: asyncio.AbstractEventLoop) -> None:
        def handler(signum: int) -> None:
            name = signal.Signals(signum).name
            # SIGUSR1 stops trading but leaves the process up, so an in-flight
            # buy can still be tracked to its fill.
            self.engage(f"signal {name}", shutdown=signum != signal.SIGUSR1)

        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGUSR1):
            try:
                loop.add_signal_handler(signum, handler, signum)
            except (NotImplementedError, ValueError):  # pragma: no cover
                signal.signal(signum, lambda s, _f: handler(s))

    async def start(self) -> None:
        """Begin polling the flag file."""
        if self.flag_file.exists():
            # Refusing to start is the correct behaviour: a leftover flag file
            # means someone stopped this bot and has not cleared it.
            self.engage(f"kill switch file already present: {self.flag_file}")
            return
        self._task = asyncio.create_task(self._poll(), name="kill-switch-poll")

    async def _poll(self) -> None:
        while not self._engaged:
            try:
                if self.flag_file.exists():
                    self.engage(f"kill switch file created: {self.flag_file}")
                    return
            except OSError as exc:  # pragma: no cover - unusual fs failure
                log.warning("kill_switch_poll_error", extra={"error": repr(exc)})
            await asyncio.sleep(self.POLL_INTERVAL_S)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None


@dataclass(slots=True)
class Reservation:
    """A committed spend, held until the buy resolves."""

    lamports: int
    released: bool = False


class SpendLedger:
    """Enforces the maximum total spend and position count.

    Spend is *reserved* before the transaction is sent and only released if the
    send fails. A confirmed buy keeps its reservation forever. That ordering is
    what makes the cap a real cap: a transaction that was broadcast might land
    even if we never see the confirmation, so its lamports must stay counted.
    """

    def __init__(self, cfg: SafetyConfig) -> None:
        self.cfg = cfg
        self._reserved = 0
        self._positions = 0

    @property
    def reserved_lamports(self) -> int:
        return self._reserved

    @property
    def positions(self) -> int:
        return self._positions

    @property
    def remaining_lamports(self) -> int:
        return max(0, self.cfg.max_total_spend_lamports - self._reserved)

    def can_afford(self, lamports: int) -> bool:
        return (
            self._positions < self.cfg.max_positions
            and self._reserved + lamports <= self.cfg.max_total_spend_lamports
        )

    def reserve(self, lamports: int) -> Optional[Reservation]:
        """Reserve `lamports` for a buy, or return None if it breaches a limit.

        Single-threaded asyncio means the check and the increment cannot be
        interleaved, so no lock is needed — but nothing may await between them.
        """
        if not self.can_afford(lamports):
            return None
        self._reserved += lamports
        self._positions += 1
        return Reservation(lamports=lamports)

    def release(self, reservation: Reservation) -> None:
        """Give the lamports back — only valid if the send definitely failed."""
        if reservation.released:
            return
        reservation.released = True
        self._reserved -= reservation.lamports
        self._positions -= 1

    def summary(self) -> dict:
        return {
            "positions": self._positions,
            "max_positions": self.cfg.max_positions,
            "reserved_sol": round(self._reserved / LAMPORTS_PER_SOL, 6),
            "cap_sol": self.cfg.max_total_spend_sol,
        }


def clear_kill_switch(flag_file: Path) -> bool:
    """Remove the flag file. Returns whether anything was removed."""
    try:
        os.unlink(flag_file)
        return True
    except FileNotFoundError:
        return False

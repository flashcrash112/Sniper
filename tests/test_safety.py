import asyncio
from pathlib import Path

import pytest

from sniper.config import SafetyConfig
from sniper.constants import LAMPORTS_PER_SOL
from sniper.safety import KillSwitch, SpendLedger


def cfg(tmp_path: Path, **kwargs) -> SafetyConfig:
    defaults = dict(
        kill_switch_file=tmp_path / "KILL", max_total_spend_sol=1.0, max_positions=1
    )
    defaults.update(kwargs)
    return SafetyConfig(**defaults)


# --- spend ledger ----------------------------------------------------------


def test_ledger_allows_one_position_then_stops(tmp_path):
    ledger = SpendLedger(cfg(tmp_path))
    assert ledger.reserve(LAMPORTS_PER_SOL // 2) is not None
    assert ledger.reserve(LAMPORTS_PER_SOL // 4) is None, "max_positions ignored"


def test_ledger_enforces_the_spend_cap(tmp_path):
    ledger = SpendLedger(cfg(tmp_path, max_positions=10))
    assert ledger.reserve(int(0.6 * LAMPORTS_PER_SOL)) is not None
    assert ledger.reserve(int(0.6 * LAMPORTS_PER_SOL)) is None
    assert ledger.reserve(int(0.4 * LAMPORTS_PER_SOL)) is not None
    assert ledger.remaining_lamports == 0


def test_release_returns_the_reservation(tmp_path):
    ledger = SpendLedger(cfg(tmp_path))
    reservation = ledger.reserve(LAMPORTS_PER_SOL // 2)
    assert ledger.positions == 1
    ledger.release(reservation)
    assert ledger.positions == 0
    assert ledger.reserved_lamports == 0
    assert ledger.reserve(LAMPORTS_PER_SOL // 2) is not None


def test_double_release_is_a_no_op(tmp_path):
    """A double release would silently raise the effective spend cap."""
    ledger = SpendLedger(cfg(tmp_path, max_positions=5))
    reservation = ledger.reserve(LAMPORTS_PER_SOL // 2)
    ledger.release(reservation)
    ledger.release(reservation)
    assert ledger.reserved_lamports == 0
    assert ledger.positions == 0


def test_a_sent_buy_keeps_its_reservation_forever(tmp_path):
    """A broadcast transaction might still land; its lamports stay counted."""
    ledger = SpendLedger(cfg(tmp_path, max_positions=2))
    ledger.reserve(int(0.7 * LAMPORTS_PER_SOL))
    assert not ledger.can_afford(int(0.7 * LAMPORTS_PER_SOL))


# --- kill switch -----------------------------------------------------------


@pytest.mark.asyncio
async def test_kill_switch_refuses_to_start_with_a_stale_flag(tmp_path):
    flag = tmp_path / "KILL"
    flag.touch()
    switch = KillSwitch(flag)
    await switch.start()
    assert switch.engaged
    assert "already present" in switch.reason


@pytest.mark.asyncio
async def test_kill_switch_trips_when_the_file_appears(tmp_path):
    flag = tmp_path / "KILL"
    switch = KillSwitch(flag)
    switch.POLL_INTERVAL_S = 0.01
    await switch.start()
    assert not switch.engaged

    flag.touch()
    await asyncio.sleep(0.1)
    assert switch.engaged
    assert switch.shutdown_requested.is_set()
    await switch.stop()


@pytest.mark.asyncio
async def test_kill_switch_never_disengages(tmp_path):
    """Once tripped, removing the file must not resume trading."""
    flag = tmp_path / "KILL"
    switch = KillSwitch(flag)
    switch.POLL_INTERVAL_S = 0.01
    await switch.start()
    flag.touch()
    await asyncio.sleep(0.1)
    flag.unlink()
    await asyncio.sleep(0.1)
    assert switch.engaged
    await switch.stop()


@pytest.mark.asyncio
async def test_sigusr1_halts_trading_without_shutdown(tmp_path):
    switch = KillSwitch(tmp_path / "KILL")
    switch.engage("signal SIGUSR1", shutdown=False)
    assert switch.engaged
    assert not switch.shutdown_requested.is_set()

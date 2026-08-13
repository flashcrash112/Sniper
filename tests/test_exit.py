"""Exit logic: sell quoting, sell instruction encoding, and trigger behaviour."""

import struct
import time

import pytest
from solders.hash import Hash
from solders.keypair import Keypair
from solders.pubkey import Pubkey

from sniper.buyer import BlockhashCache
from sniper.config import ExitConfig
from sniper.constants import (
    EVENT_AUTHORITY,
    GLOBAL_ACCOUNT,
    LAMPORTS_PER_SOL,
    PUMP_FUN_PROGRAM,
    SELL_IX_DISCRIMINATOR,
    SYSTEM_PROGRAM,
    TOKEN_PROGRAM,
)
from sniper.curve import (
    CurveState,
    net_sol_from_sale,
    quote_sell,
    sol_out_for_tokens,
    tokens_for_sol,
)
from sniper.exit import ExitManager, Position
from sniper.pumpfun import (
    BuyLayout,
    SellAccounts,
    build_sell_instruction,
    parse_bonding_curve,
)


# --- sell curve math -------------------------------------------------------


def test_selling_returns_less_than_the_buy_cost_due_to_fees_and_spread():
    """A buy immediately followed by a sell must lose money, not print it."""
    state = CurveState.initial()
    spend = LAMPORTS_PER_SOL
    tokens = tokens_for_sol(state, spend)

    moved = CurveState(
        virtual_token_reserves=state.virtual_token_reserves - tokens,
        virtual_sol_reserves=state.virtual_sol_reserves + spend,
        real_token_reserves=state.real_token_reserves - tokens,
    )
    assert net_sol_from_sale(moved, tokens) < spend


def test_sol_out_rises_with_tokens_sold_but_sublinearly():
    """Price impact: dumping twice as many tokens returns less than twice the SOL.

    Measured at a size that is a real fraction of the reserves. At tiny sizes
    the curvature is smaller than the integer rounding, so a test there would
    be measuring floor() rather than the curve.
    """
    state = CurveState.initial()
    small = sol_out_for_tokens(state, 100_000_000_000_000)
    large = sol_out_for_tokens(state, 200_000_000_000_000)
    assert small < large < small * 2


def test_quote_sell_sets_a_floor_below_the_expectation():
    state = CurveState.initial()
    quote = quote_sell(state, 1_000_000_000, slippage_bps=1500)
    assert quote.min_sol_output < quote.expected_sol_output
    assert quote.min_sol_output == quote.expected_sol_output * 8500 // 10000


def test_zero_slippage_sell_demands_the_full_expectation():
    state = CurveState.initial()
    quote = quote_sell(state, 1_000_000_000, slippage_bps=0)
    assert quote.min_sol_output == quote.expected_sol_output


def test_quote_sell_rejects_a_zero_position():
    with pytest.raises(ValueError):
        quote_sell(CurveState.initial(), 0, 100)


# --- sell instruction ------------------------------------------------------


def sell_accounts():
    return SellAccounts(
        fee_recipient=Pubkey.new_unique(),
        mint=Pubkey.new_unique(),
        bonding_curve=Pubkey.new_unique(),
        associated_bonding_curve=Pubkey.new_unique(),
        associated_user=Pubkey.new_unique(),
        user=Pubkey.new_unique(),
        creator_vault=Pubkey.new_unique(),
    )


def test_sell_instruction_encoding():
    accounts = sell_accounts()
    ix = build_sell_instruction(accounts, token_amount=12345, min_sol_output=678)

    assert ix.program_id == PUMP_FUN_PROGRAM
    data = bytes(ix.data)
    assert data[:8] == SELL_IX_DISCRIMINATOR
    amount, min_out = struct.unpack_from("<QQ", data, 8)
    assert (amount, min_out) == (12345, 678)
    assert len(data) == 24, "sell has no trailing Option field"
    assert len(ix.accounts) == 14


def test_sell_account_order_differs_from_buy():
    """creator_vault sits at 8 and token_program at 9 — the reverse of buy."""
    accounts = sell_accounts()
    ix = build_sell_instruction(accounts, 1, 1)
    keys = [meta.pubkey for meta in ix.accounts]

    assert keys[0] == GLOBAL_ACCOUNT
    assert keys[6] == accounts.user
    assert keys[7] == SYSTEM_PROGRAM
    assert keys[8] == accounts.creator_vault
    assert keys[9] == TOKEN_PROGRAM
    assert keys[10] == EVENT_AUTHORITY
    assert keys[11] == PUMP_FUN_PROGRAM


def test_sell_signer_and_writable_flags():
    accounts = sell_accounts()
    ix = build_sell_instruction(accounts, 1, 1)
    signers = [m.pubkey for m in ix.accounts if m.is_signer]
    assert signers == [accounts.user]
    writable = {m.pubkey for m in ix.accounts if m.is_writable}
    assert accounts.bonding_curve in writable
    assert accounts.associated_user in writable
    assert GLOBAL_ACCOUNT not in writable


def test_legacy_sell_layout_drops_the_fee_accounts():
    ix = build_sell_instruction(sell_accounts(), 1, 1, layout=BuyLayout.LEGACY)
    assert len(ix.accounts) == 12


# --- bonding curve account -------------------------------------------------


def curve_account_bytes(complete=False, creator=None, vtok=1_000_000_000_000_000,
                        vsol=32_000_000_000):
    data = (
        b"\x00" * 8
        + struct.pack("<Q", vtok)
        + struct.pack("<Q", vsol)
        + struct.pack("<Q", 700_000_000_000_000)
        + struct.pack("<Q", 2_000_000_000)
        + struct.pack("<Q", 1_000_000_000_000_000)
        + bytes([1 if complete else 0])
    )
    if creator is not None:
        data += bytes(creator)
    return data


def test_parse_bonding_curve():
    creator = Pubkey.new_unique()
    state = parse_bonding_curve(curve_account_bytes(creator=creator))
    assert state.virtual_sol_reserves == 32_000_000_000
    assert state.real_sol_reserves == 2_000_000_000
    assert state.complete is False
    assert state.creator == creator


def test_parse_bonding_curve_without_the_creator_field():
    state = parse_bonding_curve(curve_account_bytes())
    assert state.creator is None
    assert state.complete is False


def test_parse_bonding_curve_complete_flag():
    assert parse_bonding_curve(curve_account_bytes(complete=True)).complete is True


# --- exit triggers ---------------------------------------------------------


class FakeRpc:
    """Returns a scripted sequence of curve states."""

    def __init__(self, curves, held=1_000_000_000):
        self.curves = list(curves)
        self.held = held
        self.sent = []

    async def get_account_data(self, pubkey):
        if not self.curves:
            return None
        value = self.curves[0]
        if len(self.curves) > 1:
            self.curves.pop(0)
        return value

    async def get_token_account_balance(self, pubkey):
        return self.held

    def build_send_body(self, raw):
        return "body"

    async def send_transaction(self, body):
        from sniper.rpc import SendResult

        self.sent.append(body)
        return SendResult(signature="SELLSIG", endpoint="x"), []


class FixedBlockhash(BlockhashCache):
    def __init__(self):
        self._blockhash = Hash.default()
        self._fetched_at = time.monotonic()

    @property
    def blockhash(self):
        return self._blockhash


def make_manager(rpc, **overrides):
    cfg_args = dict(
        enabled=True,
        take_profit_pct=200.0,
        stop_loss_pct=50.0,
        max_hold_s=300.0,
        poll_interval_s=0.01,
        slippage_bps=1500,
    )
    cfg_args.update(overrides)
    return ExitManager(
        keypair=Keypair(),
        rpc=rpc,
        blockhash=FixedBlockhash(),
        cfg=ExitConfig(**cfg_args),
        fee_recipient=Pubkey.new_unique(),
        base_curve=CurveState.initial(),
    )


def position(cost_lamports, token_amount=1_000_000_000):
    return Position(
        mint=Pubkey.new_unique(),
        creator=Pubkey.new_unique(),
        associated_user=Pubkey.new_unique(),
        token_amount=token_amount,
        cost_lamports=cost_lamports,
        opened_at=time.monotonic(),
    )


@pytest.mark.asyncio
async def test_take_profit_fires_and_sells():
    # A curve rich enough that the position is worth well over 2x its cost.
    rpc = FakeRpc([curve_account_bytes(vsol=300_000_000_000, vtok=100_000_000_000_000)])
    manager = make_manager(rpc)
    pos = position(cost_lamports=1_000_000)

    outcome = await manager.monitor(pos)
    assert outcome.sold
    assert "take profit" in outcome.reason
    assert outcome.signature == "SELLSIG"
    assert outcome.pnl_lamports > 0


@pytest.mark.asyncio
async def test_stop_loss_fires_and_sells():
    rpc = FakeRpc([curve_account_bytes(vsol=1_000_000, vtok=10_000_000_000_000_000)])
    manager = make_manager(rpc)
    pos = position(cost_lamports=10 * LAMPORTS_PER_SOL)

    outcome = await manager.monitor(pos)
    assert outcome.sold
    assert "stop loss" in outcome.reason
    assert outcome.pnl_lamports < 0


@pytest.mark.asyncio
async def test_max_hold_exits_a_flat_position():
    """Neither trigger hit, but time is up."""
    rpc = FakeRpc([curve_account_bytes()])
    manager = make_manager(rpc, max_hold_s=0.0)
    # Cost chosen so value sits between the stop and the take-profit.
    curve = parse_bonding_curve(curve_account_bytes()).to_curve(100, 5)
    value = net_sol_from_sale(curve, 1_000_000_000)

    outcome = await manager.monitor(position(cost_lamports=value))
    assert outcome.sold
    assert "max hold" in outcome.reason


@pytest.mark.asyncio
async def test_a_migrated_curve_is_not_sold_through_pumpfun():
    """Once complete, pump.fun sells revert — we must stop, not keep trying."""
    rpc = FakeRpc([curve_account_bytes(complete=True)])
    manager = make_manager(rpc)

    outcome = await manager.monitor(position(cost_lamports=1_000_000))
    assert not outcome.sold
    assert "migrated" in outcome.reason
    assert rpc.sent == []


@pytest.mark.asyncio
async def test_missing_curve_account_stops_cleanly():
    manager = make_manager(FakeRpc([]))
    outcome = await manager.monitor(position(cost_lamports=1_000_000))
    assert not outcome.sold
    assert "unreadable" in outcome.reason


@pytest.mark.asyncio
async def test_sell_is_capped_at_the_balance_actually_held():
    """The fill may differ from the quote; selling more than we hold reverts."""
    rpc = FakeRpc(
        [curve_account_bytes(vsol=300_000_000_000, vtok=100_000_000_000_000)],
        held=400_000_000,
    )
    manager = make_manager(rpc)
    outcome = await manager.monitor(position(cost_lamports=1_000, token_amount=1_000_000_000))

    assert outcome.sold
    assert outcome.quote.token_amount == 400_000_000


@pytest.mark.asyncio
async def test_nothing_held_means_nothing_sold():
    rpc = FakeRpc([curve_account_bytes(vsol=300_000_000_000, vtok=100_000_000_000_000)], held=0)
    manager = make_manager(rpc)
    outcome = await manager.monitor(position(cost_lamports=1_000))
    assert not outcome.sold
    assert outcome.error == "no tokens held"


@pytest.mark.asyncio
async def test_dry_run_exit_sends_nothing():
    rpc = FakeRpc([curve_account_bytes(vsol=300_000_000_000, vtok=100_000_000_000_000)])
    manager = make_manager(rpc)
    manager.dry_run = True

    outcome = await manager.monitor(position(cost_lamports=1_000_000))
    assert not outcome.sold
    assert rpc.sent == []
    assert outcome.quote is not None


@pytest.mark.asyncio
async def test_sell_transaction_is_signed_and_well_formed():
    rpc = FakeRpc([curve_account_bytes()])
    manager = make_manager(rpc)
    pos = position(cost_lamports=1_000_000)
    quote = quote_sell(CurveState.initial(), pos.token_amount, 1500)

    tx = manager.build_sell_transaction(pos, quote, Hash.default())
    assert all(tx.verify_with_results())
    assert len(tx.message.instructions) == 3  # 2 budget + sell
    assert len(bytes(tx)) <= 1232

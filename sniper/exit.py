"""Exit management: take-profit, stop-loss and time-based selling.

The buy side is a latency problem. The exit is not — it is a polling loop, and
the only thing that matters is that it is correct, honest about what it can see,
and impossible to run away with.

Position value is computed from the coin's own bonding curve account, which is
polled on an interval. That is the real, executable price: what the curve would
actually pay for the position right now, net of fees. It is not a market price
from an index, and it does not pretend the position could be exited without
moving the curve — selling into a thin curve is exactly what the quote models.

Three exit triggers, checked in this order:

1. **Stop loss** — value has fallen to `stop_loss_pct` of cost.
2. **Take profit** — value has reached `take_profit_pct` of cost.
3. **Max hold** — `max_hold_s` elapsed, exit regardless.

A curve that has completed (migrated to a DEX) cannot be sold through
pump.fun at all; the monitor detects this, logs it and stops, because
continuing to poll would be pretending we still have an exit we do not have.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Optional

from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from .buyer import BlockhashCache, set_compute_unit_limit, set_compute_unit_price
from .config import ExitConfig
from .constants import LAMPORTS_PER_SOL, PUMP_TOKEN_DECIMALS
from .curve import CurveState, SellQuote, quote_sell
from .logging_setup import get_logger
from .pumpfun import (
    BuyLayout,
    SellAccounts,
    build_sell_instruction,
    derive_associated_token_account,
    derive_bonding_curve,
    derive_creator_vault,
    parse_bonding_curve,
)
from .rpc import RpcPool

log = get_logger("sniper.exit")


@dataclass(frozen=True, slots=True)
class Position:
    """What we bought, and what it cost."""

    mint: Pubkey
    creator: Pubkey
    associated_user: Pubkey
    token_amount: int
    cost_lamports: int
    opened_at: float

    @property
    def age_s(self) -> float:
        return time.monotonic() - self.opened_at


@dataclass(frozen=True, slots=True)
class ExitOutcome:
    reason: str
    sold: bool
    signature: Optional[str] = None
    error: Optional[str] = None
    quote: Optional[SellQuote] = None
    value_lamports: int = 0
    pnl_lamports: int = 0

    def to_log(self) -> dict:
        return {
            "reason": self.reason,
            "sold": self.sold,
            "signature": self.signature,
            "error": self.error,
            "value_sol": round(self.value_lamports / LAMPORTS_PER_SOL, 9),
            "pnl_sol": round(self.pnl_lamports / LAMPORTS_PER_SOL, 9),
            "min_sol_output": self.quote.min_sol_output if self.quote else None,
        }


class ExitManager:
    """Watches one position and sells it when a trigger fires."""

    def __init__(
        self,
        keypair: Keypair,
        rpc: RpcPool,
        blockhash: BlockhashCache,
        cfg: ExitConfig,
        fee_recipient: Pubkey,
        base_curve: CurveState,
        layout: BuyLayout = BuyLayout.CURRENT,
        dry_run: bool = False,
    ) -> None:
        self.keypair = keypair
        self.pubkey = keypair.pubkey()
        self.rpc = rpc
        self.blockhash = blockhash
        self.cfg = cfg
        self.fee_recipient = fee_recipient
        self.base_curve = base_curve
        self.layout = layout
        self.dry_run = dry_run

    async def read_curve(self, mint: Pubkey) -> Optional[tuple[CurveState, bool]]:
        """Read the live curve for `mint`. Returns (state, completed)."""
        bonding_curve = derive_bonding_curve(mint)
        data = await self.rpc.get_account_data(str(bonding_curve))
        if not data:
            return None
        state = parse_bonding_curve(data)
        return (
            state.to_curve(
                self.base_curve.fee_basis_points,
                self.base_curve.creator_fee_basis_points,
            ),
            state.complete,
        )

    async def monitor(self, position: Position) -> ExitOutcome:
        """Poll until a trigger fires, then sell.

        Returns without selling if the curve migrated or the position vanished
        — both are states where there is nothing here to sell.
        """
        deadline = position.opened_at + self.cfg.max_hold_s
        take_profit = int(position.cost_lamports * self.cfg.take_profit_pct / 100)
        stop_loss = int(position.cost_lamports * self.cfg.stop_loss_pct / 100)

        log.info(
            "exit_monitor_started",
            extra={
                "mint": str(position.mint),
                "cost_sol": round(position.cost_lamports / LAMPORTS_PER_SOL, 9),
                "take_profit_sol": round(take_profit / LAMPORTS_PER_SOL, 9),
                "stop_loss_sol": round(stop_loss / LAMPORTS_PER_SOL, 9),
                "max_hold_s": self.cfg.max_hold_s,
            },
        )

        while True:
            reading = await self.read_curve(position.mint)
            if reading is None:
                return ExitOutcome("curve account unreadable", sold=False)
            curve, completed = reading

            if completed:
                # Migrated to a DEX. pump.fun sells revert from here on.
                return ExitOutcome("curve completed and migrated", sold=False)

            quote = quote_sell(curve, position.token_amount, self.cfg.slippage_bps)
            value = quote.expected_sol_output
            pnl = value - position.cost_lamports

            reason = None
            if value <= stop_loss:
                reason = f"stop loss ({self.cfg.stop_loss_pct}% of cost)"
            elif value >= take_profit:
                reason = f"take profit ({self.cfg.take_profit_pct}% of cost)"
            elif time.monotonic() >= deadline:
                reason = f"max hold ({self.cfg.max_hold_s}s)"

            log.debug(
                "exit_tick",
                extra={
                    "mint": str(position.mint),
                    "value_sol": round(value / LAMPORTS_PER_SOL, 9),
                    "pnl_sol": round(pnl / LAMPORTS_PER_SOL, 9),
                    "age_s": round(position.age_s, 1),
                    "trigger": reason,
                },
            )

            if reason is not None:
                return await self.sell(position, curve, reason, value, pnl)

            await asyncio.sleep(self.cfg.poll_interval_s)

    def build_sell_transaction(
        self, position: Position, quote: SellQuote, blockhash: Hash
    ) -> VersionedTransaction:
        mint = position.mint
        bonding_curve = derive_bonding_curve(mint)
        accounts = SellAccounts(
            fee_recipient=self.fee_recipient,
            mint=mint,
            bonding_curve=bonding_curve,
            associated_bonding_curve=derive_associated_token_account(bonding_curve, mint),
            associated_user=position.associated_user,
            user=self.pubkey,
            creator_vault=derive_creator_vault(position.creator),
        )
        instructions = [
            set_compute_unit_limit(self.cfg.compute_unit_limit),
            set_compute_unit_price(self.cfg.priority_fee_microlamports),
            build_sell_instruction(
                accounts,
                token_amount=quote.token_amount,
                min_sol_output=quote.min_sol_output,
                layout=self.layout,
            ),
        ]
        message = MessageV0.try_compile(
            payer=self.pubkey,
            instructions=instructions,
            address_lookup_table_accounts=[],
            recent_blockhash=blockhash,
        )
        return VersionedTransaction(message, [self.keypair])

    async def sell(
        self,
        position: Position,
        curve: CurveState,
        reason: str,
        value: int,
        pnl: int,
    ) -> ExitOutcome:
        """Build, sign and broadcast the sell."""
        # Re-quote against the balance the wallet actually holds. The fill may
        # have differed from the quote, and selling tokens we do not have
        # reverts the whole transaction.
        held = await self.rpc.get_token_account_balance(str(position.associated_user))
        amount = min(position.token_amount, held) if held is not None else position.token_amount
        if amount <= 0:
            return ExitOutcome(reason, sold=False, error="no tokens held")

        quote = quote_sell(curve, amount, self.cfg.slippage_bps)

        blockhash = self.blockhash.blockhash
        if blockhash is None:
            return ExitOutcome(reason, sold=False, error="no cached blockhash")

        transaction = self.build_sell_transaction(position, quote, blockhash)

        if self.dry_run:
            log.warning(
                "dry_run_sell",
                extra={
                    "mint": str(position.mint),
                    "reason": reason,
                    "token_amount": amount,
                    "expected_sol": round(
                        quote.expected_sol_output / LAMPORTS_PER_SOL, 9
                    ),
                    "min_sol_output": quote.min_sol_output,
                },
            )
            return ExitOutcome(
                reason, sold=False, quote=quote, value_lamports=value, pnl_lamports=pnl
            )

        body = self.rpc.build_send_body(bytes(transaction))
        winner, _ = await self.rpc.send_transaction(body)

        outcome = ExitOutcome(
            reason=reason,
            sold=winner.ok,
            signature=winner.signature,
            error=winner.error,
            quote=quote,
            value_lamports=value,
            pnl_lamports=pnl,
        )
        log.warning(
            "exit_sell",
            extra={
                "mint": str(position.mint),
                "token_amount_ui": round(amount / 10**PUMP_TOKEN_DECIMALS, 6),
                **outcome.to_log(),
            },
        )
        return outcome

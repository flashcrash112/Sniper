"""The buy module.

Everything that can be decided before a launch exists is decided at startup:
the keypair is unlocked, the fee recipient and curve parameters are read from
the chain, the compute-budget instructions are built, the static account metas
are resolved, the wallet's volume-accumulator PDA is derived, connections are
warm and a blockhash is already in hand.

What remains between "this is the coin" and "the transaction is on the wire":

1. derive three mint/creator-specific PDAs,
2. quote the buy off the known initial curve state,
3. build four instructions and compile the message,
4. sign (one ed25519 signature),
5. base64 + string-concat the request body,
6. POST it to every configured endpoint at once.

No RPC round trips, no metadata fetches, no config reads, no allocation of
anything large. :meth:`Buyer.execute` measures each of those stages and logs
the breakdown so the latency budget is verifiable rather than assumed.
"""

from __future__ import annotations

import asyncio
import struct
import time
from dataclasses import dataclass
from typing import Optional

from solders.hash import Hash
from solders.instruction import Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from .config import BuyConfig, JitoConfig, RpcConfig, SafetyConfig
from .constants import (
    COMPUTE_BUDGET_PROGRAM,
    GLOBAL_ACCOUNT,
    LAMPORTS_PER_SOL,
    PUMP_TOKEN_DECIMALS,
)
from .curve import BuyQuote, CurveState, quote_buy
from .jito import JitoClient, build_tip_instruction
from .logging_setup import get_logger
from .pumpfun import (
    BuyAccounts,
    LaunchEvent,
    build_buy_instruction,
    build_create_ata_idempotent_instruction,
    derive_associated_token_account,
    derive_bonding_curve,
    derive_creator_vault,
    derive_user_volume_accumulator,
    parse_global_account,
)
from .rpc import RpcPool, SendResult

log = get_logger("sniper.buyer")


def set_compute_unit_limit(units: int) -> Instruction:
    """ComputeBudget `SetComputeUnitLimit` (discriminator 0x02)."""
    return Instruction(
        program_id=COMPUTE_BUDGET_PROGRAM,
        data=bytes([2]) + struct.pack("<I", units),
        accounts=[],
    )


def set_compute_unit_price(micro_lamports: int) -> Instruction:
    """ComputeBudget `SetComputeUnitPrice` (discriminator 0x03).

    The price is per compute unit in micro-lamports, so the fee actually paid
    is ``price * limit / 1e6`` lamports — which is why the limit should be set
    close to what the transaction really uses rather than left at the default.
    """
    return Instruction(
        program_id=COMPUTE_BUDGET_PROGRAM,
        data=bytes([3]) + struct.pack("<Q", micro_lamports),
        accounts=[],
    )


class BlockhashCache:
    """Keeps a recent blockhash ready so the hot path never fetches one.

    A `getLatestBlockhash` round trip is 20-100 ms. Blockhashes stay valid for
    150 slots (~60 s), so refreshing on a timer costs nothing and removes that
    entire round trip from the critical path.
    """

    def __init__(self, rpc: RpcPool, refresh_ms: int, commitment: str = "confirmed") -> None:
        self._rpc = rpc
        self._interval = refresh_ms / 1000.0
        self._commitment = commitment
        self._blockhash: Optional[Hash] = None
        self._fetched_at = 0.0
        self._ready = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

    @property
    def blockhash(self) -> Optional[Hash]:
        """Hot-path read: no lock, no await."""
        return self._blockhash

    @property
    def age_s(self) -> float:
        return time.monotonic() - self._fetched_at if self._fetched_at else float("inf")

    async def start(self) -> None:
        await self._refresh_once()
        await self._ready.wait()
        self._task = asyncio.create_task(self._loop(), name="blockhash-refresh")

    async def _refresh_once(self) -> None:
        try:
            value, _ = await self._rpc.get_latest_blockhash(self._commitment)
            self._blockhash = Hash.from_string(value)
            self._fetched_at = time.monotonic()
            self._ready.set()
        except Exception as exc:
            log.warning("blockhash_refresh_failed", extra={"error": repr(exc)})

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            await self._refresh_once()

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None


@dataclass(frozen=True, slots=True)
class BuyOutcome:
    """The full record of one buy attempt."""

    event: LaunchEvent
    quote: BuyQuote
    dry_run: bool
    signature: Optional[str] = None
    send_error: Optional[str] = None
    endpoint: Optional[str] = None
    associated_user: Optional[Pubkey] = None
    timings_us: dict[str, int] = None  # type: ignore[assignment]
    bundle_id: Optional[str] = None
    bundle_error: Optional[str] = None

    @property
    def sent(self) -> bool:
        return self.signature is not None

    def to_log(self) -> dict:
        return {
            "mint": str(self.event.mint),
            "symbol": self.event.symbol,
            "dry_run": self.dry_run,
            "signature": self.signature,
            "endpoint": self.endpoint,
            "send_error": self.send_error,
            "token_amount": self.quote.token_amount,
            "token_amount_ui": round(
                self.quote.token_amount / 10**PUMP_TOKEN_DECIMALS, 6
            ),
            "max_sol_cost": round(self.quote.max_sol_cost / LAMPORTS_PER_SOL, 9),
            "expected_sol_cost": round(
                self.quote.expected_sol_cost / LAMPORTS_PER_SOL, 9
            ),
            "bundle_id": self.bundle_id,
            "bundle_error": self.bundle_error,
            "timings_us": self.timings_us or {},
        }


class Buyer:
    """Builds, signs and broadcasts pump.fun buys."""

    def __init__(
        self,
        keypair: Keypair,
        rpc: RpcPool,
        buy_cfg: BuyConfig,
        rpc_cfg: RpcConfig,
        safety_cfg: SafetyConfig,
        dry_run: bool = False,
        jito: Optional["JitoClient"] = None,
        jito_cfg: Optional[JitoConfig] = None,
    ) -> None:
        self.keypair = keypair
        self.pubkey = keypair.pubkey()
        self.rpc = rpc
        self.cfg = buy_cfg
        self.safety_cfg = safety_cfg
        self.dry_run = dry_run
        self.jito = jito
        self.jito_cfg = jito_cfg

        self.blockhash = BlockhashCache(rpc, rpc_cfg.blockhash_refresh_ms, rpc_cfg.commitment)

        # --- Precomputed, launch-independent state ---------------------------
        self._budget_ixs = [
            set_compute_unit_limit(buy_cfg.compute_unit_limit),
            set_compute_unit_price(buy_cfg.priority_fee_microlamports),
        ]
        # Depends only on our own wallet, so it never changes between launches.
        self._user_volume_accumulator = derive_user_volume_accumulator(self.pubkey)

        self._fee_recipient: Optional[Pubkey] = None
        self._initial_curve = CurveState.initial()

    @property
    def fee_recipient(self) -> Optional[Pubkey]:
        return self._fee_recipient

    @property
    def initial_curve(self) -> CurveState:
        return self._initial_curve

    async def prepare(self) -> None:
        """Resolve on-chain state and get the blockhash cache running."""
        try:
            data = await self.rpc.get_account_data(str(GLOBAL_ACCOUNT))
            if data:
                state = parse_global_account(data)
                self._fee_recipient = state.fee_recipient
                self._initial_curve = state.curve
                log.info(
                    "global_state_loaded",
                    extra={
                        "fee_recipient": str(state.fee_recipient),
                        "fee_bps": state.curve.fee_basis_points,
                        "virtual_sol_reserves": state.curve.virtual_sol_reserves,
                        "virtual_token_reserves": state.curve.virtual_token_reserves,
                    },
                )
        except Exception as exc:
            log.error("global_state_load_failed", extra={"error": repr(exc)})

        if self._fee_recipient is None:
            raise RuntimeError(
                "could not read the pump.fun global account; the fee recipient is "
                "required to build a buy. Check [rpc] http_url."
            )

        await self.blockhash.start()

    def quote(self, event: LaunchEvent) -> BuyQuote:
        """Quote the buy against the curve state this launch started at.

        A brand-new coin has made no trades, so its reserves are the initial
        ones. The create event carries them explicitly on current program
        versions; otherwise we fall back to the values from the global account.
        """
        state = event.curve or self._initial_curve
        if event.curve is not None:
            # The event reports reserves but not fee rates; those come from the
            # global account.
            state = CurveState(
                virtual_token_reserves=event.curve.virtual_token_reserves,
                virtual_sol_reserves=event.curve.virtual_sol_reserves,
                real_token_reserves=event.curve.real_token_reserves,
                fee_basis_points=self._initial_curve.fee_basis_points,
                creator_fee_basis_points=self._initial_curve.creator_fee_basis_points,
            )
        return quote_buy(state, self.cfg.amount_lamports, self.cfg.slippage_bps)

    def build_transaction(
        self, event: LaunchEvent, quote: BuyQuote, blockhash: Hash
    ) -> tuple[VersionedTransaction, Pubkey]:
        """Build and sign the buy. Returns the transaction and our token account."""
        assert self._fee_recipient is not None

        mint = event.mint
        bonding_curve = event.bonding_curve or derive_bonding_curve(mint)
        associated_bonding_curve = derive_associated_token_account(bonding_curve, mint)
        associated_user = derive_associated_token_account(self.pubkey, mint)
        creator_vault = derive_creator_vault(event.creator)

        accounts = BuyAccounts(
            fee_recipient=self._fee_recipient,
            mint=mint,
            bonding_curve=bonding_curve,
            associated_bonding_curve=associated_bonding_curve,
            associated_user=associated_user,
            user=self.pubkey,
            creator_vault=creator_vault,
            user_volume_accumulator=self._user_volume_accumulator,
        )

        instructions = [
            *self._budget_ixs,
            build_create_ata_idempotent_instruction(
                payer=self.pubkey, owner=self.pubkey, mint=mint, ata=associated_user
            ),
            build_buy_instruction(
                accounts,
                token_amount=quote.token_amount,
                max_sol_cost=quote.max_sol_cost,
                layout=self.cfg.layout,
                track_volume=self.cfg.track_volume,
            ),
        ]

        # The Jito tip must ride in the same transaction as the buy, so that a
        # bundle which lands pays the tip and one which does not, does not.
        if self.jito is not None and self.jito_cfg is not None:
            instructions.append(
                build_tip_instruction(
                    self.pubkey,
                    self.jito_cfg.tip_lamports,
                    self.jito.next_tip_account(),
                )
            )

        message = MessageV0.try_compile(
            payer=self.pubkey,
            instructions=instructions,
            address_lookup_table_accounts=[],
            recent_blockhash=blockhash,
        )
        return VersionedTransaction(message, [self.keypair]), associated_user

    async def execute(self, event: LaunchEvent) -> BuyOutcome:
        """The hot path: quote, build, sign, send.

        Timings are captured per stage in microseconds. `total_us` is measured
        from entry to the moment the request body hits the socket; if the
        listener stamped `received_ns`, `since_event_us` covers the whole
        pipeline from the frame arriving to the transaction going out.
        """
        t0 = time.perf_counter_ns()

        blockhash = self.blockhash.blockhash
        if blockhash is None:
            return BuyOutcome(
                event=event,
                quote=quote_buy(self._initial_curve, self.cfg.amount_lamports, self.cfg.slippage_bps),
                dry_run=self.dry_run,
                send_error="no cached blockhash available",
            )

        quote = self.quote(event)
        t_quote = time.perf_counter_ns()

        transaction, associated_user = self.build_transaction(event, quote, blockhash)
        t_build = time.perf_counter_ns()

        body = self.rpc.build_send_body(bytes(transaction))
        t_serialize = time.perf_counter_ns()

        timings = {
            "quote_us": (t_quote - t0) // 1000,
            "build_sign_us": (t_build - t_quote) // 1000,
            "serialize_us": (t_serialize - t_build) // 1000,
            "total_us": (t_serialize - t0) // 1000,
        }
        if event.received_ns:
            timings["since_event_us"] = (t_serialize - event.received_ns) // 1000

        if self.dry_run:
            log.warning(
                "dry_run_buy",
                extra={
                    "mint": str(event.mint),
                    "symbol": event.symbol,
                    "would_send_bytes": len(bytes(transaction)),
                    "signature_unsent": str(transaction.signatures[0]),
                    "blockhash_age_s": round(self.blockhash.age_s, 2),
                    **timings,
                },
            )
            return BuyOutcome(
                event=event,
                quote=quote,
                dry_run=True,
                associated_user=associated_user,
                timings_us=timings,
            )

        raw = bytes(transaction)
        use_jito = self.jito is not None and self.jito_cfg is not None
        send_rpc = not use_jito or (
            self.jito_cfg is not None and self.jito_cfg.also_send_rpc
        )

        # Bundle and ordinary broadcast go out together, not in sequence — the
        # bundle is the fast path, the RPC send is the fallback if it is not
        # selected, and serialising them would give away the difference.
        bundle_task = (
            asyncio.create_task(self.jito.send_bundle([raw])) if use_jito else None
        )
        rpc_task = (
            asyncio.create_task(self.rpc.send_transaction(body)) if send_rpc else None
        )

        bundle_id = bundle_error = None
        if bundle_task is not None:
            bundle = await bundle_task
            bundle_id, bundle_error = bundle.bundle_id, bundle.error

        if rpc_task is not None:
            winner, all_results = await rpc_task
            self._log_send(winner, all_results)
        else:
            winner = SendResult(
                signature=str(transaction.signatures[0]) if bundle_id else None,
                endpoint=self.jito_cfg.block_engine_url if self.jito_cfg else "",
                error=bundle_error,
            )

        t_sent = time.perf_counter_ns()
        timings["send_us"] = (t_sent - t_serialize) // 1000
        timings["total_with_send_us"] = (t_sent - t0) // 1000

        return BuyOutcome(
            event=event,
            quote=quote,
            dry_run=False,
            signature=winner.signature,
            send_error=winner.error,
            endpoint=winner.endpoint,
            associated_user=associated_user,
            timings_us=timings,
            bundle_id=bundle_id,
            bundle_error=bundle_error,
        )

    def _log_send(self, winner: SendResult, results: list[SendResult]) -> None:
        for result in results:
            if result is winner:
                continue
            log.debug(
                "send_endpoint_result",
                extra={
                    "endpoint": result.endpoint,
                    "signature": result.signature,
                    "error": result.error,
                },
            )

    async def confirm(self, outcome: BuyOutcome) -> dict:
        """Poll for confirmation and report what actually filled.

        Runs after the send, off the hot path. The fill is read from the token
        account balance rather than inferred from the quote, so the log records
        what was received and not what was hoped for.
        """
        if not outcome.signature:
            return {"status": "not_sent"}

        deadline = time.monotonic() + self.safety_cfg.confirm_timeout_s
        status: Optional[dict] = None
        while time.monotonic() < deadline:
            try:
                status = await self.rpc.get_signature_status(outcome.signature)
            except Exception as exc:
                log.debug("confirm_poll_error", extra={"error": repr(exc)})
                status = None
            if status is not None:
                break
            await asyncio.sleep(0.25)

        if status is None:
            return {"status": "timeout", "signature": outcome.signature}

        if status.get("err") is not None:
            return {
                "status": "failed",
                "signature": outcome.signature,
                "err": status["err"],
                "slot": status.get("slot"),
            }

        filled: Optional[int] = None
        if outcome.associated_user is not None:
            filled = await self.rpc.get_token_account_balance(str(outcome.associated_user))

        return {
            "status": "confirmed",
            "signature": outcome.signature,
            "slot": status.get("slot"),
            "confirmation_status": status.get("confirmationStatus"),
            "tokens_received": filled,
            "tokens_received_ui": (
                round(filled / 10**PUMP_TOKEN_DECIMALS, 6) if filled is not None else None
            ),
        }

    async def close(self) -> None:
        await self.blockhash.stop()

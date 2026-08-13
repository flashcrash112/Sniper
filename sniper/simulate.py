"""Simulating a real buy against mainnet, without spending anything.

`verify-layout` compares our encoding to other people's transactions. This goes
one step further and asks the cluster to actually execute ours.

That matters because of a specific gap the IDL cannot close. pump.fun's
published IDL declares 16 accounts for `buy`, but live transactions pass 18.
Anchor permits undeclared trailing accounts, so those two extras may be inert —
or the program may read `ctx.remaining_accounts` and require them. Reading the
IDL cannot tell you which; running the instruction can.

`simulateTransaction` runs it against real chain state and returns the program
logs and error, with no signature required and nothing broadcast. It is the
closest thing to a live buy that costs nothing, and it is the last check worth
doing before risking a priority fee.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Optional

from solders.hash import Hash
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from .buyer import Buyer
from .constants import PUMP_FUN_PROGRAM, TOKEN_PROGRAMS
from .curve import CurveState
from .logging_setup import get_logger
from .pumpfun import LaunchEvent, derive_bonding_curve, parse_bonding_curve
from .rpc import RpcPool

log = get_logger("sniper.simulate")


@dataclass(frozen=True, slots=True)
class SimulationResult:
    mint: Pubkey
    token_program: Optional[Pubkey] = None
    payer: Optional[Pubkey] = None
    err: Optional[object] = None
    logs: list[str] = field(default_factory=list)
    units_consumed: Optional[int] = None
    build_error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.build_error is None and self.err is None

    @property
    def token_program_name(self) -> str:
        return TOKEN_PROGRAMS.get(self.token_program, "unknown")

    def verdict(self) -> str:
        """Translate the raw failure into what it means for the account list.

        A simulation from an unfunded wallet is *expected* to fail on lamports,
        and that failure is good news: reaching the point of moving money means
        every account passed validation. Distinguishing that from a structural
        rejection is the whole point of running this.
        """
        if self.build_error:
            return f"could not build the transaction: {self.build_error}"
        if self.err is None:
            return "SUCCESS — the buy executed against real chain state"

        blob = " ".join(self.logs).lower() + " " + str(self.err).lower()

        if "accountnotfound" in blob.replace(" ", "") and not self.logs:
            return (
                "INCONCLUSIVE — the fee payer account does not exist on chain, so "
                "nothing ran.\n"
                "    A wallet that has never received SOL is not merely empty, it "
                "is absent, and\n"
                "    Solana rejects it as fee payer before executing anything "
                "(note: 0 compute units).\n"
                "    Re-run with --payer <a funded address> to test the "
                "instruction itself."
            )

        structural = {
            "accountnotenoughkeys": "the program wanted more accounts than we sent",
            "notenoughaccountkeys": "the program wanted more accounts than we sent",
            "constraintseeds": "a PDA we derived does not match what the program expects",
            "accountownedbywrongprogram": "an account we passed is owned by the wrong program",
            "instructiondidnotdeserialize": "our instruction data does not match the program's args",
            "invalidprogramid": "we invoked the wrong program",
            "accountnotinitialized": "an account we passed does not exist yet",
        }
        for needle, meaning in structural.items():
            if needle in blob.replace("_", "").replace(" ", ""):
                return f"STRUCTURAL FAILURE — {meaning}"

        funding = ("insufficient lamports", "insufficient funds", "0x1 ", "debit an account")
        if any(needle in blob for needle in funding):
            return (
                "accounts VALIDATED — failed only on funding, which is expected "
                "from an empty wallet.\n"
                "    Everything structural passed: the account list, the PDAs and "
                "the instruction data are all accepted by the live program."
            )
        return f"failed for a non-structural reason: {self.err}"


async def resolve_mint(rpc: RpcPool, mint: Pubkey) -> tuple[Optional[Pubkey], Optional[LaunchEvent]]:
    """Read a live coin's token program and curve state.

    Returns (token_program, event). The token program comes from the mint
    account's owner, which is authoritative — it is exactly what the runtime
    will check account 8 against.
    """
    accounts = await rpc.get_multiple_accounts(
        [str(mint), str(derive_bonding_curve(mint))]
    )
    mint_account, curve_account = (accounts + [None, None])[:2]

    token_program = None
    if mint_account and mint_account.get("owner"):
        token_program = Pubkey.from_string(mint_account["owner"])

    if not curve_account:
        return token_program, None

    raw = base64.b64decode(curve_account["data"][0])
    state = parse_bonding_curve(raw)
    if state.complete:
        log.warning("curve_complete", extra={"mint": str(mint)})

    event = LaunchEvent(
        mint=mint,
        name="(simulated)",
        symbol="(simulated)",
        uri="",
        # The curve account carries the creator on current program versions;
        # without it creator_vault cannot be derived correctly.
        creator=state.creator or mint,
        bonding_curve=derive_bonding_curve(mint),
        curve=CurveState(
            virtual_token_reserves=state.virtual_token_reserves,
            virtual_sol_reserves=state.virtual_sol_reserves,
            real_token_reserves=state.real_token_reserves,
        ),
        token_program=token_program,
    )
    return token_program, event


async def find_recent_mint(rpc: RpcPool, scan_limit: int = 60) -> Optional[Pubkey]:
    """Pick a live pump.fun coin to simulate against, from recent buys."""
    from .verify import _account_keys, _find_buy_instruction

    signatures = await rpc.call(
        "getSignaturesForAddress",
        [str(PUMP_FUN_PROGRAM), {"limit": scan_limit, "commitment": "confirmed"}],
    )
    for entry in signatures or []:
        if entry.get("err") is not None:
            continue
        try:
            transaction = await rpc.call(
                "getTransaction",
                [
                    entry["signature"],
                    {
                        "encoding": "json",
                        "commitment": "confirmed",
                        "maxSupportedTransactionVersion": 0,
                    },
                ],
            )
        except Exception:
            continue
        if not transaction:
            continue
        found = _find_buy_instruction(transaction)
        if found is None:
            continue
        indices, _ = found
        keys = _account_keys(transaction)
        if len(indices) > 2 and indices[2] < len(keys):
            return keys[indices[2]]
    return None


def build_unsigned_for_simulation(
    buyer: Buyer,
    event: LaunchEvent,
    quote,
    blockhash: Hash,
    payer: Pubkey,
    token_program: Optional[Pubkey] = None,
) -> VersionedTransaction:
    """Compile the buy for an arbitrary payer, with a placeholder signature.

    Simulation runs with `sigVerify: false`, so a real signature is not needed
    — which means we can simulate as an address whose key we do not hold. That
    is what makes it possible to test the instruction with a funded account
    when our own wallet has never been funded.
    """
    instructions = buyer.build_instructions(
        event, quote, payer=payer, token_program=token_program
    )
    message = MessageV0.try_compile(
        payer=payer,
        instructions=instructions,
        address_lookup_table_accounts=[],
        recent_blockhash=blockhash,
    )
    return VersionedTransaction.populate(message, [Signature.default()])


async def find_recent_buyer(rpc: RpcPool, scan_limit: int = 60):
    """A (mint, buyer) pair from a recent successful buy.

    The buyer is guaranteed to exist and hold SOL — they just paid for a
    transaction — which makes them a usable stand-in payer for a simulation
    that only needs the account structure to be exercised.
    """
    from .verify import _account_keys, _find_buy_instruction

    signatures = await rpc.call(
        "getSignaturesForAddress",
        [str(PUMP_FUN_PROGRAM), {"limit": scan_limit, "commitment": "confirmed"}],
    )
    for entry in signatures or []:
        if entry.get("err") is not None:
            continue
        try:
            transaction = await rpc.call(
                "getTransaction",
                [
                    entry["signature"],
                    {
                        "encoding": "json",
                        "commitment": "confirmed",
                        "maxSupportedTransactionVersion": 0,
                    },
                ],
            )
        except Exception:
            continue
        if not transaction:
            continue
        found = _find_buy_instruction(transaction)
        if found is None:
            continue
        indices, _ = found
        keys = _account_keys(transaction)
        if len(indices) > 6 and max(indices[2], indices[6]) < len(keys):
            return keys[indices[2]], keys[indices[6]]
    return None, None


async def simulate_buy(
    rpc: RpcPool,
    buyer: Buyer,
    mint: Pubkey,
    token_program: Optional[Pubkey] = None,
    payer: Optional[Pubkey] = None,
) -> SimulationResult:
    """Build our real buy for `mint` and simulate it against mainnet.

    `payer` overrides who pays, for the case where our own wallet does not
    exist on chain yet. The instruction is identical either way.
    """
    resolved_token_program, event = await resolve_mint(rpc, mint)
    token_program = token_program or resolved_token_program

    if event is None:
        return SimulationResult(
            mint=mint,
            token_program=token_program,
            build_error="no bonding curve account — is this a pump.fun mint?",
        )

    blockhash = buyer.blockhash.blockhash or Hash.default()
    try:
        quote = buyer.quote(event)
        if payer is None or payer == buyer.pubkey:
            transaction, _ = buyer.build_transaction(
                event, quote, blockhash, token_program=token_program
            )
        else:
            transaction = build_unsigned_for_simulation(
                buyer, event, quote, blockhash, payer, token_program
            )
    except Exception as exc:
        return SimulationResult(
            mint=mint, token_program=token_program, build_error=repr(exc)
        )

    result = await rpc.call(
        "simulateTransaction",
        [
            base64.b64encode(bytes(transaction)).decode("ascii"),
            {
                "encoding": "base64",
                # Unsigned is fine: we are testing the instruction, not the
                # signature, and replacing the blockhash avoids "blockhash not
                # found" masking the real result.
                "sigVerify": False,
                "replaceRecentBlockhash": True,
                "commitment": "processed",
            },
        ],
    )
    value = (result or {}).get("value") or {}
    return SimulationResult(
        mint=mint,
        token_program=token_program,
        payer=payer or buyer.pubkey,
        err=value.get("err"),
        logs=value.get("logs") or [],
        units_consumed=value.get("unitsConsumed"),
    )

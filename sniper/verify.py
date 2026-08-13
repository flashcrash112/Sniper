"""Verify our encoded `buy` against a real one from mainnet.

The problem this solves: pump.fun is upgradeable, its `buy` account list has
grown several times, and no unit test can tell you whether the layout in
`build_buy_instruction` still matches the deployed program. The usual way to
find out is a failed transaction, which costs a priority fee and a launch.

Instead, pull a recent *successful* buy off the chain — one that somebody else
paid for — decode it, and check it position by position against what we would
have built. Two read-only RPC calls, no signing, no spending.

What it actually proves: the discriminator is right, the account count is
right, and every account whose value we can derive independently (the PDAs,
the program IDs, the ATAs) sits at the index we put it at. That is the entire
failure surface of a layout drift.
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
from typing import Optional, Sequence

import base58
from solders.pubkey import Pubkey

from .constants import (
    BUY_IX_DISCRIMINATOR,
    EVENT_AUTHORITY,
    FEE_CONFIG,
    GLOBAL_ACCOUNT,
    GLOBAL_VOLUME_ACCUMULATOR,
    PUMP_FUN_FEE_PROGRAM,
    PUMP_FUN_PROGRAM,
    SYSTEM_PROGRAM,
    TOKEN_PROGRAM,
)
from .logging_setup import get_logger
from .pumpfun import (
    BuyLayout,
    derive_associated_token_account,
    derive_bonding_curve,
    derive_user_volume_accumulator,
)
from .rpc import RpcPool

log = get_logger("sniper.verify")

LAYOUT_ACCOUNT_COUNTS = {BuyLayout.CURRENT: 16, BuyLayout.LEGACY: 12}

TOKEN_2022_PROGRAM = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")

# Both are valid token programs for a pump.fun mint. Which one a given mint
# uses changes the associated-token-account addresses, because the token
# program ID is one of the ATA derivation seeds — so a Token-2022 mint makes a
# legacy-derived ATA wrong, and vice versa.
KNOWN_TOKEN_PROGRAMS = {
    TOKEN_PROGRAM: "SPL Token (legacy)",
    TOKEN_2022_PROGRAM: "Token-2022",
}


@dataclass(frozen=True, slots=True)
class SlotCheck:
    """One account position, as observed on-chain versus as we would encode it."""

    index: int
    role: str
    observed: Pubkey
    expected: Optional[Pubkey]
    ok: Optional[bool]
    """True/False if we could derive the account independently, None if not."""

    @property
    def status(self) -> str:
        if self.ok is None:
            return "?"
        return "ok" if self.ok else "MISMATCH"


@dataclass(frozen=True, slots=True)
class LayoutReport:
    signature: str
    slot: Optional[int]
    account_count: int
    data_len: int
    detected_layout: Optional[BuyLayout]
    configured_layout: BuyLayout
    checks: list[SlotCheck] = field(default_factory=list)
    mint: Optional[Pubkey] = None
    fee_recipient: Optional[Pubkey] = None
    token_program: Optional[Pubkey] = None
    notes: list[str] = field(default_factory=list)

    @property
    def token_program_name(self) -> str:
        return KNOWN_TOKEN_PROGRAMS.get(self.token_program, "UNKNOWN")

    @property
    def shape(self) -> str:
        """A short key for grouping samples that agree with each other."""
        layout = self.detected_layout.value if self.detected_layout else "unknown"
        return f"{self.account_count} accounts / {self.token_program_name} / {layout}"

    @property
    def mismatches(self) -> list[SlotCheck]:
        return [c for c in self.checks if c.ok is False]

    @property
    def ok(self) -> bool:
        return (
            self.detected_layout is not None
            and self.detected_layout == self.configured_layout
            and not self.mismatches
        )


# --- Fetching a reference transaction --------------------------------------


@dataclass(frozen=True, slots=True)
class ScanStats:
    """How the sample gathering actually went, so a thin result is explicable."""

    signatures_seen: int = 0
    transactions_fetched: int = 0
    buys_found: int = 0
    fetch_errors: int = 0


async def fetch_recent_buys(
    rpc: RpcPool, scan_limit: int = 100, want: int = 8, concurrency: int = 4
) -> tuple[list[dict], ScanStats]:
    """Find recent successful pump.fun buys and return their transactions.

    Samples widely rather than trusting one transaction: pump.fun can serve
    several instruction shapes at once (a legacy-SPL mint and a Token-2022 mint
    take different account lists), so a single sample tells you that a shape
    exists, not that it is the only one.

    Fetches concurrently but with a small cap, and retries once on failure —
    a free-tier RPC key will rate-limit a burst of `getTransaction` calls, and
    the resulting empty result would otherwise look like "no buys are
    happening" rather than "you got throttled".
    """
    signatures = await rpc.call(
        "getSignaturesForAddress",
        [str(PUMP_FUN_PROGRAM), {"limit": scan_limit, "commitment": "confirmed"}],
    )
    candidates = [
        entry["signature"] for entry in (signatures or []) if entry.get("err") is None
    ]
    stats = {"fetched": 0, "errors": 0}
    found: list[dict] = []
    semaphore = asyncio.Semaphore(concurrency)
    done = asyncio.Event()

    async def fetch(signature: str) -> None:
        if done.is_set():
            return
        async with semaphore:
            if done.is_set():
                return
            for attempt in range(2):
                try:
                    transaction = await rpc.call(
                        "getTransaction",
                        [
                            signature,
                            {
                                "encoding": "json",
                                "commitment": "confirmed",
                                "maxSupportedTransactionVersion": 0,
                            },
                        ],
                    )
                    break
                except Exception:
                    if attempt == 0:
                        # Almost always a rate limit; back off and try once more.
                        await asyncio.sleep(0.5)
                        continue
                    stats["errors"] += 1
                    return
            stats["fetched"] += 1
            if transaction and _find_buy_instruction(transaction) is not None:
                found.append(transaction)
                if len(found) >= want:
                    done.set()

    await asyncio.gather(*(fetch(s) for s in candidates), return_exceptions=True)

    return found[:want], ScanStats(
        signatures_seen=len(candidates),
        transactions_fetched=stats["fetched"],
        buys_found=len(found),
        fetch_errors=stats["errors"],
    )


def _account_keys(transaction: dict) -> list[Pubkey]:
    """Static keys plus anything resolved from address lookup tables."""
    message = transaction["transaction"]["message"]
    keys = [Pubkey.from_string(k) for k in message["accountKeys"]]
    loaded = (transaction.get("meta") or {}).get("loadedAddresses") or {}
    keys += [Pubkey.from_string(k) for k in loaded.get("writable", [])]
    keys += [Pubkey.from_string(k) for k in loaded.get("readonly", [])]
    return keys


def _decode_ix_data(raw: str) -> bytes:
    """Instruction data is base58 under `encoding: json`."""
    try:
        return base58.b58decode(raw)
    except Exception:
        try:
            return base64.b64decode(raw)
        except Exception:
            return b""


def _find_buy_instruction(transaction: dict) -> Optional[tuple[list[int], bytes]]:
    """Locate the pump.fun buy among a transaction's instructions."""
    keys = _account_keys(transaction)
    message = transaction["transaction"]["message"]
    meta = transaction.get("meta") or {}

    groups: list[list[dict]] = [message.get("instructions") or []]
    for inner in meta.get("innerInstructions") or []:
        groups.append(inner.get("instructions") or [])

    for group in groups:
        for ix in group:
            index = ix.get("programIdIndex")
            if index is None or index >= len(keys):
                continue
            if keys[index] != PUMP_FUN_PROGRAM:
                continue
            data = _decode_ix_data(ix.get("data", ""))
            if data[:8] == BUY_IX_DISCRIMINATOR:
                return ix.get("accounts") or [], data
    return None


# --- Comparison ------------------------------------------------------------

# The roles we expect at each index, in the order `build_buy_instruction`
# emits them. Positions 12-15 exist only in the current layout.
_ROLES = [
    "global",
    "fee_recipient",
    "mint",
    "bonding_curve",
    "associated_bonding_curve",
    "associated_user",
    "user",
    "system_program",
    "token_program",
    "creator_vault",
    "event_authority",
    "program",
    "global_volume_accumulator",
    "user_volume_accumulator",
    "fee_config",
    "fee_program",
]


def compare_against_observed(
    observed: Sequence[Pubkey],
    data: bytes,
    configured_layout: BuyLayout,
    signature: str = "",
    slot: Optional[int] = None,
) -> LayoutReport:
    """Check an on-chain buy's accounts against what we would have encoded.

    The mint and the buyer come from the observed transaction; everything
    derivable from them is re-derived here and compared. An account we cannot
    derive independently (the fee recipient, which is whatever the global
    account currently says) is recorded but not judged.
    """
    count = len(observed)
    detected = next(
        (layout for layout, n in LAYOUT_ACCOUNT_COUNTS.items() if n == count), None
    )
    notes: list[str] = []
    if detected is None:
        notes.append(
            f"observed {count} accounts, which matches neither the current "
            f"({LAYOUT_ACCOUNT_COUNTS[BuyLayout.CURRENT]}) nor the legacy "
            f"({LAYOUT_ACCOUNT_COUNTS[BuyLayout.LEGACY]}) layout — the program "
            f"has changed and sniper/pumpfun.py needs updating"
        )
    elif detected != configured_layout:
        notes.append(
            f"chain is using the {detected.value!r} layout but [buy] layout is "
            f"set to {configured_layout.value!r} — change it"
        )

    if len(observed) < 7:
        return LayoutReport(
            signature=signature,
            slot=slot,
            account_count=count,
            data_len=len(data),
            detected_layout=detected,
            configured_layout=configured_layout,
            notes=notes + ["too few accounts to identify the mint and buyer"],
        )

    mint = observed[2]
    user = observed[6]

    # Read the token program out of the transaction rather than assuming the
    # legacy one. It is an ATA seed, so assuming it would report three
    # mismatches (token_program and both ATAs) for what is really one fact.
    token_program = observed[8] if len(observed) > 8 else TOKEN_PROGRAM
    if token_program not in KNOWN_TOKEN_PROGRAMS:
        notes.append(
            f"account 8 is {token_program}, which is neither SPL Token nor "
            f"Token-2022 — the account order has changed, so the positions "
            f"below are being compared against the wrong roles"
        )
        token_program = TOKEN_PROGRAM
    elif token_program == TOKEN_2022_PROGRAM:
        notes.append(
            "this mint uses Token-2022, so its associated token accounts are "
            "derived with the Token-2022 program ID as a seed"
        )

    bonding_curve = derive_bonding_curve(mint)
    # Everything we can independently derive from the mint and the buyer.
    expectations: dict[int, Pubkey] = {
        0: GLOBAL_ACCOUNT,
        3: bonding_curve,
        4: derive_associated_token_account(bonding_curve, mint, token_program),
        5: derive_associated_token_account(user, mint, token_program),
        7: SYSTEM_PROGRAM,
        10: EVENT_AUTHORITY,
        11: PUMP_FUN_PROGRAM,
    }
    if count > 12:
        expectations[12] = GLOBAL_VOLUME_ACCUMULATOR
        expectations[13] = derive_user_volume_accumulator(user)
        expectations[14] = FEE_CONFIG
        expectations[15] = PUMP_FUN_FEE_PROGRAM

    checks = []
    for index, account in enumerate(observed):
        role = _ROLES[index] if index < len(_ROLES) else f"unknown[{index}]"
        if index == 8:
            # Not compared against a fixed value — either token program is
            # legitimate — but it must be *a* token program. That still catches
            # an account-order change that lands something else here.
            checks.append(
                SlotCheck(
                    index=index,
                    role=role,
                    observed=account,
                    expected=None,
                    ok=account in KNOWN_TOKEN_PROGRAMS,
                )
            )
            continue
        expected = expectations.get(index)
        checks.append(
            SlotCheck(
                index=index,
                role=role,
                observed=account,
                expected=expected,
                ok=None if expected is None else expected == account,
            )
        )

    expected_data_len = 24 if detected is BuyLayout.LEGACY else 25
    if detected is not None and len(data) not in (24, 25):
        notes.append(
            f"buy instruction data is {len(data)} bytes; expected 24 (legacy) "
            f"or 25 (current, with the track_volume Option<bool>)"
        )
    elif detected is BuyLayout.CURRENT and len(data) != expected_data_len:
        notes.append(
            f"current layout observed with {len(data)}-byte data; we encode "
            f"{expected_data_len}"
        )

    # Distinguish "reordered" from "extended". If every position we know about
    # still matches and there are simply more accounts on the end, the fix is
    # additive — identify the extras — rather than a rewrite of the account
    # list. That is a much smaller and less dangerous change, so say so.
    judged = [c for c in checks if c.ok is not None]
    if (
        detected is None
        and count > LAYOUT_ACCOUNT_COUNTS[BuyLayout.CURRENT]
        and judged
        and all(c.ok for c in judged)
    ):
        extra = count - LAYOUT_ACCOUNT_COUNTS[BuyLayout.CURRENT]
        notes.append(
            f"all {LAYOUT_ACCOUNT_COUNTS[BuyLayout.CURRENT]} accounts we encode "
            f"match exactly, in order — the layout was EXTENDED by {extra} "
            f"account(s), not reordered. Identify accounts "
            f"{LAYOUT_ACCOUNT_COUNTS[BuyLayout.CURRENT]}-{count - 1} and append "
            f"them in build_buy_instruction"
        )

    return LayoutReport(
        signature=signature,
        slot=slot,
        account_count=count,
        data_len=len(data),
        detected_layout=detected,
        configured_layout=configured_layout,
        checks=checks,
        mint=mint,
        fee_recipient=observed[1],
        token_program=token_program,
        notes=notes,
    )


async def verify_layout(
    rpc: RpcPool, configured_layout: BuyLayout, samples: int = 8
) -> tuple[list[LayoutReport], ScanStats]:
    """Fetch recent mainnet buys and check each against our encoding."""
    transactions, stats = await fetch_recent_buys(rpc, want=samples)
    if not transactions:
        raise RuntimeError(
            f"found no recent successful pump.fun buys to compare against.\n"
            f"  scanned {stats.signatures_seen} signatures, fetched "
            f"{stats.transactions_fetched}, {stats.fetch_errors} errors.\n"
            f"  A high error count usually means the RPC key is being rate "
            f"limited — wait a moment and re-run, or use a paid endpoint."
        )

    reports = []
    for transaction in transactions:
        found = _find_buy_instruction(transaction)
        if found is None:
            continue
        indices, data = found
        keys = _account_keys(transaction)
        observed = [keys[i] for i in indices if 0 <= i < len(keys)]
        reports.append(
            compare_against_observed(
                observed,
                data,
                configured_layout,
                signature=(transaction.get("transaction", {}).get("signatures") or [""])[0],
                slot=transaction.get("slot"),
            )
        )
    return reports, stats


def summarise(reports: Sequence[LayoutReport]) -> str:
    """Group samples by shape.

    The distribution is the useful part: one disagreeing sample means a shape
    exists, while every sample agreeing means the program moved. pump.fun can
    serve more than one shape at a time, so a histogram answers a question a
    single sample cannot.
    """
    counts: dict[str, int] = {}
    passing: dict[str, int] = {}
    for report in reports:
        counts[report.shape] = counts.get(report.shape, 0) + 1
        if report.ok:
            passing[report.shape] = passing.get(report.shape, 0) + 1

    lines = ["Shapes observed across samples:", ""]
    for shape, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        good = passing.get(shape, 0)
        verdict = "matches our encoding" if good == count else "does NOT match"
        lines.append(f"  {count:>2} x  {shape:<48} {verdict}")
    return "\n".join(lines)


def format_report(report: LayoutReport) -> str:
    """Render a report as a readable table."""
    lines = [
        f"  signature      {report.signature}",
        f"  slot           {report.slot}",
        f"  accounts       {report.account_count}"
        f"  (detected layout: {report.detected_layout.value if report.detected_layout else 'UNKNOWN'})",
        f"  data           {report.data_len} bytes",
        "",
        f"  {'#':>2}  {'role':<28} {'status':<9} account",
        f"  {'-' * 2}  {'-' * 28} {'-' * 9} {'-' * 44}",
    ]
    for check in report.checks:
        lines.append(
            f"  {check.index:>2}  {check.role:<28} {check.status:<9} {check.observed}"
        )
        if check.ok is False:
            lines.append(f"      {'':<28} {'':<9} expected {check.expected}")
    for note in report.notes:
        lines.append(f"\n  ! {note}")
    return "\n".join(lines)

"""Fetching an Anchor program's IDL from the chain.

When the account list changes, the choice is between reverse-engineering it
from observed transactions or reading the program's own description of itself.
Anchor programs can publish their IDL on chain, and pump.fun is an Anchor
program — so when it is published this is the authoritative answer: exact
account names, exact order, no inference.

The IDL lives at a deterministic address::

    base = find_program_address([], program_id)
    idl  = create_with_seed(base, "anchor:idl", program_id)

and the account holds::

    8   discriminator
    32  authority
    4   u32 length
    N   zlib-compressed JSON

Publishing is optional, so this can legitimately come back empty. That is a
"not available" answer, not a failure — :func:`fetch_idl` returns None and the
caller falls back to the transaction-sampling route in `verify.py`.
"""

from __future__ import annotations

import json
import struct
import zlib
from typing import Any, Optional

from solders.pubkey import Pubkey

from .constants import PUMP_FUN_FEE_PROGRAM, PUMP_FUN_PROGRAM
from .logging_setup import get_logger
from .rpc import RpcPool

log = get_logger("sniper.idl")

IDL_SEED = "anchor:idl"


def idl_address(program_id: Pubkey) -> Pubkey:
    """The deterministic address an Anchor program publishes its IDL at."""
    base = Pubkey.find_program_address([], program_id)[0]
    return Pubkey.create_with_seed(base, IDL_SEED, program_id)


def decode_idl_account(data: bytes) -> Optional[dict]:
    """Decode an Anchor IDL account's contents.

    Returns None rather than raising if the payload is not a well-formed IDL —
    an address collision or a differently-versioned account should read as
    "no IDL here", not as a crash.
    """
    if len(data) < 44:
        return None
    length = struct.unpack_from("<I", data, 40)[0]
    if length == 0 or 44 + length > len(data):
        # Some programs write the full remaining buffer; try it anyway.
        payload = data[44:]
    else:
        payload = data[44 : 44 + length]

    for candidate in (payload, data[44:]):
        try:
            return json.loads(zlib.decompress(candidate))
        except Exception:
            continue
    return None


async def fetch_idl(rpc: RpcPool, program_id: Pubkey = PUMP_FUN_PROGRAM) -> Optional[dict]:
    """Fetch and decode a program's on-chain IDL, or None if not published."""
    address = idl_address(program_id)
    try:
        data = await rpc.get_account_data(str(address))
    except Exception as exc:
        log.warning("idl_fetch_failed", extra={"error": repr(exc)})
        return None
    if not data:
        return None
    return decode_idl_account(data)


def instruction_accounts(idl: dict, name: str) -> Optional[list[str]]:
    """The ordered account names for one instruction, flattening nested groups."""
    for instruction in idl.get("instructions", []):
        if instruction.get("name", "").lower() != name.lower():
            continue

        names: list[str] = []

        def walk(accounts: list[dict]) -> None:
            for account in accounts:
                # Anchor allows nested account structs; they flatten in order.
                if "accounts" in account:
                    walk(account["accounts"])
                else:
                    names.append(account.get("name", "?"))

        walk(instruction.get("accounts", []))
        return names
    return None


def instruction_args(idl: dict, name: str) -> Optional[list[tuple[str, Any]]]:
    """The argument names and types for one instruction."""
    for instruction in idl.get("instructions", []):
        if instruction.get("name", "").lower() == name.lower():
            return [
                (arg.get("name", "?"), arg.get("type")) for arg in instruction.get("args", [])
            ]
    return None


def find_error(idl: dict, code: int) -> Optional[dict]:
    """Look up a custom program error by its numeric code.

    Anchor assigns custom errors starting at 6000, in declaration order, and
    publishes the mapping in the IDL's `errors` array as
    `{"code": 6000, "name": "...", "msg": "..."}`. A `Custom(N)` value out of
    `simulateTransaction` or a failed `sendTransaction` is otherwise just a
    number — this turns it back into the name and message the program author
    wrote, straight from the program's own description of itself rather than
    a guess at what a given code "usually" means.
    """
    for error in idl.get("errors", []):
        if error.get("code") == code:
            return error
    return None


def format_error(code: int, error: Optional[dict]) -> str:
    if error is None:
        return (
            f"error {code} is not in this program's IDL error list.\n"
            f"    Either it is a framework-level Anchor error (account "
            f"constraints, discriminator mismatches — not one the program "
            f"declared itself), or the deployed program has moved on from "
            f"the IDL version currently published."
        )
    name = error.get("name", "?")
    msg = error.get("msg", "(no message)")
    return f"{code}  {name}\n    {msg}"


def summarise_idl(idl: dict) -> str:
    """Render the parts of an IDL this bot depends on."""
    lines = []
    version = idl.get("version") or (idl.get("metadata") or {}).get("version")
    name = idl.get("name") or (idl.get("metadata") or {}).get("name")
    lines.append(f"program        {name or '(unnamed)'}  version {version or '?'}")
    lines.append(f"instructions   {len(idl.get('instructions', []))}")
    lines.append("")

    for target in ("buy", "sell"):
        accounts = instruction_accounts(idl, target)
        if accounts is None:
            lines.append(f"{target}: NOT FOUND in this IDL")
            continue
        args = instruction_args(idl, target) or []
        lines.append(f"{target} — {len(accounts)} accounts, {len(args)} args")
        for index, account_name in enumerate(accounts):
            lines.append(f"  {index:>2}  {account_name}")
        if args:
            rendered = ", ".join(f"{n}: {t}" for n, t in args)
            lines.append(f"  args: {rendered}")
        lines.append("")
    return "\n".join(lines)


PROGRAMS = {
    "pump": PUMP_FUN_PROGRAM,
    "fee": PUMP_FUN_FEE_PROGRAM,
}

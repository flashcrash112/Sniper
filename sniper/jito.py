"""Jito bundle submission.

A normal `sendTransaction` puts you in the same queue as everyone else and
lets the priority fee decide. A Jito bundle instead pays a validator directly
to include your transaction, and the block engine forwards it to the leader
over its own path. On a contested launch this matters more than any amount of
software tuning.

Mechanically: append a SOL transfer to one of Jito's tip accounts to the same
transaction, then submit it as a single-transaction bundle to the block engine
instead of to an RPC node.

Two things worth knowing before enabling it:

* **The tip is paid whether or not you win**, exactly like a priority fee.
* **A bundle either lands whole or not at all.** For a single-transaction
  bundle that is the same as a normal revert, but it means the tip is not
  charged if the bundle is dropped.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

import aiohttp
import base58
from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey

from .constants import SYSTEM_PROGRAM
from .logging_setup import get_logger

log = get_logger("sniper.jito")

# Jito's published mainnet tip accounts. Pick one per bundle; spreading load
# across them is what they are for.
TIP_ACCOUNTS = [
    Pubkey.from_string("96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5"),
    Pubkey.from_string("HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe"),
    Pubkey.from_string("Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY"),
    Pubkey.from_string("ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49"),
    Pubkey.from_string("DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh"),
    Pubkey.from_string("ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt"),
    Pubkey.from_string("DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL"),
    Pubkey.from_string("3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT"),
]

# Bundles below this are rejected outright.
MIN_TIP_LAMPORTS = 1_000

BLOCK_ENGINES = {
    "mainnet": "https://mainnet.block-engine.jito.wtf",
    "amsterdam": "https://amsterdam.mainnet.block-engine.jito.wtf",
    "frankfurt": "https://frankfurt.mainnet.block-engine.jito.wtf",
    "ny": "https://ny.mainnet.block-engine.jito.wtf",
    "tokyo": "https://tokyo.mainnet.block-engine.jito.wtf",
}


def build_tip_instruction(payer: Pubkey, lamports: int, tip_account: Pubkey) -> Instruction:
    """A plain system transfer to a Jito tip account.

    System program instruction 2 is Transfer: a u32 discriminator followed by
    the u64 lamport amount.
    """
    if lamports < MIN_TIP_LAMPORTS:
        raise ValueError(f"Jito tips below {MIN_TIP_LAMPORTS} lamports are rejected")
    return Instruction(
        program_id=SYSTEM_PROGRAM,
        data=(2).to_bytes(4, "little") + lamports.to_bytes(8, "little"),
        accounts=[
            AccountMeta(payer, is_signer=True, is_writable=True),
            AccountMeta(tip_account, is_signer=False, is_writable=True),
        ],
    )


@dataclass(frozen=True, slots=True)
class BundleResult:
    bundle_id: Optional[str]
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.bundle_id is not None


class JitoClient:
    """Submits bundles to a Jito block engine.

    Holds its own warm session for the same reason the RPC pool does: a cold
    TLS handshake on the send path would undo the point of using Jito at all.
    """

    def __init__(self, block_engine_url: str, timeout_s: float = 5.0) -> None:
        self.url = block_engine_url.rstrip("/") + "/api/v1/bundles"
        self.timeout_s = timeout_s
        self._session: Optional[aiohttp.ClientSession] = None
        self._tip_index = 0

    async def __aenter__(self) -> "JitoClient":
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.timeout_s),
            connector=aiohttp.TCPConnector(limit=8, ttl_dns_cache=600, keepalive_timeout=300),
            headers={"content-type": "application/json"},
        )
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.close()

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    def next_tip_account(self) -> Pubkey:
        """Round-robin the tip accounts, as Jito asks clients to."""
        account = TIP_ACCOUNTS[self._tip_index % len(TIP_ACCOUNTS)]
        self._tip_index += 1
        return account

    async def warm(self) -> None:
        """Open the connection ahead of time."""
        if self._session is None:
            return
        try:
            async with self._session.post(
                self.url,
                json={"jsonrpc": "2.0", "id": 1, "method": "getTipAccounts", "params": []},
                timeout=aiohttp.ClientTimeout(total=3),
            ):
                pass
            log.info("jito_warm", extra={"endpoint": self.url})
        except Exception as exc:
            log.warning("jito_warm_failed", extra={"endpoint": self.url, "error": repr(exc)})

    async def send_bundle(self, transactions: Sequence[bytes]) -> BundleResult:
        """Submit signed transactions as one bundle. Never raises."""
        if self._session is None:
            return BundleResult(None, "Jito client used outside its async context")

        encoded = [base58.b58encode(raw).decode("ascii") for raw in transactions]
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendBundle",
            "params": [encoded],
        }
        try:
            async with self._session.post(self.url, json=body) as response:
                status = response.status
                text = await response.text()
        except Exception as exc:
            return BundleResult(None, repr(exc))

        import json

        try:
            payload = json.loads(text)
        except ValueError:
            return BundleResult(None, f"HTTP {status}, non-JSON body: {text[:200]!r}")
        if "error" in payload:
            return BundleResult(None, str(payload["error"]))
        return BundleResult(payload.get("result"))

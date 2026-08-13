"""Minimal async JSON-RPC client, tuned for send latency.

Deliberately not `solana-py`'s `AsyncClient`: this needs to broadcast one
pre-serialised body to several endpoints at once and take the first answer,
which is not what a general-purpose client is shaped for.

The optimisations that matter:

* **Connections are pre-warmed.** TLS to a fresh endpoint costs two round
  trips; doing that after a match would dwarf every other saving in this
  codebase. :meth:`RpcPool.warm` opens them at startup and a keepalive task
  holds them open.
* **The send body is assembled by string concatenation**, not `json.dumps` —
  the envelope is constant and only the base64 payload changes.
* **First response wins.** Endpoints are raced; the rest are cancelled.
"""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass
from typing import Any, Optional, Sequence

import aiohttp

from .logging_setup import get_logger

log = get_logger("sniper.rpc")

# Constant halves of the sendTransaction body. Everything between them is the
# base64 transaction.
_SEND_PREFIX = (
    '{"jsonrpc":"2.0","id":1,"method":"sendTransaction","params":["'
)
_SEND_SUFFIX = (
    '",{"encoding":"base64","skipPreflight":true,"maxRetries":0,'
    '"preflightCommitment":"processed"}]}'
)

_JSON_HEADERS = {"content-type": "application/json"}


class RpcError(Exception):
    """A JSON-RPC error response."""


@dataclass(frozen=True, slots=True)
class SendResult:
    signature: Optional[str]
    endpoint: str
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.signature is not None


class RpcPool:
    """HTTP transport over one query endpoint and N send endpoints."""

    def __init__(
        self,
        http_url: str,
        send_urls: Sequence[str],
        keepalive_interval_s: float = 15.0,
    ) -> None:
        self.http_url = http_url
        self.send_urls = list(send_urls) or [http_url]
        self.keepalive_interval_s = keepalive_interval_s
        self._session: Optional[aiohttp.ClientSession] = None
        self._keepalive: Optional[asyncio.Task] = None
        # Strong references to in-flight broadcasts. asyncio only holds weak
        # references to running tasks, so a losing send could otherwise be
        # garbage-collected mid-flight — silently dropping the extra path to
        # the leader that racing the endpoints was meant to buy us.
        self._inflight: set[asyncio.Task] = set()

    async def __aenter__(self) -> "RpcPool":
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10),
            connector=aiohttp.TCPConnector(
                limit=64,
                ttl_dns_cache=600,
                keepalive_timeout=300,
                # One connection per endpoint is enough and keeps the warm
                # socket the one we actually send on.
                limit_per_host=8,
                force_close=False,
            ),
            headers=_JSON_HEADERS,
        )
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.close()

    async def close(self) -> None:
        if self._keepalive is not None:
            self._keepalive.cancel()
            try:
                await self._keepalive
            except asyncio.CancelledError:
                pass
            self._keepalive = None
        if self._inflight:
            # Give any still-broadcasting duplicates a moment to finish rather
            # than tearing their connections out from under them.
            await asyncio.wait(set(self._inflight), timeout=2)
        if self._session is not None:
            await self._session.close()
            self._session = None

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError("RpcPool used outside its async context")
        return self._session

    async def call(
        self, method: str, params: Any = None, url: Optional[str] = None
    ) -> Any:
        """A normal JSON-RPC call against the query endpoint."""
        body = {"jsonrpc": "2.0", "id": 1, "method": method}
        if params is not None:
            body["params"] = params
        target = url or self.http_url
        async with self.session.post(target, json=body) as response:
            status = response.status
            text = await response.text()

        try:
            payload = json.loads(text)
        except ValueError:
            # Rate-limit pages, proxy errors and Cloudflare interstitials all
            # arrive as HTML. Surfacing the status and a body excerpt is far
            # more useful than a JSONDecodeError traceback.
            raise RpcError(
                f"{method}: {target} returned HTTP {status} with a non-JSON body: "
                f"{text[:200]!r}"
            ) from None

        if not isinstance(payload, dict):
            raise RpcError(f"{method}: unexpected response shape from {target}")
        if "error" in payload:
            raise RpcError(f"{method}: {payload['error']}")
        return payload.get("result")

    async def warm(self) -> None:
        """Open and TLS-handshake every endpoint before trading starts."""
        endpoints = {self.http_url, *self.send_urls}

        async def touch(url: str) -> None:
            try:
                await self.call("getHealth", url=url)
            except Exception as exc:
                log.warning(
                    "endpoint_warm_failed", extra={"endpoint": url, "error": repr(exc)}
                )

        await asyncio.gather(*(touch(url) for url in endpoints))
        self._keepalive = asyncio.create_task(self._keepalive_loop(), name="rpc-keepalive")
        log.info("rpc_warm", extra={"endpoints": sorted(endpoints)})

    async def _keepalive_loop(self) -> None:
        """Hold the warm sockets open so the send never pays for a handshake."""
        endpoints = {self.http_url, *self.send_urls}
        while True:
            await asyncio.sleep(self.keepalive_interval_s)
            for url in endpoints:
                try:
                    await self.call("getHealth", url=url)
                except Exception:
                    # A failed keepalive is not fatal; the next send will
                    # reconnect, and we would rather log nothing every 15s.
                    pass

    def build_send_body(self, transaction_bytes: bytes) -> str:
        """Pre-render the sendTransaction body for a signed transaction."""
        return (
            _SEND_PREFIX
            + base64.b64encode(transaction_bytes).decode("ascii")
            + _SEND_SUFFIX
        )

    async def _send_one(self, url: str, body: str) -> SendResult:
        try:
            async with self.session.post(url, data=body, headers=_JSON_HEADERS) as resp:
                payload = await resp.json(content_type=None)
        except Exception as exc:
            return SendResult(signature=None, endpoint=url, error=repr(exc))
        if "error" in payload:
            return SendResult(signature=None, endpoint=url, error=str(payload["error"]))
        return SendResult(signature=payload.get("result"), endpoint=url)

    async def send_transaction(self, body: str) -> tuple[SendResult, list[SendResult]]:
        """Broadcast to every send endpoint, returning as soon as one accepts.

        Losers keep running in the background rather than being cancelled: a
        duplicate broadcast costs nothing and gives the transaction another
        path to the leader. Their results are collected for the log.
        """
        tasks = [
            asyncio.create_task(self._send_one(url, body)) for url in self.send_urls
        ]
        for task in tasks:
            self._inflight.add(task)
            task.add_done_callback(self._inflight.discard)

        winner: Optional[SendResult] = None
        pending = set(tasks)
        while pending:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                result = task.result()
                if result.ok:
                    winner = result
                    break
            if winner is not None:
                break

        others = [t.result() for t in tasks if t.done()]
        if winner is None:
            # Everything failed; `others` holds every error for the log.
            failed = [r for r in others if not r.ok]
            return (
                failed[0]
                if failed
                else SendResult(None, self.send_urls[0], "no endpoints responded"),
                others,
            )
        return winner, others

    async def get_account_data(self, pubkey: str) -> Optional[bytes]:
        result = await self.call(
            "getAccountInfo", [pubkey, {"encoding": "base64", "commitment": "confirmed"}]
        )
        value = (result or {}).get("value")
        if not value:
            return None
        return base64.b64decode(value["data"][0])

    async def get_latest_blockhash(self, commitment: str = "confirmed") -> tuple[str, int]:
        result = await self.call("getLatestBlockhash", [{"commitment": commitment}])
        value = result["value"]
        return value["blockhash"], value["lastValidBlockHeight"]

    async def get_signature_status(self, signature: str) -> Optional[dict]:
        result = await self.call(
            "getSignatureStatuses", [[signature], {"searchTransactionHistory": False}]
        )
        values = (result or {}).get("value") or [None]
        return values[0]

    async def get_token_account_balance(self, pubkey: str) -> Optional[int]:
        try:
            result = await self.call(
                "getTokenAccountBalance", [pubkey, {"commitment": "confirmed"}]
            )
        except RpcError:
            return None
        value = (result or {}).get("value")
        if not value:
            return None
        return int(value["amount"])

    async def get_balance(self, pubkey: str) -> int:
        result = await self.call("getBalance", [pubkey, {"commitment": "confirmed"}])
        return int((result or {}).get("value", 0))

"""Helius Atlas `transactionSubscribe` backend.

Atlas is Helius's geyser feed exposed over a websocket, so it gives you
geyser-grade latency without the gRPC codegen step. It requires a paid Helius
plan; the endpoint looks like::

    wss://atlas-mainnet.helius-rpc.com/?api-key=YOUR_KEY

Frames carry the whole transaction. We read the logs first because the Anchor
event there is self-contained, and fall back to decoding the transaction bytes
if the logs are absent or truncated.
"""

from __future__ import annotations

import base64
import time
from typing import AsyncIterator, Optional

from solders.transaction import VersionedTransaction
from websockets.asyncio.client import connect

from ..constants import PUMP_FUN_PROGRAM
from ..logging_setup import get_logger
from ..pumpfun import LaunchEvent
from . import _json
from .base import Listener
from .scan import scan_instructions, scan_logs

log = get_logger("sniper.listener.atlas")


def _decode_transaction(encoded: object) -> Optional[LaunchEvent]:
    """Parse a base64-encoded transaction for a create instruction.

    Only the top-level instructions are reachable this way; a create hidden
    behind a CPI is not visible without the meta. That is fine — this path only
    runs when the logs, which do contain CPI events, were unusable.
    """
    if isinstance(encoded, list) and encoded:
        encoded = encoded[0]
    if not isinstance(encoded, str):
        return None
    try:
        tx = VersionedTransaction.from_bytes(base64.b64decode(encoded))
    except Exception:
        return None

    message = tx.message
    keys = list(message.account_keys)
    instructions = [
        (ix.program_id_index, list(ix.accounts), bytes(ix.data))
        for ix in message.instructions
    ]
    return scan_instructions(keys, instructions)


class AtlasListener(Listener):
    @property
    def name(self) -> str:
        return "helius_atlas"

    async def _connect_and_stream(self) -> AsyncIterator[LaunchEvent]:
        request = _json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "transactionSubscribe",
                "params": [
                    {
                        "failed": False,
                        "vote": False,
                        "accountInclude": [str(PUMP_FUN_PROGRAM)],
                    },
                    {
                        "commitment": "processed",
                        "encoding": "base64",
                        "transactionDetails": "full",
                        "showRewards": False,
                        "maxSupportedTransactionVersion": 0,
                    },
                ],
            }
        )

        async with connect(
            self.cfg.endpoint,
            ping_interval=self.cfg.ping_interval_s,
            ping_timeout=self.cfg.ping_interval_s,
            max_size=32 * 1024 * 1024,
            open_timeout=10,
        ) as ws:
            await ws.send(request)
            ack = _json.loads(await ws.recv())
            if "error" in ack:
                raise RuntimeError(f"transactionSubscribe rejected: {ack['error']}")
            log.info(
                "listener_connected",
                extra={"backend": self.name, "subscription": ack.get("result")},
            )

            async for raw in ws:
                received_ns = time.perf_counter_ns()
                message = _json.loads(raw)
                if message.get("method") != "transactionNotification":
                    continue
                result = message["params"]["result"]
                payload = result.get("transaction") or {}
                meta = payload.get("meta") or {}
                if meta.get("err") is not None:
                    continue

                event = scan_logs(meta.get("logMessages") or ())
                if event is None:
                    event = _decode_transaction(payload.get("transaction"))
                if event is None:
                    continue

                yield event.with_context(
                    signature=result.get("signature"),
                    slot=result.get("slot"),
                    received_ns=received_ns,
                )

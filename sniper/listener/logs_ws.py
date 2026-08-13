"""`logsSubscribe` backend — works against any standard Solana RPC websocket.

This is the compatibility path, not the fast path. It is push-based and it
carries the full Anchor event, so matching works exactly as it does on geyser,
but the notification is emitted after the node has processed the transaction
and it goes through the node's generic pubsub fan-out. Expect tens of
milliseconds more latency than a dedicated geyser feed, and expect it to be the
first thing to fall behind when the cluster is busy.

Use it to develop against and to fail over to. Run `yellowstone` in production.
"""

from __future__ import annotations

import time
from typing import AsyncIterator

from websockets.asyncio.client import connect

from ..constants import PUMP_FUN_PROGRAM
from ..logging_setup import get_logger
from ..pumpfun import LaunchEvent
from . import _json
from .base import Listener
from .scan import scan_logs

log = get_logger("sniper.listener.logs")


class LogsListener(Listener):
    @property
    def name(self) -> str:
        return "logs"

    async def _connect_and_stream(self) -> AsyncIterator[LaunchEvent]:
        request = _json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "logsSubscribe",
                "params": [
                    {"mentions": [str(PUMP_FUN_PROGRAM)]},
                    # `processed` is the whole point: waiting for `confirmed`
                    # would cost us the block we are trying to land in.
                    {"commitment": "processed"},
                ],
            }
        )

        async with connect(
            self.cfg.endpoint,
            ping_interval=self.cfg.ping_interval_s,
            ping_timeout=self.cfg.ping_interval_s,
            max_size=16 * 1024 * 1024,
            open_timeout=10,
        ) as ws:
            await ws.send(request)
            ack = _json.loads(await ws.recv())
            if "error" in ack:
                raise RuntimeError(f"logsSubscribe rejected: {ack['error']}")
            self.subscribed = True
            log.info(
                "listener_connected",
                extra={"backend": self.name, "subscription": ack.get("result")},
            )

            async for raw in ws:
                received_ns = time.perf_counter_ns()
                message = _json.loads(raw)
                if message.get("method") != "logsNotification":
                    continue
                result = message["params"]["result"]
                value = result["value"]
                if value.get("err") is not None:
                    continue

                event = scan_logs(value.get("logs") or ())
                if event is None:
                    continue
                yield event.with_context(
                    signature=value.get("signature"),
                    slot=result.get("context", {}).get("slot"),
                    received_ns=received_ns,
                )

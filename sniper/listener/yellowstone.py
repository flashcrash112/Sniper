"""Yellowstone gRPC backend — the production path.

This talks directly to a Yellowstone geyser plugin (Helius dedicated nodes,
Triton, or your own validator). It is the lowest-latency option available: the
plugin hands us the transaction as the validator processes it, with no pubsub
fan-out and no JSON in between.

It needs generated protobuf stubs, which are not vendored because they must
match your provider's plugin version. Generate them once::

    ./scripts/gen_proto.sh

Endpoint forms::

    https://your-node.example.com:443     TLS (typical for a hosted provider)
    http://10.0.0.5:10000                 plaintext (same-host / private network)
    your-node.example.com:443             TLS assumed

Providers authenticate with an `x-token` metadata header; put it in
`[geyser] x_token`.
"""

from __future__ import annotations

import asyncio
import time
from typing import AsyncIterator, Optional, Sequence

from solders.pubkey import Pubkey

from ..constants import PUMP_FUN_PROGRAM
from ..logging_setup import get_logger
from ..pumpfun import LaunchEvent
from .base import Listener
from .scan import scan_instructions, scan_logs

log = get_logger("sniper.listener.yellowstone")

_IMPORT_HELP = (
    "Yellowstone protobuf stubs are missing. Generate them with "
    "./scripts/gen_proto.sh (needs grpcio-tools), or switch "
    "[geyser] backend to 'helius_atlas' or 'logs'."
)


def _load_stubs():
    """Import the generated stubs, with an actionable error if absent."""
    try:
        from .proto import geyser_pb2, geyser_pb2_grpc  # type: ignore
    except ImportError as exc:  # pragma: no cover - depends on codegen
        raise RuntimeError(f"{_IMPORT_HELP} (import error: {exc})") from exc
    return geyser_pb2, geyser_pb2_grpc


def _b58(raw: bytes) -> str:
    import base58

    return base58.b58encode(raw).decode("ascii")


class YellowstoneListener(Listener):
    def __init__(self, cfg) -> None:
        super().__init__(cfg)
        self._pb, self._grpc_stubs = _load_stubs()

    @property
    def name(self) -> str:
        return "yellowstone"

    def _build_request(self):
        pb = self._pb
        request = pb.SubscribeRequest()
        # One filter, as narrow as the protocol allows: non-vote, non-failed
        # transactions that touch the pump.fun program. Everything else is
        # dropped at the validator instead of on our socket.
        tx_filter = request.transactions["pump"]
        tx_filter.vote = False
        tx_filter.failed = False
        tx_filter.account_include.append(str(PUMP_FUN_PROGRAM))
        request.commitment = pb.CommitmentLevel.PROCESSED
        return request

    async def _requests(self, pb, stop: asyncio.Event):
        """Outbound half of the bidi stream: subscribe, then keep alive."""
        yield self._build_request()
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.cfg.ping_interval_s)
                return
            except asyncio.TimeoutError:
                pass
            ping = pb.SubscribeRequest()
            ping.ping.id = 1
            yield ping

    async def _connect_and_stream(self) -> AsyncIterator[LaunchEvent]:
        import grpc

        pb, pb_grpc = self._pb, self._grpc_stubs

        endpoint = self.cfg.endpoint
        secure = True
        if endpoint.startswith("http://"):
            endpoint, secure = endpoint[len("http://") :], False
        elif endpoint.startswith("https://"):
            endpoint = endpoint[len("https://") :]
        endpoint = endpoint.rstrip("/")

        options = [
            ("grpc.keepalive_time_ms", 15_000),
            ("grpc.keepalive_timeout_ms", 5_000),
            ("grpc.keepalive_permit_without_calls", 1),
            ("grpc.max_receive_message_length", 128 * 1024 * 1024),
            # Nagle would batch small frames and add latency for no benefit.
            ("grpc.http2.max_pings_without_data", 0),
        ]
        channel = (
            grpc.aio.secure_channel(
                endpoint, grpc.ssl_channel_credentials(), options=options
            )
            if secure
            else grpc.aio.insecure_channel(endpoint, options=options)
        )

        metadata = []
        if self.cfg.x_token:
            metadata.append(("x-token", self.cfg.x_token))

        stop = asyncio.Event()
        try:
            stub = pb_grpc.GeyserStub(channel)
            call = stub.Subscribe(self._requests(pb, stop), metadata=metadata)
            log.info("listener_connected", extra={"backend": self.name})

            async for update in call:
                received_ns = time.perf_counter_ns()
                if not update.HasField("transaction"):
                    continue
                event = self._parse_update(update)
                if event is not None:
                    yield event.with_context(
                        signature=_b58(bytes(update.transaction.transaction.signature)),
                        slot=update.transaction.slot,
                        received_ns=received_ns,
                    )
        finally:
            stop.set()
            await channel.close()

    def _parse_update(self, update) -> Optional[LaunchEvent]:
        info = update.transaction.transaction
        meta = info.meta
        message = info.transaction.message

        def account_keys() -> Sequence[Pubkey]:
            # Address-table lookups are appended in the order the runtime
            # resolves them: static keys, then writable, then readonly.
            keys = [Pubkey.from_bytes(bytes(k)) for k in message.account_keys]
            keys += [Pubkey.from_bytes(bytes(k)) for k in meta.loaded_writable_addresses]
            keys += [Pubkey.from_bytes(bytes(k)) for k in meta.loaded_readonly_addresses]
            return keys

        # Inner instructions first: pump.fun emits its create event as a
        # self-CPI, so this is where a launch normally shows up.
        for group in meta.inner_instructions:
            event = scan_instructions(
                account_keys,
                (
                    (ix.program_id_index, list(ix.accounts), bytes(ix.data))
                    for ix in group.instructions
                ),
            )
            if event is not None:
                return event

        event = scan_instructions(
            account_keys,
            (
                (ix.program_id_index, list(ix.accounts), bytes(ix.data))
                for ix in message.instructions
            ),
        )
        if event is not None:
            return event

        return scan_logs(meta.log_messages)

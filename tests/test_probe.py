"""Feed probe: does this endpoint and API key actually give us what we need?

The probe exists to answer a question a pricing page cannot: whether the plan
you bought supports the subscription this bot needs. So the tests cover the
four outcomes that must be told apart, because each has a different fix.
"""

import asyncio
import json

import pytest
import websockets

from conftest import make_program_data_log
from sniper.config import GeyserConfig
from sniper.constants import PUMP_FUN_PROGRAM
from sniper.listener.base import probe_listener


class Feed:
    def __init__(self, handler):
        self.handler = handler
        self.server = None
        self._closing = asyncio.Event()

    async def __aenter__(self):
        self.server = await websockets.serve(self._run, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        return GeyserConfig(backend="logs", endpoint=f"ws://127.0.0.1:{port}")

    async def __aexit__(self, *exc):
        self._closing.set()
        self.server.close()
        await self.server.wait_closed()

    async def _run(self, ws):
        await self.handler(ws)
        await self._closing.wait()


def notification(symbol):
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "method": "logsNotification",
            "params": {
                "result": {
                    "context": {"slot": 1},
                    "value": {
                        "signature": f"sig-{symbol}",
                        "err": None,
                        "logs": [
                            f"Program {PUMP_FUN_PROGRAM} invoke [1]",
                            make_program_data_log(symbol=symbol),
                        ],
                    },
                },
                "subscription": 1,
            },
        }
    )


async def healthy(ws):
    request = json.loads(await ws.recv())
    assert request["method"] == "logsSubscribe"
    await ws.send(json.dumps({"jsonrpc": "2.0", "result": 1, "id": 1}))
    for symbol in ("AAA", "BBB", "CCC"):
        await ws.send(notification(symbol))


async def rejected(ws):
    await ws.recv()
    await ws.send(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "error": {"code": -32601, "message": "method not supported on your plan"},
            }
        )
    )


async def silent(ws):
    await ws.recv()
    await ws.send(json.dumps({"jsonrpc": "2.0", "result": 1, "id": 1}))


@pytest.mark.asyncio
async def test_healthy_feed_reports_ok_with_timing():
    async with Feed(healthy) as cfg:
        result = await probe_listener(cfg, timeout_s=5)

    assert result.ok
    assert result.subscribed
    assert result.events_seen == 3
    assert result.first_event_ms is not None
    assert "receiving" in result.explain()


@pytest.mark.asyncio
async def test_plan_rejection_is_diagnosed_as_a_plan_problem():
    """The case a user on the wrong Helius tier will actually hit."""
    async with Feed(rejected) as cfg:
        result = await probe_listener(cfg, timeout_s=5)

    assert not result.ok
    assert not result.subscribed
    assert "not supported on your plan" in result.error
    assert "plan does not include" in result.explain()


@pytest.mark.asyncio
async def test_subscribed_but_silent_is_distinguished_from_rejected():
    """Both look like 'no data'; only one is a billing problem."""
    async with Feed(silent) as cfg:
        result = await probe_listener(cfg, timeout_s=1.5)

    assert not result.ok
    assert result.subscribed, "a successful subscription must be recorded as such"
    assert result.events_seen == 0
    assert result.error is None
    assert "no pump.fun launches arrived" in result.explain()


@pytest.mark.asyncio
async def test_unreachable_endpoint_is_reported_not_raised():
    cfg = GeyserConfig(backend="logs", endpoint="ws://127.0.0.1:1")
    result = await probe_listener(cfg, timeout_s=5)

    assert not result.ok
    assert not result.subscribed
    assert result.error


@pytest.mark.asyncio
async def test_probe_does_not_retry_on_failure():
    """A diagnostic must surface the error, not back off and hide it.

    `stream()` retries forever by design; the probe must not inherit that or a
    bad API key would look like a hang.
    """
    attempts = 0

    async def count_and_reject(ws):
        nonlocal attempts
        attempts += 1
        await ws.recv()
        await ws.send(
            json.dumps({"jsonrpc": "2.0", "id": 1, "error": {"message": "nope"}})
        )

    async with Feed(count_and_reject) as cfg:
        await probe_listener(cfg, timeout_s=5)

    assert attempts == 1


@pytest.mark.asyncio
async def test_probe_returns_promptly_on_a_silent_feed():
    """It must respect its own timeout rather than hanging on a quiet endpoint."""
    async with Feed(silent) as cfg:
        loop = asyncio.get_running_loop()
        started = loop.time()
        await probe_listener(cfg, timeout_s=1.0)
        elapsed = loop.time() - started

    assert elapsed < 3.0, f"probe overran its timeout ({elapsed:.1f}s)"

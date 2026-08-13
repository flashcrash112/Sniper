"""End-to-end test against a stubbed RPC and a stubbed geyser feed.

Runs the real `Sniper` object over the real listener, matcher, buyer and RPC
code paths. The only fakes are the two network endpoints. What it proves:

* a create event on the wire is parsed, scored and acted on,
* the transaction that reaches `sendTransaction` is a valid, signed pump.fun
  buy for the mint that was announced,
* the spend cap stops trading afterwards,
* `--dry-run` sends nothing at all.
"""

import asyncio
import base64
import json
import struct

import pytest
import websockets
from aiohttp import web
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from conftest import make_program_data_log
from sniper.config import load_config
from sniper.constants import BUY_IX_DISCRIMINATOR, PUMP_FUN_PROGRAM
from sniper.keystore import ScryptParams, create_keystore
from sniper.logging_setup import setup_logging, shutdown_logging
from sniper.main import Sniper

FEE_RECIPIENT = Pubkey.new_unique()
BLOCKHASH = "11111111111111111111111111111111"


def global_account_bytes() -> str:
    data = (
        b"\x00" * 8
        + b"\x01"
        + bytes(Pubkey.new_unique())
        + bytes(FEE_RECIPIENT)
        + struct.pack("<Q", 1_073_000_000_000_000)
        + struct.pack("<Q", 30_000_000_000)
        + struct.pack("<Q", 793_100_000_000_000)
        + struct.pack("<Q", 1_000_000_000_000_000)
        + struct.pack("<Q", 100)
    )
    return base64.b64encode(data).decode()


class FakeRpc:
    """Just enough JSON-RPC for the bot to start up and send."""

    def __init__(self):
        self.sent: list[str] = []
        self.app = web.Application()
        self.app.router.add_post("/", self.handle)
        self.runner = None
        self.url = None

    async def start(self):
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}/"
        return self.url

    async def stop(self):
        if self.runner:
            await self.runner.cleanup()

    async def handle(self, request: web.Request) -> web.Response:
        body = await request.json()
        method = body.get("method")
        result = self.dispatch(method, body.get("params"))
        return web.json_response({"jsonrpc": "2.0", "id": body.get("id"), "result": result})

    def dispatch(self, method, params):
        if method == "getHealth":
            return "ok"
        if method == "getAccountInfo":
            return {
                "context": {"slot": 1},
                "value": {
                    "data": [global_account_bytes(), "base64"],
                    "executable": False,
                    "lamports": 1,
                    "owner": str(PUMP_FUN_PROGRAM),
                    "rentEpoch": 0,
                },
            }
        if method == "getLatestBlockhash":
            return {
                "context": {"slot": 1},
                "value": {"blockhash": BLOCKHASH, "lastValidBlockHeight": 1000},
            }
        if method == "getBalance":
            return {"context": {"slot": 1}, "value": 5_000_000_000}
        if method == "sendTransaction":
            self.sent.append(params[0])
            tx = VersionedTransaction.from_bytes(base64.b64decode(params[0]))
            return str(tx.signatures[0])
        if method == "getSignatureStatuses":
            return {
                "context": {"slot": 2},
                "value": [
                    {
                        "slot": 2,
                        "confirmations": 0,
                        "err": None,
                        "confirmationStatus": "confirmed",
                    }
                ],
            }
        if method == "getTokenAccountBalance":
            return {
                "context": {"slot": 2},
                "value": {"amount": "12345678", "decimals": 6, "uiAmount": 12.345678},
            }
        raise AssertionError(f"unexpected RPC method: {method}")


class FakeGeyser:
    """A websocket that accepts logsSubscribe and replays canned launches."""

    def __init__(self, notifications):
        self.notifications = notifications
        self.server = None
        self.url = None
        self._closing = asyncio.Event()

    async def start(self):
        self.server = await websockets.serve(self._handler, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}"
        return self.url

    async def stop(self):
        self._closing.set()
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    async def _handler(self, ws):
        request = json.loads(await ws.recv())
        assert request["method"] == "logsSubscribe"
        await ws.send(json.dumps({"jsonrpc": "2.0", "result": 1, "id": 1}))
        for notification in self.notifications:
            await ws.send(json.dumps(notification))
        # Then go quiet and stay connected, like a real feed between launches,
        # until the test tears the server down.
        await self._closing.wait()


def logs_notification(log_line: str, signature: str, slot: int = 100) -> dict:
    return {
        "jsonrpc": "2.0",
        "method": "logsNotification",
        "params": {
            "result": {
                "context": {"slot": slot},
                "value": {
                    "signature": signature,
                    "err": None,
                    "logs": [
                        f"Program {PUMP_FUN_PROGRAM} invoke [1]",
                        "Program log: Instruction: Create",
                        log_line,
                        f"Program {PUMP_FUN_PROGRAM} success",
                    ],
                },
            },
            "subscription": 1,
        },
    }


def write_config(tmp_path, rpc_url, ws_url, keystore, **overrides) -> str:
    settings = {
        "ticker": "SNIPE",
        "twitter": "",
        "threshold": 0.7,
        "max_positions": 1,
        "max_spend": 1.0,
        "dry_run": "false",
    }
    settings.update(overrides)
    twitter_line = (
        f'twitter = "{settings["twitter"]}"' if settings["twitter"] else ""
    )
    text = f"""
[rpc]
http_url = "{rpc_url}"
send_urls = ["{rpc_url}"]
blockhash_refresh_ms = 5000

[geyser]
backend = "logs"
endpoint = "{ws_url}"

[target]
ticker = "{settings['ticker']}"
{twitter_line}
confidence_threshold = {settings['threshold']}
metadata_timeout_ms = 150

[wallet]
keystore_path = "{keystore}"

[buy]
amount_sol = 0.1
slippage_bps = 1000
priority_fee_microlamports = 100000
compute_unit_limit = 120000

[safety]
dry_run = {settings['dry_run']}
kill_switch_file = "{tmp_path}/KILL"
max_total_spend_sol = {settings['max_spend']}
max_positions = {settings['max_positions']}
confirm_timeout_s = 5

[logging]
level = "DEBUG"
json = true
"""
    path = tmp_path / "config.toml"
    path.write_text(text)
    return str(path)


@pytest.fixture
def keystore(tmp_path, monkeypatch):
    path = tmp_path / "key.json"
    create_keystore(path, bytes(Keypair()), "pw", params=ScryptParams(n=1 << 12))
    monkeypatch.setenv("SNIPER_KEYSTORE_PASSWORD", "pw")
    return path


async def run_sniper(config_path, dry_run=False, timeout=15, stop_after=None):
    """Run to completion. `stop_after` trips the kill switch after N seconds.

    Calls setup_logging(), matching cmd_run() in main.py. Skipping this is not
    a shortcut: with no handler configured, the root logger defaults to
    WARNING, so every log.info()/log.debug() call in the bot — including the
    ones fired on the exact path a live match takes — is silently skipped
    before it ever runs. A bug in what those calls pass as `extra=` (see the
    "Attempt to overwrite 'name' in LogRecord" incident) is then invisible to
    every test, and only crashes in production once real logging is on.
    """
    cfg = load_config(config_path)
    setup_logging(cfg.logging)
    sniper = Sniper(cfg, dry_run=dry_run)

    stopper = None
    if stop_after is not None:

        async def trip():
            await asyncio.sleep(stop_after)
            cfg.safety.kill_switch_file.touch()

        stopper = asyncio.create_task(trip())
    try:
        await asyncio.wait_for(sniper.run(), timeout=timeout)
    finally:
        if stopper is not None:
            stopper.cancel()
        shutdown_logging()
    return sniper


@pytest.mark.asyncio
async def test_matching_launch_is_bought(tmp_path, keystore):
    mint = Pubkey.new_unique()
    creator = Pubkey.new_unique()
    rpc, geyser = FakeRpc(), FakeGeyser(
        [
            # A decoy that must be ignored, then the real thing.
            logs_notification(make_program_data_log(symbol="OTHER"), "sig-decoy"),
            logs_notification(
                make_program_data_log(symbol="SNIPE", mint=mint, creator=creator),
                "sig-target",
            ),
        ]
    )
    rpc_url, ws_url = await rpc.start(), await geyser.start()
    try:
        sniper = await run_sniper(write_config(tmp_path, rpc_url, ws_url, keystore))
    finally:
        await rpc.stop()
        await geyser.stop()

    assert sniper.candidates == 2
    assert sniper.fired == 1
    assert len(rpc.sent) == 1, "expected exactly one broadcast"

    tx = VersionedTransaction.from_bytes(base64.b64decode(rpc.sent[0]))
    assert all(tx.verify_with_results()), "broadcast transaction was not validly signed"

    keys = list(tx.message.account_keys)
    buy = tx.message.instructions[-1]
    assert keys[buy.program_id_index] == PUMP_FUN_PROGRAM
    assert bytes(buy.data)[:8] == BUY_IX_DISCRIMINATOR
    # Account 2 of the buy layout is the mint that was announced on the feed.
    assert keys[buy.accounts[2]] == mint
    assert keys[buy.accounts[1]] == FEE_RECIPIENT

    # The cap accounts for the position plus its priority fee.
    assert sniper.ledger.positions == 1
    assert sniper.ledger.reserved_lamports == 100_000_000 + 12_000


@pytest.mark.asyncio
async def test_dry_run_sends_nothing(tmp_path, keystore):
    rpc, geyser = FakeRpc(), FakeGeyser(
        [logs_notification(make_program_data_log(symbol="SNIPE"), "sig-target")]
    )
    rpc_url, ws_url = await rpc.start(), await geyser.start()
    try:
        sniper = await run_sniper(
            write_config(tmp_path, rpc_url, ws_url, keystore), dry_run=True
        )
    finally:
        await rpc.stop()
        await geyser.stop()

    assert sniper.fired == 1
    assert rpc.sent == [], "dry run broadcast a transaction"


@pytest.mark.asyncio
async def test_non_matching_ticker_is_never_bought(tmp_path, keystore):
    rpc, geyser = FakeRpc(), FakeGeyser(
        [logs_notification(make_program_data_log(symbol=s), f"sig-{s}") for s in
         ("AAA", "BBB", "SNIPEX", "SNIP")]
    )
    rpc_url, ws_url = await rpc.start(), await geyser.start()
    try:
        config = write_config(tmp_path, rpc_url, ws_url, keystore)
        sniper = await run_sniper(config, timeout=20, stop_after=1.0)
    finally:
        await rpc.stop()
        await geyser.stop()

    assert sniper.candidates == 4
    assert sniper.fired == 0
    assert rpc.sent == []


@pytest.mark.asyncio
async def test_kill_switch_stops_a_quiet_feed_promptly(tmp_path, keystore):
    """A parked listener must still be interruptible — the case you need it in."""
    rpc, geyser = FakeRpc(), FakeGeyser([])
    rpc_url, ws_url = await rpc.start(), await geyser.start()
    try:
        config = write_config(tmp_path, rpc_url, ws_url, keystore)
        started = asyncio.get_running_loop().time()
        sniper = await run_sniper(config, timeout=20, stop_after=1.0)
        elapsed = asyncio.get_running_loop().time() - started
    finally:
        await rpc.stop()
        await geyser.stop()

    assert sniper.kill.engaged
    assert rpc.sent == []
    # Trip at 1.0s, 200 ms poll interval, plus shutdown. Anything near the
    # 30 s feed silence means the loop was blocked on the stream.
    assert elapsed < 5, f"kill switch took {elapsed:.1f}s to stop a quiet feed"


@pytest.mark.asyncio
async def test_duplicate_mints_are_only_evaluated_once(tmp_path, keystore):
    """The same launch arriving twice must not produce two buys."""
    mint = Pubkey.new_unique()
    line = make_program_data_log(symbol="SNIPE", mint=mint)
    rpc, geyser = FakeRpc(), FakeGeyser(
        [logs_notification(line, "sig-a"), logs_notification(line, "sig-b")]
    )
    rpc_url, ws_url = await rpc.start(), await geyser.start()
    try:
        # max_positions=2 so the run does not stop on the position limit; if
        # dedupe were broken the second notification would buy again.
        config = write_config(
            tmp_path, rpc_url, ws_url, keystore, max_positions=2, max_spend=1.0
        )
        sniper = await run_sniper(config, timeout=20, stop_after=1.5)
    finally:
        await rpc.stop()
        await geyser.stop()

    assert sniper.candidates == 1
    assert len(rpc.sent) == 1

"""Jito tip encoding and bundle submission."""

import base58
import pytest
from aiohttp import web
from solders.hash import Hash
from solders.keypair import Keypair
from solders.pubkey import Pubkey

from sniper.buyer import Buyer
from sniper.config import BuyConfig, ConfigError, JitoConfig, RpcConfig, SafetyConfig
from sniper.constants import SYSTEM_PROGRAM
from sniper.curve import CurveState
from sniper.jito import (
    MIN_TIP_LAMPORTS,
    TIP_ACCOUNTS,
    JitoClient,
    build_tip_instruction,
)
from sniper.pumpfun import LaunchEvent


def test_tip_instruction_is_a_system_transfer():
    payer = Pubkey.new_unique()
    ix = build_tip_instruction(payer, 1_000_000, TIP_ACCOUNTS[0])

    assert ix.program_id == SYSTEM_PROGRAM
    data = bytes(ix.data)
    assert data[:4] == (2).to_bytes(4, "little")  # Transfer
    assert int.from_bytes(data[4:12], "little") == 1_000_000
    assert ix.accounts[0].pubkey == payer
    assert ix.accounts[0].is_signer
    assert ix.accounts[1].pubkey == TIP_ACCOUNTS[0]
    assert ix.accounts[1].is_writable


def test_tip_below_the_minimum_is_refused():
    with pytest.raises(ValueError, match=str(MIN_TIP_LAMPORTS)):
        build_tip_instruction(Pubkey.new_unique(), 999, TIP_ACCOUNTS[0])


def test_tip_accounts_round_robin():
    client = JitoClient("https://example.invalid")
    picked = [client.next_tip_account() for _ in range(len(TIP_ACCOUNTS) + 2)]
    assert picked[: len(TIP_ACCOUNTS)] == TIP_ACCOUNTS
    assert picked[len(TIP_ACCOUNTS)] == TIP_ACCOUNTS[0]  # wraps


# --- integration with the buy transaction ----------------------------------


def make_buyer(jito_client=None, jito_cfg=None):
    buyer = Buyer(
        keypair=Keypair(),
        rpc=None,
        buy_cfg=BuyConfig(amount_sol=0.1, slippage_bps=1000, priority_fee_microlamports=1),
        rpc_cfg=RpcConfig(http_url="x", send_urls=["x"]),
        safety_cfg=SafetyConfig(kill_switch_file="/tmp/K", max_total_spend_sol=1.0),
        dry_run=True,
        jito=jito_client,
        jito_cfg=jito_cfg,
    )
    buyer._fee_recipient = Pubkey.new_unique()
    return buyer


def make_event():
    return LaunchEvent(
        mint=Pubkey.new_unique(),
        name="n",
        symbol="T",
        uri="",
        creator=Pubkey.new_unique(),
        curve=CurveState.initial(),
    )


def test_tip_is_appended_to_the_buy_transaction():
    """The tip must ride with the buy so an unlanded bundle costs nothing."""
    client = JitoClient("https://example.invalid")
    buyer = make_buyer(client, JitoConfig(enabled=True, tip_lamports=1_000_000))
    event = make_event()

    tx, _ = buyer.build_transaction(event, buyer.quote(event), Hash.default())
    keys = list(tx.message.account_keys)
    programs = [keys[ix.program_id_index] for ix in tx.message.instructions]

    assert len(tx.message.instructions) == 5  # 2 budget + ATA + buy + tip
    assert programs[-1] == SYSTEM_PROGRAM
    tip_ix = tx.message.instructions[-1]
    assert int.from_bytes(bytes(tip_ix.data)[4:12], "little") == 1_000_000
    assert keys[tip_ix.accounts[1]] in TIP_ACCOUNTS
    assert all(tx.verify_with_results())
    assert len(bytes(tx)) <= 1232


def test_no_tip_when_jito_is_disabled():
    buyer = make_buyer()
    event = make_event()
    tx, _ = buyer.build_transaction(event, buyer.quote(event), Hash.default())
    assert len(tx.message.instructions) == 4


# --- bundle submission -----------------------------------------------------


class StubEngine:
    def __init__(self, handler):
        self.handler = handler
        self.bodies = []
        self.runner = None

    async def start(self):
        app = web.Application()
        app.router.add_post("/api/v1/bundles", self._handle)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        return f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"

    async def stop(self):
        await self.runner.cleanup()

    async def _handle(self, request):
        body = await request.json()
        self.bodies.append(body)
        return self.handler(body)


@pytest.mark.asyncio
async def test_send_bundle_encodes_base58_and_returns_the_id():
    engine = StubEngine(
        lambda body: web.json_response({"jsonrpc": "2.0", "id": 1, "result": "BUNDLE123"})
    )
    url = await engine.start()
    try:
        async with JitoClient(url) as client:
            result = await client.send_bundle([b"\x01\x02\x03"])
    finally:
        await engine.stop()

    assert result.ok
    assert result.bundle_id == "BUNDLE123"
    sent = engine.bodies[-1]
    assert sent["method"] == "sendBundle"
    assert base58.b58decode(sent["params"][0][0]) == b"\x01\x02\x03"


@pytest.mark.asyncio
async def test_bundle_error_is_returned_not_raised():
    """A Jito failure must degrade to the RPC path, never kill the buy."""
    engine = StubEngine(
        lambda body: web.json_response(
            {"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "tip too low"}}
        )
    )
    url = await engine.start()
    try:
        async with JitoClient(url) as client:
            result = await client.send_bundle([b"\x01"])
    finally:
        await engine.stop()

    assert not result.ok
    assert "tip too low" in result.error


@pytest.mark.asyncio
async def test_unreachable_block_engine_is_survivable():
    async with JitoClient("http://127.0.0.1:1") as client:
        result = await client.send_bundle([b"\x01"])
    assert not result.ok
    assert result.error


@pytest.mark.asyncio
async def test_html_response_from_the_engine_is_readable():
    engine = StubEngine(lambda body: web.Response(status=502, text="<html>bad gateway</html>"))
    url = await engine.start()
    try:
        async with JitoClient(url) as client:
            result = await client.send_bundle([b"\x01"])
    finally:
        await engine.stop()

    assert not result.ok
    assert "502" in result.error


# --- config ----------------------------------------------------------------

BASE = """
[rpc]
http_url = "https://rpc.example.com"
[geyser]
backend = "logs"
endpoint = "wss://rpc.example.com"
[target]
ticker = "TEST"
[wallet]
keystore_path = "./key.json"
[buy]
amount_sol = 0.1
slippage_bps = 1000
priority_fee_microlamports = 1000000
[safety]
max_total_spend_sol = 0.25
[jito]
enabled = true
tip_lamports = 1000000
"""


def test_jito_tip_counts_against_the_spend_cap(tmp_path):
    """The tip is spent per attempt, so the cap must account for it.

    0.1 SOL buy + 0.00012 SOL priority fee + a 0.2 SOL tip is 0.30012 SOL,
    over the 0.25 cap — so this config could never fire and must be rejected
    at load time rather than at 3am.
    """
    from sniper.config import load_config

    path = tmp_path / "c.toml"
    path.write_text(BASE.replace("tip_lamports = 1000000", "tip_lamports = 200000000"))
    with pytest.raises(ConfigError, match="Jito tip"):
        load_config(path)


def test_a_tip_that_fits_under_the_cap_is_accepted(tmp_path):
    """The boundary in the other direction — 0.1 + 0.00012 + 0.1 fits in 0.25."""
    from sniper.config import load_config

    path = tmp_path / "c.toml"
    path.write_text(BASE.replace("tip_lamports = 1000000", "tip_lamports = 100000000"))
    assert load_config(path).jito.tip_lamports == 100_000_000


def test_jito_minimum_tip_is_enforced_in_config(tmp_path):
    from sniper.config import load_config

    path = tmp_path / "c.toml"
    path.write_text(BASE.replace("tip_lamports = 1000000", "tip_lamports = 500"))
    with pytest.raises(ConfigError, match="at least 1000"):
        load_config(path)


def test_valid_jito_config_loads(tmp_path):
    from sniper.config import load_config

    path = tmp_path / "c.toml"
    path.write_text(BASE)
    cfg = load_config(path)
    assert cfg.jito.enabled
    assert cfg.jito.tip_lamports == 1_000_000
    assert cfg.jito.also_send_rpc is True

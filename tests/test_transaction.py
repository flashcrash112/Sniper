"""End-to-end checks on the transaction the buy module actually emits."""

import pytest
from solders.hash import Hash
from solders.keypair import Keypair
from solders.pubkey import Pubkey

from sniper.config import BuyConfig, RpcConfig, SafetyConfig
from sniper.constants import (
    ASSOCIATED_TOKEN_PROGRAM,
    BUY_IX_DISCRIMINATOR,
    COMPUTE_BUDGET_PROGRAM,
    EVENT_AUTHORITY,
    GLOBAL_ACCOUNT,
    PUMP_FUN_PROGRAM,
)
from sniper.buyer import Buyer, set_compute_unit_limit, set_compute_unit_price
from sniper.curve import CurveState
from sniper.pumpfun import (
    BuyLayout,
    LaunchEvent,
    derive_associated_token_account,
    derive_bonding_curve,
)


@pytest.fixture
def buyer():
    keypair = Keypair()
    b = Buyer(
        keypair=keypair,
        rpc=None,  # not touched by the build path
        buy_cfg=BuyConfig(
            amount_sol=0.5,
            slippage_bps=1000,
            priority_fee_microlamports=2_000_000,
            compute_unit_limit=120_000,
        ),
        rpc_cfg=RpcConfig(http_url="http://x", send_urls=["http://x"]),
        safety_cfg=SafetyConfig(kill_switch_file="/tmp/KILL", max_total_spend_sol=1.0),
        dry_run=True,
    )
    b._fee_recipient = Pubkey.new_unique()
    return b


@pytest.fixture
def event():
    return LaunchEvent(
        mint=Pubkey.new_unique(),
        name="Test Coin",
        symbol="TEST",
        uri="ipfs://x",
        creator=Pubkey.new_unique(),
        curve=CurveState.initial(),
    )


def test_known_pdas_match_mainnet():
    """Guards the seed constants against a typo."""
    assert str(GLOBAL_ACCOUNT) == "4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf"
    assert str(EVENT_AUTHORITY) == "Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1"


def test_compute_budget_encoding():
    limit = set_compute_unit_limit(120_000)
    assert limit.program_id == COMPUTE_BUDGET_PROGRAM
    assert bytes(limit.data) == b"\x02" + (120_000).to_bytes(4, "little")

    price = set_compute_unit_price(2_000_000)
    assert bytes(price.data) == b"\x03" + (2_000_000).to_bytes(8, "little")


def test_transaction_structure(buyer, event):
    quote = buyer.quote(event)
    tx, ata = buyer.build_transaction(event, quote, Hash.default())

    instructions = tx.message.instructions
    assert len(instructions) == 4, "expected 2 budget + 1 ATA + 1 buy"

    keys = list(tx.message.account_keys)
    programs = [keys[ix.program_id_index] for ix in instructions]
    assert programs[0] == COMPUTE_BUDGET_PROGRAM
    assert programs[1] == COMPUTE_BUDGET_PROGRAM
    assert programs[2] == ASSOCIATED_TOKEN_PROGRAM
    assert programs[3] == PUMP_FUN_PROGRAM

    buy = instructions[3]
    assert bytes(buy.data)[:8] == BUY_IX_DISCRIMINATOR
    assert len(buy.accounts) == 16, "current pump.fun buy layout takes 16 accounts"

    assert ata == derive_associated_token_account(buyer.pubkey, event.mint)


def test_buy_instruction_arguments_match_the_quote(buyer, event):
    import struct

    quote = buyer.quote(event)
    tx, _ = buyer.build_transaction(event, quote, Hash.default())
    data = bytes(tx.message.instructions[3].data)

    amount, max_sol_cost = struct.unpack_from("<QQ", data, 8)
    assert amount == quote.token_amount
    assert max_sol_cost == quote.max_sol_cost
    # Trailing Option<bool> track_volume, defaulted to None.
    assert data[24:] == b"\x00"


def test_legacy_layout_drops_the_appended_accounts(buyer, event):
    buyer.cfg = BuyConfig(
        amount_sol=0.5,
        slippage_bps=1000,
        priority_fee_microlamports=1,
        layout=BuyLayout.LEGACY,
    )
    quote = buyer.quote(event)
    tx, _ = buyer.build_transaction(event, quote, Hash.default())
    buy = tx.message.instructions[3]
    assert len(buy.accounts) == 12
    assert len(bytes(buy.data)) == 24, "legacy layout has no track_volume field"


def test_transaction_is_signed_and_verifies(buyer, event):
    quote = buyer.quote(event)
    tx, _ = buyer.build_transaction(event, quote, Hash.default())
    tx.verify_with_results()
    assert all(tx.verify_with_results())
    assert len(tx.signatures) == 1


def test_transaction_fits_in_a_single_packet(buyer, event):
    """Solana drops anything over 1232 bytes."""
    quote = buyer.quote(event)
    tx, _ = buyer.build_transaction(event, quote, Hash.default())
    assert len(bytes(tx)) <= 1232


def test_bonding_curve_from_the_event_is_preferred_over_re_derivation(buyer):
    """Using the event's value saves a PDA derivation on the hot path."""
    mint = Pubkey.new_unique()
    derived = derive_bonding_curve(mint)
    event = LaunchEvent(
        mint=mint,
        name="n",
        symbol="TEST",
        uri="",
        creator=Pubkey.new_unique(),
        bonding_curve=derived,
    )
    quote = buyer.quote(event)
    tx, _ = buyer.build_transaction(event, quote, Hash.default())
    keys = list(tx.message.account_keys)
    buy_accounts = [keys[i] for i in tx.message.instructions[3].accounts]
    assert buy_accounts[3] == derived


def test_quote_uses_reserves_from_the_event_when_present(buyer):
    """A launch with non-default reserves must be quoted against its own curve."""
    odd = CurveState(
        virtual_token_reserves=500_000_000_000_000,
        virtual_sol_reserves=15_000_000_000,
        real_token_reserves=400_000_000_000_000,
    )
    event = LaunchEvent(
        mint=Pubkey.new_unique(),
        name="n",
        symbol="TEST",
        uri="",
        creator=Pubkey.new_unique(),
        curve=odd,
    )
    default_event = LaunchEvent(
        mint=Pubkey.new_unique(),
        name="n",
        symbol="TEST",
        uri="",
        creator=Pubkey.new_unique(),
        curve=CurveState.initial(),
    )
    assert buyer.quote(event).token_amount != buyer.quote(default_event).token_amount

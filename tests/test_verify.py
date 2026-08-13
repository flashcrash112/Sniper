"""Tests for the mainnet layout verifier.

The verifier's job is to catch a layout drift. So the tests build a transaction
with our own encoder, feed it back in as if it came from mainnet (it must
pass), and then break it in each of the ways a real drift would (it must fail,
and say where).
"""

import base58
from solders.hash import Hash
from solders.keypair import Keypair
from solders.pubkey import Pubkey

from sniper.config import BuyConfig, RpcConfig, SafetyConfig
from sniper.buyer import Buyer
from sniper.constants import GLOBAL_ACCOUNT, PUMP_FUN_PROGRAM
from sniper.curve import CurveState
from sniper.pumpfun import BuyLayout, LaunchEvent
from sniper.verify import (
    _decode_ix_data,
    _find_buy_instruction,
    compare_against_observed,
    format_report,
)


def make_buy(layout=BuyLayout.CURRENT):
    """Build a real buy with our encoder and return (account_keys, data)."""
    keypair = Keypair()
    buyer = Buyer(
        keypair=keypair,
        rpc=None,
        buy_cfg=BuyConfig(
            amount_sol=0.1, slippage_bps=1000, priority_fee_microlamports=1, layout=layout
        ),
        rpc_cfg=RpcConfig(http_url="x", send_urls=["x"]),
        safety_cfg=SafetyConfig(kill_switch_file="/tmp/K", max_total_spend_sol=1.0),
        dry_run=True,
    )
    buyer._fee_recipient = Pubkey.new_unique()
    event = LaunchEvent(
        mint=Pubkey.new_unique(),
        name="n",
        symbol="T",
        uri="",
        creator=Pubkey.new_unique(),
        curve=CurveState.initial(),
    )
    quote = buyer.quote(event)
    tx, _ = buyer.build_transaction(event, quote, Hash.default())
    keys = list(tx.message.account_keys)
    buy_ix = tx.message.instructions[-1]
    return [keys[i] for i in buy_ix.accounts], bytes(buy_ix.data)


def test_our_own_encoding_passes_verification():
    """The baseline: what we build must validate against what we expect."""
    observed, data = make_buy()
    report = compare_against_observed(observed, data, BuyLayout.CURRENT)

    assert report.ok, [c.role for c in report.mismatches]
    assert report.detected_layout is BuyLayout.CURRENT
    assert report.account_count == 16
    assert report.notes == []
    # Every derivable position was actually checked, not silently skipped.
    assert sum(1 for c in report.checks if c.ok is True) == 12
    assert report.token_program_name == "SPL Token (legacy)"


def test_legacy_encoding_is_detected_as_legacy():
    observed, data = make_buy(BuyLayout.LEGACY)
    report = compare_against_observed(observed, data, BuyLayout.LEGACY)
    assert report.ok
    assert report.detected_layout is BuyLayout.LEGACY
    assert report.account_count == 12


def test_layout_mismatch_against_config_is_reported():
    """Chain says legacy, config says current — the exact drift we care about."""
    observed, data = make_buy(BuyLayout.LEGACY)
    report = compare_against_observed(observed, data, BuyLayout.CURRENT)

    assert not report.ok
    assert report.detected_layout is BuyLayout.LEGACY
    assert any("layout is set to" in note for note in report.notes)


def test_a_swapped_account_is_caught():
    """The failure mode a drift actually produces: right count, wrong order."""
    observed, data = make_buy()
    swapped = list(observed)
    swapped[7], swapped[8] = swapped[8], swapped[7]  # system <-> token program

    report = compare_against_observed(swapped, data, BuyLayout.CURRENT)
    assert not report.ok
    bad = {c.role for c in report.mismatches}
    assert bad == {"system_program", "token_program"}


def test_a_token_2022_mint_is_recognised_not_reported_as_three_failures():
    """The real mainnet case: Token-2022 changes the ATA seeds.

    Naively comparing against the legacy token program reports three
    mismatches — token_program and both ATAs — for what is one fact. Deriving
    the ATAs against the observed token program collapses that to zero.
    """
    from sniper.pumpfun import derive_associated_token_account, derive_bonding_curve
    from sniper.verify import TOKEN_2022_PROGRAM

    observed, data = make_buy()
    mint, user = observed[2], observed[6]
    curve = derive_bonding_curve(mint)

    t22 = list(observed)
    t22[4] = derive_associated_token_account(curve, mint, TOKEN_2022_PROGRAM)
    t22[5] = derive_associated_token_account(user, mint, TOKEN_2022_PROGRAM)
    t22[8] = TOKEN_2022_PROGRAM

    report = compare_against_observed(t22, data, BuyLayout.CURRENT)
    assert report.mismatches == [], [c.role for c in report.mismatches]
    assert report.token_program_name == "Token-2022"
    assert any("Token-2022" in note for note in report.notes)


def test_legacy_ata_against_a_token_2022_mint_is_caught():
    """The inverse: right token program, ATAs derived with the wrong one."""
    from sniper.verify import TOKEN_2022_PROGRAM

    observed, data = make_buy()
    broken = list(observed)
    broken[8] = TOKEN_2022_PROGRAM  # ATAs at 4/5 still legacy-derived

    report = compare_against_observed(broken, data, BuyLayout.CURRENT)
    assert {c.role for c in report.mismatches} == {
        "associated_bonding_curve",
        "associated_user",
    }


def test_a_non_token_program_at_index_8_is_caught():
    from solders.pubkey import Pubkey as _P

    observed, data = make_buy()
    broken = list(observed)
    broken[8] = _P.new_unique()

    report = compare_against_observed(broken, data, BuyLayout.CURRENT)
    assert "token_program" in {c.role for c in report.mismatches}
    assert any("neither SPL Token nor Token-2022" in n for n in report.notes)


def test_a_wrong_pda_is_caught():
    observed, data = make_buy()
    corrupted = list(observed)
    corrupted[3] = Pubkey.new_unique()  # bonding curve

    report = compare_against_observed(corrupted, data, BuyLayout.CURRENT)
    assert not report.ok
    assert [c.role for c in report.mismatches] == ["bonding_curve"]
    assert report.mismatches[0].expected is not None


def test_an_unknown_account_count_is_flagged_loudly():
    """A future program version with 17 accounts must not pass quietly."""
    observed, data = make_buy()
    report = compare_against_observed(list(observed) + [Pubkey.new_unique()], data, BuyLayout.CURRENT)

    assert not report.ok
    assert report.detected_layout is None
    assert any("needs updating" in note for note in report.notes)


def test_fee_recipient_is_recorded_but_not_judged():
    """We cannot derive it, so it must be reported as unknown, not as a pass."""
    observed, data = make_buy()
    report = compare_against_observed(observed, data, BuyLayout.CURRENT)

    fee = next(c for c in report.checks if c.role == "fee_recipient")
    assert fee.ok is None
    assert fee.status == "?"
    assert report.fee_recipient == observed[1]


def test_too_few_accounts_does_not_crash():
    report = compare_against_observed([GLOBAL_ACCOUNT], b"\x00" * 24, BuyLayout.CURRENT)
    assert not report.ok
    assert any("too few accounts" in n for n in report.notes)


def test_report_formats_without_error():
    observed, data = make_buy()
    text = format_report(compare_against_observed(observed, data, BuyLayout.CURRENT))
    assert "bonding_curve" in text
    assert "user_volume_accumulator" in text


# --- transaction-shape parsing ---------------------------------------------


def test_find_buy_instruction_in_an_rpc_response():
    observed, data = make_buy()
    keys = [PUMP_FUN_PROGRAM] + list(observed)
    transaction = {
        "slot": 5,
        "transaction": {
            "signatures": ["sig"],
            "message": {
                "accountKeys": [str(k) for k in keys],
                "instructions": [
                    {
                        "programIdIndex": 0,
                        "accounts": list(range(1, len(keys))),
                        "data": base58.b58encode(data).decode(),
                    }
                ],
            },
        },
        "meta": {"innerInstructions": []},
    }
    found = _find_buy_instruction(transaction)
    assert found is not None
    indices, decoded = found
    assert decoded == data
    assert [keys[i] for i in indices] == list(observed)


def test_find_buy_instruction_searches_inner_instructions():
    """Aggregators route buys through CPI; the buy is then an inner instruction."""
    observed, data = make_buy()
    keys = [PUMP_FUN_PROGRAM] + list(observed)
    transaction = {
        "transaction": {
            "signatures": ["sig"],
            "message": {
                "accountKeys": [str(k) for k in keys],
                "instructions": [
                    {"programIdIndex": 1, "accounts": [], "data": base58.b58encode(b"\x00").decode()}
                ],
            },
        },
        "meta": {
            "innerInstructions": [
                {
                    "index": 0,
                    "instructions": [
                        {
                            "programIdIndex": 0,
                            "accounts": list(range(1, len(keys))),
                            "data": base58.b58encode(data).decode(),
                        }
                    ],
                }
            ]
        },
    }
    assert _find_buy_instruction(transaction) is not None


def test_lookup_table_addresses_are_appended_in_runtime_order():
    """v0 transactions resolve writable addresses before readonly ones."""
    from sniper.verify import _account_keys

    static = [Pubkey.new_unique() for _ in range(2)]
    writable = [Pubkey.new_unique()]
    readonly = [Pubkey.new_unique()]
    transaction = {
        "transaction": {"message": {"accountKeys": [str(k) for k in static]}},
        "meta": {
            "loadedAddresses": {
                "writable": [str(k) for k in writable],
                "readonly": [str(k) for k in readonly],
            }
        },
    }
    assert _account_keys(transaction) == static + writable + readonly


def test_decode_ix_data_handles_base58():
    assert _decode_ix_data(base58.b58encode(b"hello").decode()) == b"hello"


def test_non_pumpfun_transaction_yields_nothing():
    transaction = {
        "transaction": {
            "signatures": ["s"],
            "message": {
                "accountKeys": [str(Pubkey.new_unique())],
                "instructions": [
                    {"programIdIndex": 0, "accounts": [], "data": base58.b58encode(b"x").decode()}
                ],
            },
        },
        "meta": {},
    }
    assert _find_buy_instruction(transaction) is None


def test_an_extended_layout_is_distinguished_from_a_reordered_one():
    """Extra accounts appended is an additive fix; reordering is not.

    Conflating them sends you rewriting an account list that was already
    correct, so the report has to tell them apart.
    """
    from solders.pubkey import Pubkey as _P

    observed, data = make_buy()
    extended = list(observed) + [_P.new_unique(), _P.new_unique()]

    report = compare_against_observed(extended, data, BuyLayout.CURRENT)
    assert not report.ok
    assert report.mismatches == []
    assert any("EXTENDED by 2" in note for note in report.notes)
    assert any("not reordered" in note for note in report.notes)


def test_a_reordered_layout_does_not_claim_to_be_merely_extended():
    from solders.pubkey import Pubkey as _P

    observed, data = make_buy()
    broken = list(observed) + [_P.new_unique(), _P.new_unique()]
    broken[3] = _P.new_unique()  # bonding curve wrong -> genuinely reordered

    report = compare_against_observed(broken, data, BuyLayout.CURRENT)
    assert report.mismatches
    assert not any("EXTENDED" in note for note in report.notes)


def test_summarise_groups_samples_by_shape():
    from sniper.verify import summarise

    observed, data = make_buy()
    good = compare_against_observed(observed, data, BuyLayout.CURRENT)
    legacy_obs, legacy_data = make_buy(BuyLayout.LEGACY)
    other = compare_against_observed(legacy_obs, legacy_data, BuyLayout.CURRENT)

    text = summarise([good, good, other])
    assert "2 x" in text and "16 accounts" in text
    assert "1 x" in text and "12 accounts" in text
    assert "matches our encoding" in text and "does NOT match" in text

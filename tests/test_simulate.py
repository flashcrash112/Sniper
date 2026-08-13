"""Simulation verdicts.

The whole value of `simulate` is telling apart "the account list is wrong" from
"the wallet is empty". Both are failures; only one means the code is broken.
"""

from solders.pubkey import Pubkey

from sniper.constants import TOKEN_2022_PROGRAM, TOKEN_PROGRAM
from sniper.simulate import SimulationResult


def result(**kwargs):
    kwargs.setdefault("mint", Pubkey.new_unique())
    return SimulationResult(**kwargs)


def test_success_is_reported_as_success():
    r = result(err=None, logs=["Program ... success"])
    assert r.ok
    assert "SUCCESS" in r.verdict()


def test_an_empty_wallet_failure_means_the_accounts_validated():
    """The expected outcome for an unfunded test wallet — and it is good news."""
    r = result(
        err={"InstructionError": [3, {"Custom": 1}]},
        logs=[
            "Program 11111111111111111111111111111111 invoke [2]",
            "Transfer: insufficient lamports 0, need 250000000",
            "Program 11111111111111111111111111111111 failed",
        ],
    )
    assert not r.ok
    verdict = r.verdict()
    assert "accounts VALIDATED" in verdict
    assert "expected" in verdict


def test_too_few_accounts_is_reported_as_structural():
    """The failure mode we are actually hunting: the 16-vs-18 question."""
    r = result(
        err={"InstructionError": [3, "NotEnoughAccountKeys"]},
        logs=["Program log: AnchorError: NotEnoughAccountKeys"],
    )
    assert "STRUCTURAL FAILURE" in r.verdict()
    assert "more accounts" in r.verdict()


def test_bad_pda_is_reported_as_structural():
    r = result(
        err={"InstructionError": [3, {"Custom": 2006}]},
        logs=["Program log: AnchorError caused by account: bonding_curve. Error Code: ConstraintSeeds"],
    )
    assert "STRUCTURAL FAILURE" in r.verdict()
    assert "PDA" in r.verdict()


def test_wrong_token_program_shows_as_wrong_owner():
    r = result(
        err={"InstructionError": [3, {"Custom": 3007}]},
        logs=["Program log: AnchorError: AccountOwnedByWrongProgram"],
    )
    assert "STRUCTURAL FAILURE" in r.verdict()
    assert "owned by the wrong program" in r.verdict()


def test_bad_instruction_data_is_structural():
    r = result(
        err={"InstructionError": [3, {"Custom": 102}]},
        logs=["Program log: AnchorError: InstructionDidNotDeserialize"],
    )
    assert "STRUCTURAL FAILURE" in r.verdict()
    assert "instruction data" in r.verdict()


def test_a_build_failure_is_reported_before_any_rpc_verdict():
    r = result(build_error="no bonding curve account")
    assert not r.ok
    assert "could not build" in r.verdict()


def test_an_unrecognised_failure_is_not_claimed_to_be_fine():
    """Unknown must not be silently classified as success or as funding."""
    r = result(err={"InstructionError": [3, {"Custom": 6999}]}, logs=["Program log: mystery"])
    verdict = r.verdict()
    assert "non-structural" in verdict
    assert "VALIDATED" not in verdict
    assert "SUCCESS" not in verdict


def test_token_program_is_named():
    assert result(token_program=TOKEN_2022_PROGRAM).token_program_name == "Token-2022"
    assert result(token_program=TOKEN_PROGRAM).token_program_name == "SPL Token (legacy)"
    assert result(token_program=None).token_program_name == "unknown"


# --- the nonexistent-payer case -------------------------------------------


def test_a_nonexistent_fee_payer_is_inconclusive_not_a_pass_or_a_failure():
    """An unfunded wallet does not exist, so nothing runs and nothing is proven."""
    r = result(err="AccountNotFound", logs=[], units_consumed=0)
    verdict = r.verdict()
    assert "INCONCLUSIVE" in verdict
    assert "--payer" in verdict
    assert "STRUCTURAL" not in verdict
    assert "VALIDATED" not in verdict


def test_accountnotfound_with_logs_is_not_treated_as_the_payer_case():
    """Mid-execution AccountNotFound is a genuine structural problem."""
    r = result(
        err={"InstructionError": [3, "AccountNotFound"]},
        logs=["Program 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P invoke [1]"],
        units_consumed=4200,
    )
    assert "INCONCLUSIVE" not in r.verdict()


def test_building_for_a_foreign_payer_produces_a_valid_unsigned_transaction():
    """Simulation runs with sigVerify off, so we can compile for any address."""
    from solders.hash import Hash
    from solders.keypair import Keypair

    from sniper.buyer import Buyer
    from sniper.config import BuyConfig, RpcConfig, SafetyConfig
    from sniper.curve import CurveState
    from sniper.pumpfun import LaunchEvent, derive_associated_token_account
    from sniper.simulate import build_unsigned_for_simulation

    buyer = Buyer(
        keypair=Keypair(),
        rpc=None,
        buy_cfg=BuyConfig(amount_sol=0.25, slippage_bps=1000, priority_fee_microlamports=1),
        rpc_cfg=RpcConfig(http_url="x", send_urls=["x"]),
        safety_cfg=SafetyConfig(kill_switch_file="/tmp/K", max_total_spend_sol=1.0),
        dry_run=True,
    )
    buyer._fee_recipient = Pubkey.new_unique()
    event = LaunchEvent(
        mint=Pubkey.new_unique(), name="n", symbol="T", uri="",
        creator=Pubkey.new_unique(), curve=CurveState.initial(),
    )
    foreign = Pubkey.new_unique()
    tx = build_unsigned_for_simulation(
        buyer, event, buyer.quote(event), Hash.default(), foreign
    )

    keys = list(tx.message.account_keys)
    assert keys[0] == foreign, "the foreign address must be the fee payer"
    buy = tx.message.instructions[-1]
    # The buyer-specific accounts must follow the payer, not our own wallet.
    assert keys[buy.accounts[6]] == foreign
    assert keys[buy.accounts[5]] == derive_associated_token_account(
        foreign, event.mint, buyer.cfg.token_program
    )
    assert buyer.pubkey not in keys, "our wallet must not appear at all"
    assert len(buy.accounts) == 16


def test_foreign_payer_build_still_uses_the_right_token_program():
    from solders.hash import Hash
    from solders.keypair import Keypair

    from sniper.buyer import Buyer
    from sniper.config import BuyConfig, RpcConfig, SafetyConfig
    from sniper.curve import CurveState
    from sniper.pumpfun import LaunchEvent
    from sniper.simulate import build_unsigned_for_simulation

    buyer = Buyer(
        keypair=Keypair(), rpc=None,
        buy_cfg=BuyConfig(amount_sol=0.25, slippage_bps=1000, priority_fee_microlamports=1),
        rpc_cfg=RpcConfig(http_url="x", send_urls=["x"]),
        safety_cfg=SafetyConfig(kill_switch_file="/tmp/K", max_total_spend_sol=1.0),
        dry_run=True,
    )
    buyer._fee_recipient = Pubkey.new_unique()
    event = LaunchEvent(
        mint=Pubkey.new_unique(), name="n", symbol="T", uri="",
        creator=Pubkey.new_unique(), curve=CurveState.initial(),
        token_program=TOKEN_2022_PROGRAM,
    )
    tx = build_unsigned_for_simulation(
        buyer, event, buyer.quote(event), Hash.default(), Pubkey.new_unique()
    )
    keys = list(tx.message.account_keys)
    assert keys[tx.message.instructions[-1].accounts[8]] == TOKEN_2022_PROGRAM

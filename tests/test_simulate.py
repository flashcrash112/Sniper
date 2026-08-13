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

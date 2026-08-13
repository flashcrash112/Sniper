"""On-chain Anchor IDL fetching and decoding."""

import json
import struct
import zlib

import pytest
from solders.pubkey import Pubkey

from sniper.constants import PUMP_FUN_PROGRAM
from sniper.idl import (
    decode_idl_account,
    idl_address,
    instruction_accounts,
    instruction_args,
    summarise_idl,
)

SAMPLE = {
    "version": "0.1.0",
    "name": "pump",
    "instructions": [
        {
            "name": "buy",
            "accounts": [
                {"name": "global"},
                {"name": "feeRecipient"},
                {"name": "mint"},
                # A nested group, which Anchor flattens in order.
                {"name": "curveAccounts", "accounts": [
                    {"name": "bondingCurve"},
                    {"name": "associatedBondingCurve"},
                ]},
                {"name": "associatedUser"},
            ],
            "args": [
                {"name": "amount", "type": "u64"},
                {"name": "maxSolCost", "type": "u64"},
            ],
        },
        {"name": "sell", "accounts": [{"name": "global"}], "args": []},
    ],
}


def build_account(document: dict, authority: bytes = b"\x01" * 32) -> bytes:
    payload = zlib.compress(json.dumps(document).encode())
    return b"\x00" * 8 + authority + struct.pack("<I", len(payload)) + payload


def test_idl_address_is_deterministic_and_matches_anchors_scheme():
    address = idl_address(PUMP_FUN_PROGRAM)
    assert address == idl_address(PUMP_FUN_PROGRAM)
    base = Pubkey.find_program_address([], PUMP_FUN_PROGRAM)[0]
    assert address == Pubkey.create_with_seed(base, "anchor:idl", PUMP_FUN_PROGRAM)


def test_decode_round_trip():
    assert decode_idl_account(build_account(SAMPLE)) == SAMPLE


def test_decode_tolerates_a_zero_length_header():
    """Some programs write the buffer without setting the length field."""
    payload = zlib.compress(json.dumps(SAMPLE).encode())
    raw = b"\x00" * 8 + b"\x01" * 32 + struct.pack("<I", 0) + payload
    assert decode_idl_account(raw) == SAMPLE


@pytest.mark.parametrize(
    "data", [b"", b"\x00" * 10, b"\x00" * 44, b"\x00" * 44 + b"not compressed"]
)
def test_decode_returns_none_for_junk_rather_than_raising(data):
    """A collision or a different account version must read as 'no IDL'."""
    assert decode_idl_account(data) is None


def test_instruction_accounts_flattens_nested_groups_in_order():
    assert instruction_accounts(SAMPLE, "buy") == [
        "global",
        "feeRecipient",
        "mint",
        "bondingCurve",
        "associatedBondingCurve",
        "associatedUser",
    ]


def test_instruction_lookup_is_case_insensitive():
    assert instruction_accounts(SAMPLE, "BUY") == instruction_accounts(SAMPLE, "buy")


def test_missing_instruction_returns_none():
    assert instruction_accounts(SAMPLE, "migrate") is None
    assert instruction_args(SAMPLE, "migrate") is None


def test_instruction_args():
    assert instruction_args(SAMPLE, "buy") == [("amount", "u64"), ("maxSolCost", "u64")]


def test_summary_numbers_the_accounts_so_they_can_be_compared_by_index():
    text = summarise_idl(SAMPLE)
    assert "buy — 6 accounts, 2 args" in text
    assert " 0  global" in text
    assert " 4  associatedBondingCurve" in text
    assert "amount: u64" in text

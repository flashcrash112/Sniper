import base64

import pytest
from solders.pubkey import Pubkey

from conftest import (
    make_cpi_event_instruction_data,
    make_create_event_payload,
    make_program_data_log,
)
from sniper.constants import BUY_IX_DISCRIMINATOR, CREATE_EVENT_DISCRIMINATOR
from sniper.listener.scan import scan_instructions, scan_logs
from sniper.pumpfun import parse_create_event, parse_global_account, parse_instruction_data


def test_parse_create_event_full():
    mint = Pubkey.new_unique()
    creator = Pubkey.new_unique()
    payload = make_create_event_payload(
        name="Very Real Coin", symbol="REAL", mint=mint, creator=creator
    )
    event = parse_create_event(payload)

    assert event.mint == mint
    assert event.symbol == "REAL"
    assert event.name == "Very Real Coin"
    assert event.creator == creator
    assert event.curve is not None
    assert event.curve.virtual_sol_reserves == 30_000_000_000


def test_parse_create_event_without_the_appended_tail():
    """Older program versions emitted a shorter event; it must still parse."""
    user = Pubkey.new_unique()
    payload = make_create_event_payload(user=user, include_tail=False)
    event = parse_create_event(payload)

    assert event.symbol == "TEST"
    assert event.creator == user  # falls back to the payer
    assert event.curve is None


def test_parse_instruction_data_accepts_the_cpi_wrapper():
    data = make_cpi_event_instruction_data(symbol="WRAP")
    event = parse_instruction_data(data)
    assert event is not None and event.symbol == "WRAP"


def test_parse_instruction_data_accepts_a_bare_event():
    data = CREATE_EVENT_DISCRIMINATOR + make_create_event_payload(symbol="BARE")
    event = parse_instruction_data(data)
    assert event is not None and event.symbol == "BARE"


def test_parse_instruction_data_ignores_other_instructions():
    assert parse_instruction_data(BUY_IX_DISCRIMINATOR + b"\x00" * 16) is None
    assert parse_instruction_data(b"") is None
    assert parse_instruction_data(b"\x01\x02") is None


def test_truncated_event_raises_rather_than_returning_garbage():
    data = make_cpi_event_instruction_data()[:40]
    with pytest.raises(ValueError):
        parse_instruction_data(data)


def test_implausible_string_length_is_rejected():
    """A corrupt frame must not turn into a multi-gigabyte allocation."""
    import struct

    payload = struct.pack("<I", 2**31) + b"junk"
    with pytest.raises(ValueError, match="implausible"):
        parse_create_event(payload)


def test_scan_logs_finds_the_create_among_noise():
    logs = [
        "Program 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P invoke [1]",
        "Program log: Instruction: Create",
        "Program data: " + base64.b64encode(b"not an event").decode(),
        make_program_data_log(symbol="FOUND"),
        "Program 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P success",
    ]
    event = scan_logs(logs)
    assert event is not None and event.symbol == "FOUND"


def test_scan_logs_returns_none_without_a_create():
    assert scan_logs(["Program log: Instruction: Buy", "Program log: nope"]) is None


def test_scan_instructions_defers_account_key_resolution():
    """Non-create instructions must not pay to decode account keys."""
    calls = []

    def keys():
        calls.append(1)
        return [Pubkey.new_unique() for _ in range(20)]

    instructions = [
        (0, [0, 1], BUY_IX_DISCRIMINATOR + b"\x00" * 16),
        (0, [0, 1], b"\x05" * 32),
    ]
    assert scan_instructions(keys, instructions) is None
    assert calls == [], "account keys were resolved for non-create instructions"


def test_scan_instructions_finds_a_create():
    keys = [Pubkey.new_unique() for _ in range(10)]
    instructions = [
        (0, [0, 1], BUY_IX_DISCRIMINATOR + b"\x00" * 16),
        (0, list(range(10)), make_cpi_event_instruction_data(symbol="HIT")),
    ]
    event = scan_instructions(keys, instructions)
    assert event is not None and event.symbol == "HIT"


def test_parse_global_account():
    import struct

    authority = Pubkey.new_unique()
    fee_recipient = Pubkey.new_unique()
    data = (
        b"\x00" * 8
        + b"\x01"
        + bytes(authority)
        + bytes(fee_recipient)
        + struct.pack("<Q", 1_073_000_000_000_000)
        + struct.pack("<Q", 30_000_000_000)
        + struct.pack("<Q", 793_100_000_000_000)
        + struct.pack("<Q", 1_000_000_000_000_000)
        + struct.pack("<Q", 100)
        + b"\xff" * 200  # fields appended by later program versions
    )
    state = parse_global_account(data)
    assert state.fee_recipient == fee_recipient
    assert state.curve.fee_basis_points == 100
    assert state.curve.virtual_sol_reserves == 30_000_000_000

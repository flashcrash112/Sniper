import struct

from solders.pubkey import Pubkey

from sniper.constants import ANCHOR_CPI_EVENT_PREFIX, CREATE_EVENT_DISCRIMINATOR


def borsh_string(value: str) -> bytes:
    raw = value.encode("utf-8")
    return struct.pack("<I", len(raw)) + raw


def make_create_event_payload(
    name: str = "Test Coin",
    symbol: str = "TEST",
    uri: str = "https://ipfs.io/ipfs/QmTest",
    mint: Pubkey = None,
    bonding_curve: Pubkey = None,
    user: Pubkey = None,
    creator: Pubkey = None,
    include_tail: bool = True,
) -> bytes:
    """Build a borsh CreateEvent body exactly as the program emits it."""
    mint = mint or Pubkey.new_unique()
    bonding_curve = bonding_curve or Pubkey.new_unique()
    user = user or Pubkey.new_unique()
    creator = creator or user

    payload = bytearray()
    payload += borsh_string(name)
    payload += borsh_string(symbol)
    payload += borsh_string(uri)
    payload += bytes(mint)
    payload += bytes(bonding_curve)
    payload += bytes(user)
    if include_tail:
        payload += bytes(creator)
        payload += struct.pack("<q", 1_700_000_000)
        payload += struct.pack("<Q", 1_073_000_000_000_000)
        payload += struct.pack("<Q", 30_000_000_000)
        payload += struct.pack("<Q", 793_100_000_000_000)
        payload += struct.pack("<Q", 1_000_000_000_000_000)
    return bytes(payload)


def make_cpi_event_instruction_data(**kwargs) -> bytes:
    """Wrap a CreateEvent the way Anchor's `emit_cpi!` does."""
    return (
        ANCHOR_CPI_EVENT_PREFIX
        + CREATE_EVENT_DISCRIMINATOR
        + make_create_event_payload(**kwargs)
    )


def make_program_data_log(**kwargs) -> str:
    """The `Program data:` log line Anchor's `emit!` writes."""
    import base64

    payload = CREATE_EVENT_DISCRIMINATOR + make_create_event_payload(**kwargs)
    return "Program data: " + base64.b64encode(payload).decode("ascii")

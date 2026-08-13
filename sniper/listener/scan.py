"""Extracting launches from whatever shape a feed hands us.

Three feeds, three encodings, one job: find the pump.fun create in a
transaction and turn it into a :class:`LaunchEvent`. Both entry points are on
the hot path and both bail out as early as they can — a `startswith` on a log
line, an 8-byte discriminator compare on instruction data — because the
overwhelming majority of pump.fun traffic is buys and sells we do not care
about.
"""

from __future__ import annotations

import base64
from typing import Callable, Iterable, Optional, Sequence

from solders.pubkey import Pubkey

from ..constants import (
    ANCHOR_CPI_EVENT_PREFIX,
    CREATE_EVENT_DISCRIMINATOR,
    CREATE_IX_DISCRIMINATOR,
)
from ..pumpfun import LaunchEvent, parse_instruction_data

_PROGRAM_DATA_PREFIX = "Program data: "


def scan_logs(logs: Iterable[str]) -> Optional[LaunchEvent]:
    """Find a create event in a transaction's log messages.

    Anchor's `emit!` writes the event as ``Program data: <base64>``. Anything
    that is not a create decodes to a different discriminator and is skipped.
    """
    for line in logs:
        if not line.startswith(_PROGRAM_DATA_PREFIX):
            continue
        encoded = line[len(_PROGRAM_DATA_PREFIX) :]
        try:
            raw = base64.b64decode(encoded, validate=False)
        except Exception:
            continue
        event = parse_instruction_data(raw)
        if event is not None:
            return event
    return None


_CREATE_PREFIXES = (
    ANCHOR_CPI_EVENT_PREFIX,
    CREATE_EVENT_DISCRIMINATOR,
    CREATE_IX_DISCRIMINATOR,
)


def scan_instructions(
    account_keys: Sequence[Pubkey] | Callable[[], Sequence[Pubkey]],
    instructions: Iterable[tuple[int, Sequence[int], bytes]],
) -> Optional[LaunchEvent]:
    """Find a create among a transaction's instructions.

    Each instruction is ``(program_id_index, account_indices, data)``, matching
    the compiled-message shape every feed exposes.

    `account_keys` may be a callable, and callers on a high-volume feed should
    pass one: the keys are only needed for the raw-`create`-instruction branch,
    and deferring the call means the thousands of buys and sells per second
    never pay to decode ~30 pubkeys just to be discarded on the next line.
    """
    for _program_index, account_indices, data in instructions:
        # Cheapest possible reject: an 8-byte prefix compare, before any
        # pubkey decoding or borsh parsing.
        if len(data) < 8 or data[:8] not in _CREATE_PREFIXES:
            continue
        keys = account_keys() if callable(account_keys) else account_keys
        accounts = [keys[i] for i in account_indices if 0 <= i < len(keys)]
        try:
            event = parse_instruction_data(data, accounts)
        except ValueError:
            # A malformed frame should drop one candidate, not the stream.
            continue
        if event is not None:
            return event
    return None

"""pump.fun protocol layer: PDAs, instruction encoding, and event decoding.

Split into three concerns:

* :class:`LaunchEvent` + the ``parse_*`` helpers — turn raw instruction bytes
  from the feed into a typed launch.
* :func:`derive_*` — PDA derivation. Cheap (a handful of sha256 rounds in
  Rust) but not free, so the hot path derives only what is mint-specific.
* :func:`build_buy_instruction` — encode the buy.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, replace
from enum import Enum
from typing import Iterable, Optional

from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey

from .constants import (
    ANCHOR_CPI_EVENT_PREFIX,
    ASSOCIATED_TOKEN_PROGRAM,
    BONDING_CURVE_SEED,
    BUY_IX_DISCRIMINATOR,
    CREATE_EVENT_DISCRIMINATOR,
    CREATE_IX_DISCRIMINATOR,
    CREATOR_VAULT_SEED,
    DEFAULT_CREATOR_FEE_BASIS_POINTS,
    EVENT_AUTHORITY,
    FEE_CONFIG,
    GLOBAL_ACCOUNT,
    GLOBAL_VOLUME_ACCUMULATOR,
    PUMP_FUN_FEE_PROGRAM,
    PUMP_FUN_PROGRAM,
    SELL_IX_DISCRIMINATOR,
    SYSTEM_PROGRAM,
    TOKEN_PROGRAM,
    USER_VOLUME_ACCUMULATOR_SEED,
)
from .curve import CurveState

# --- Borsh reading ---------------------------------------------------------


class _Reader:
    """Minimal borsh cursor. Raises ValueError on truncation."""

    __slots__ = ("_buf", "_pos")

    def __init__(self, buf: bytes, pos: int = 0) -> None:
        self._buf = buf
        self._pos = pos

    @property
    def remaining(self) -> int:
        return len(self._buf) - self._pos

    def take(self, n: int) -> bytes:
        if self.remaining < n:
            raise ValueError(f"truncated: need {n} bytes, have {self.remaining}")
        chunk = self._buf[self._pos : self._pos + n]
        self._pos += n
        return chunk

    def u8(self) -> int:
        return self.take(1)[0]

    def u32(self) -> int:
        return struct.unpack_from("<I", self.take(4))[0]

    def u64(self) -> int:
        return struct.unpack_from("<Q", self.take(8))[0]

    def i64(self) -> int:
        return struct.unpack_from("<q", self.take(8))[0]

    def string(self) -> str:
        length = self.u32()
        if length > 4096:
            # Guards against a malformed feed frame turning into a huge alloc.
            raise ValueError(f"implausible string length {length}")
        return self.take(length).decode("utf-8", errors="replace")

    def pubkey(self) -> Pubkey:
        return Pubkey.from_bytes(self.take(32))


# --- Launch event ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LaunchEvent:
    """A newly created pump.fun coin, as seen on the wire."""

    mint: Pubkey
    name: str
    symbol: str
    uri: str
    creator: Pubkey
    bonding_curve: Optional[Pubkey] = None
    user: Optional[Pubkey] = None
    timestamp: Optional[int] = None
    signature: Optional[str] = None
    slot: Optional[int] = None
    curve: Optional[CurveState] = None
    """Reserves as reported by the create event, when the feed supplies them."""

    received_ns: int = 0
    """`time.perf_counter_ns()` at the moment the frame was read off the socket."""

    token_program: Optional[Pubkey] = None
    """The mint's token program, when the feed reveals it.

    Not in the create event, so it is only known on feeds that hand us the
    transaction's account list. None means "fall back to the configured
    default"."""

    def with_context(
        self,
        *,
        signature: Optional[str] = None,
        slot: Optional[int] = None,
        received_ns: int = 0,
    ) -> "LaunchEvent":
        """Attach the transport-level fields the parser cannot know about."""
        return replace(
            self, signature=signature, slot=slot, received_ns=received_ns
        )

    def summary(self) -> dict:
        return {
            "mint": str(self.mint),
            "symbol": self.symbol,
            "name": self.name,
            "uri": self.uri,
            "creator": str(self.creator),
            "slot": self.slot,
            "signature": self.signature,
        }


def parse_create_event(payload: bytes) -> LaunchEvent:
    """Decode a borsh ``CreateEvent`` body (discriminator already stripped).

    Field order per the pump.fun IDL::

        name, symbol, uri, mint, bonding_curve, user,
        creator, timestamp, virtual_token_reserves, virtual_sol_reserves,
        real_token_reserves, token_total_supply

    Everything from ``creator`` on was appended in later program versions, so
    the tail is decoded opportunistically — an older node replaying an older
    event still yields a usable launch instead of an exception.
    """
    r = _Reader(payload)
    name = r.string()
    symbol = r.string()
    uri = r.string()
    mint = r.pubkey()
    bonding_curve = r.pubkey()
    user = r.pubkey()

    creator = user
    timestamp: Optional[int] = None
    curve: Optional[CurveState] = None
    if r.remaining >= 32:
        creator = r.pubkey()
    if r.remaining >= 8:
        timestamp = r.i64()
    if r.remaining >= 32:
        vtok = r.u64()
        vsol = r.u64()
        rtok = r.u64()
        r.u64()  # token_total_supply, unused for quoting
        curve = CurveState(
            virtual_token_reserves=vtok,
            virtual_sol_reserves=vsol,
            real_token_reserves=rtok,
        )

    return LaunchEvent(
        mint=mint,
        name=name,
        symbol=symbol,
        uri=uri,
        creator=creator,
        bonding_curve=bonding_curve,
        user=user,
        timestamp=timestamp,
        curve=curve,
    )


def parse_create_instruction(data: bytes, accounts: Iterable[Pubkey]) -> LaunchEvent:
    """Decode a top-level ``create`` instruction.

    Only used when the feed gives us instruction data without the self-CPI
    event (the event path is preferred, since it is self-contained). The mint
    is account 0 and the payer is account 7 in the create account list.
    """
    r = _Reader(data, pos=8)
    name = r.string()
    symbol = r.string()
    uri = r.string()
    creator: Optional[Pubkey] = None
    if r.remaining >= 32:
        creator = r.pubkey()

    keys = list(accounts)
    if not keys:
        raise ValueError("create instruction has no accounts")
    mint = keys[0]
    user = keys[7] if len(keys) > 7 else None
    return LaunchEvent(
        mint=mint,
        name=name,
        symbol=symbol,
        uri=uri,
        creator=creator or user or mint,
        bonding_curve=keys[2] if len(keys) > 2 else None,
        user=user,
    )


def parse_instruction_data(
    data: bytes, accounts: Optional[Iterable[Pubkey]] = None
) -> Optional[LaunchEvent]:
    """Return a :class:`LaunchEvent` if `data` is a pump.fun create, else None.

    Handles both shapes a launch can arrive in:

    * the Anchor self-CPI event (``ANCHOR_CPI_EVENT_PREFIX || CreateEvent || body``),
      which carries the mint inline, and
    * the raw ``create`` instruction, which needs the account list.

    This is the single hottest parser in the process — it runs against every
    pump.fun instruction on the feed — so it rejects on an 8-byte prefix
    compare before doing any real work.
    """
    if len(data) < 8:
        return None

    head = data[:8]
    if head == ANCHOR_CPI_EVENT_PREFIX:
        if len(data) < 16 or data[8:16] != CREATE_EVENT_DISCRIMINATOR:
            return None
        return parse_create_event(data[16:])
    if head == CREATE_EVENT_DISCRIMINATOR:
        # Some feeds hand back the event payload with the CPI prefix stripped.
        return parse_create_event(data[8:])
    if head == CREATE_IX_DISCRIMINATOR:
        if accounts is None:
            return None
        return parse_create_instruction(data, accounts)
    return None


# --- Global account --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GlobalState:
    """The subset of the pump.fun `global` account that a buy depends on."""

    fee_recipient: Pubkey
    curve: CurveState


def parse_global_account(data: bytes) -> GlobalState:
    """Decode the stable prefix of the `global` account.

    Layout::

        8   discriminator
        1   initialized
        32  authority
        32  fee_recipient
        8   initial_virtual_token_reserves
        8   initial_virtual_sol_reserves
        8   initial_real_token_reserves
        8   token_total_supply
        8   fee_basis_points
        ... later additions, ignored

    Trailing fields have been appended over time; we stop at the point the
    layout is known to be stable so a future append does not break startup.
    """
    r = _Reader(data, pos=8)
    r.u8()  # initialized
    r.pubkey()  # authority
    fee_recipient = r.pubkey()
    vtok = r.u64()
    vsol = r.u64()
    rtok = r.u64()
    r.u64()  # token_total_supply
    fee_bps = r.u64()

    creator_fee_bps = DEFAULT_CREATOR_FEE_BASIS_POINTS
    return GlobalState(
        fee_recipient=fee_recipient,
        curve=CurveState(
            virtual_token_reserves=vtok,
            virtual_sol_reserves=vsol,
            real_token_reserves=rtok,
            fee_basis_points=fee_bps,
            creator_fee_basis_points=creator_fee_bps,
        ),
    )


# --- PDA derivation --------------------------------------------------------


def derive_bonding_curve(mint: Pubkey) -> Pubkey:
    return Pubkey.find_program_address(
        [BONDING_CURVE_SEED, bytes(mint)], PUMP_FUN_PROGRAM
    )[0]


def derive_creator_vault(creator: Pubkey) -> Pubkey:
    return Pubkey.find_program_address(
        [CREATOR_VAULT_SEED, bytes(creator)], PUMP_FUN_PROGRAM
    )[0]


def derive_user_volume_accumulator(user: Pubkey) -> Pubkey:
    return Pubkey.find_program_address(
        [USER_VOLUME_ACCUMULATOR_SEED, bytes(user)], PUMP_FUN_PROGRAM
    )[0]


def derive_associated_token_account(
    owner: Pubkey, mint: Pubkey, token_program: Pubkey = TOKEN_PROGRAM
) -> Pubkey:
    """ATA derivation that also works for PDA owners (the bonding curve).

    `token_program` is one of the derivation seeds, so a mint created under
    Token-2022 has a completely different ATA from the same mint under the
    legacy SPL Token program. Passing the wrong one yields a valid-looking
    address that the runtime will reject.
    """
    return Pubkey.find_program_address(
        [bytes(owner), bytes(token_program), bytes(mint)],
        ASSOCIATED_TOKEN_PROGRAM,
    )[0]


def build_create_ata_idempotent_instruction(
    payer: Pubkey,
    owner: Pubkey,
    mint: Pubkey,
    ata: Pubkey,
    token_program: Pubkey = TOKEN_PROGRAM,
) -> Instruction:
    """`CreateIdempotent` on the associated-token program.

    Idempotent matters here: if the wallet already holds the ATA (a re-run, or
    a second buy of the same mint), a plain `Create` would fail the whole
    transaction. This variant is a no-op instead.
    """
    return Instruction(
        program_id=ASSOCIATED_TOKEN_PROGRAM,
        data=bytes([1]),
        accounts=[
            AccountMeta(payer, is_signer=True, is_writable=True),
            AccountMeta(ata, is_signer=False, is_writable=True),
            AccountMeta(owner, is_signer=False, is_writable=False),
            AccountMeta(mint, is_signer=False, is_writable=False),
            AccountMeta(SYSTEM_PROGRAM, is_signer=False, is_writable=False),
            AccountMeta(token_program, is_signer=False, is_writable=False),
        ],
    )


# --- Buy instruction -------------------------------------------------------


class BuyLayout(str, Enum):
    """Which revision of the `buy` account list to encode.

    pump.fun has appended accounts to `buy` several times. `CURRENT` is the
    live layout; `LEGACY` is the pre-volume-accumulator one, kept so a rollback
    on their side does not mean a code change on ours.
    """

    CURRENT = "current"
    LEGACY = "legacy"


@dataclass(frozen=True, slots=True)
class BuyAccounts:
    """Everything a buy touches, resolved."""

    fee_recipient: Pubkey
    mint: Pubkey
    bonding_curve: Pubkey
    associated_bonding_curve: Pubkey
    associated_user: Pubkey
    user: Pubkey
    creator_vault: Pubkey
    user_volume_accumulator: Pubkey
    token_program: Pubkey = TOKEN_PROGRAM
    """Which token program the mint belongs to.

    pump.fun now creates mints under Token-2022. This sits at account 8 and is
    also a seed of both associated token accounts, so getting it wrong makes
    three accounts wrong at once.
    """


def build_buy_instruction(
    accounts: BuyAccounts,
    token_amount: int,
    max_sol_cost: int,
    layout: BuyLayout = BuyLayout.CURRENT,
    track_volume: bool = False,
) -> Instruction:
    """Encode the pump.fun `buy` instruction.

    `token_amount` is in base units (6 decimals) and `max_sol_cost` is the
    lamport ceiling the program checks the realised cost against; both come
    from :func:`sniper.curve.quote_buy`.
    """
    data = bytearray(BUY_IX_DISCRIMINATOR)
    data += struct.pack("<QQ", token_amount, max_sol_cost)

    metas = [
        AccountMeta(GLOBAL_ACCOUNT, is_signer=False, is_writable=False),
        AccountMeta(accounts.fee_recipient, is_signer=False, is_writable=True),
        AccountMeta(accounts.mint, is_signer=False, is_writable=False),
        AccountMeta(accounts.bonding_curve, is_signer=False, is_writable=True),
        AccountMeta(
            accounts.associated_bonding_curve, is_signer=False, is_writable=True
        ),
        AccountMeta(accounts.associated_user, is_signer=False, is_writable=True),
        AccountMeta(accounts.user, is_signer=True, is_writable=True),
        AccountMeta(SYSTEM_PROGRAM, is_signer=False, is_writable=False),
        AccountMeta(accounts.token_program, is_signer=False, is_writable=False),
        AccountMeta(accounts.creator_vault, is_signer=False, is_writable=True),
        AccountMeta(EVENT_AUTHORITY, is_signer=False, is_writable=False),
        AccountMeta(PUMP_FUN_PROGRAM, is_signer=False, is_writable=False),
    ]

    if layout is BuyLayout.CURRENT:
        # `track_volume` is an Option<bool>: 0x00 = None, 0x01 0x01 = Some(true).
        data += b"\x01\x01" if track_volume else b"\x00"
        metas += [
            AccountMeta(GLOBAL_VOLUME_ACCUMULATOR, is_signer=False, is_writable=True),
            AccountMeta(
                accounts.user_volume_accumulator, is_signer=False, is_writable=True
            ),
            AccountMeta(FEE_CONFIG, is_signer=False, is_writable=False),
            AccountMeta(PUMP_FUN_FEE_PROGRAM, is_signer=False, is_writable=False),
        ]

    return Instruction(
        program_id=PUMP_FUN_PROGRAM, data=bytes(data), accounts=metas
    )


@dataclass(frozen=True, slots=True)
class SellAccounts:
    """Accounts for a sell. Note the ordering differs from a buy."""

    fee_recipient: Pubkey
    mint: Pubkey
    bonding_curve: Pubkey
    associated_bonding_curve: Pubkey
    associated_user: Pubkey
    user: Pubkey
    creator_vault: Pubkey
    token_program: Pubkey = TOKEN_PROGRAM


def build_sell_instruction(
    accounts: SellAccounts,
    token_amount: int,
    min_sol_output: int,
    layout: BuyLayout = BuyLayout.CURRENT,
) -> Instruction:
    """Encode the pump.fun `sell` instruction.

    The account list is *not* the buy list with the tail removed: `sell` puts
    `creator_vault` at index 8 and `token_program` at 9, where `buy` has them
    the other way around, and it has no volume accumulators. Getting this wrong
    fails with a seeds-constraint error that looks like a PDA bug.
    """
    data = bytearray(SELL_IX_DISCRIMINATOR)
    data += struct.pack("<QQ", token_amount, min_sol_output)

    metas = [
        AccountMeta(GLOBAL_ACCOUNT, is_signer=False, is_writable=False),
        AccountMeta(accounts.fee_recipient, is_signer=False, is_writable=True),
        AccountMeta(accounts.mint, is_signer=False, is_writable=False),
        AccountMeta(accounts.bonding_curve, is_signer=False, is_writable=True),
        AccountMeta(
            accounts.associated_bonding_curve, is_signer=False, is_writable=True
        ),
        AccountMeta(accounts.associated_user, is_signer=False, is_writable=True),
        AccountMeta(accounts.user, is_signer=True, is_writable=True),
        AccountMeta(SYSTEM_PROGRAM, is_signer=False, is_writable=False),
        AccountMeta(accounts.creator_vault, is_signer=False, is_writable=True),
        AccountMeta(accounts.token_program, is_signer=False, is_writable=False),
        AccountMeta(EVENT_AUTHORITY, is_signer=False, is_writable=False),
        AccountMeta(PUMP_FUN_PROGRAM, is_signer=False, is_writable=False),
    ]

    if layout is BuyLayout.CURRENT:
        metas += [
            AccountMeta(FEE_CONFIG, is_signer=False, is_writable=False),
            AccountMeta(PUMP_FUN_FEE_PROGRAM, is_signer=False, is_writable=False),
        ]

    return Instruction(program_id=PUMP_FUN_PROGRAM, data=bytes(data), accounts=metas)


# --- Bonding curve account -------------------------------------------------


@dataclass(frozen=True, slots=True)
class BondingCurveState:
    """The live reserves of one coin's curve, read from its account."""

    virtual_token_reserves: int
    virtual_sol_reserves: int
    real_token_reserves: int
    real_sol_reserves: int
    token_total_supply: int
    complete: bool
    creator: Optional[Pubkey] = None

    def to_curve(self, fee_basis_points: int, creator_fee_basis_points: int) -> CurveState:
        return CurveState(
            virtual_token_reserves=self.virtual_token_reserves,
            virtual_sol_reserves=self.virtual_sol_reserves,
            real_token_reserves=self.real_token_reserves,
            fee_basis_points=fee_basis_points,
            creator_fee_basis_points=creator_fee_basis_points,
        )


def parse_bonding_curve(data: bytes) -> BondingCurveState:
    """Decode a `BondingCurve` account.

    Layout::

        8   discriminator
        8   virtual_token_reserves
        8   virtual_sol_reserves
        8   real_token_reserves
        8   real_sol_reserves
        8   token_total_supply
        1   complete
        32  creator          (appended in a later version)

    `complete` means the curve has filled and migrated to a DEX — at which
    point pump.fun trades on it revert, and any exit has to go through the
    pool instead.
    """
    r = _Reader(data, pos=8)
    state = dict(
        virtual_token_reserves=r.u64(),
        virtual_sol_reserves=r.u64(),
        real_token_reserves=r.u64(),
        real_sol_reserves=r.u64(),
        token_total_supply=r.u64(),
        complete=bool(r.u8()),
    )
    creator = r.pubkey() if r.remaining >= 32 else None
    return BondingCurveState(**state, creator=creator)

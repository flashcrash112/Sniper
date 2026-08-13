"""On-chain constants for the pump.fun program and the SPL programs it touches.

Everything here is static and resolved once at import time so the hot path
(match -> signed transaction) never pays for a string parse or a PDA lookup it
could have avoided.

NOTE: pump.fun is a live, upgradeable program. The account *ordering* of the
`buy` instruction has changed several times (creator_vault, volume accumulators
and the fee config were all appended over time). If buys start failing with
`AccountNotEnoughKeys`, `ConstraintSeeds` or a raw `InstructionDidNotDeserialize`,
re-check the account list in `pumpfun.build_buy_instruction` against the current
IDL before blaming the network. See README -> "Keeping up with program changes".
"""

from __future__ import annotations

from solders.pubkey import Pubkey

# --- Program IDs -----------------------------------------------------------

PUMP_FUN_PROGRAM = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
PUMP_FUN_FEE_PROGRAM = Pubkey.from_string("pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ")

SYSTEM_PROGRAM = Pubkey.from_string("11111111111111111111111111111111")
TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
ASSOCIATED_TOKEN_PROGRAM = Pubkey.from_string(
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
)
RENT_SYSVAR = Pubkey.from_string("SysvarRent111111111111111111111111111111111")
COMPUTE_BUDGET_PROGRAM = Pubkey.from_string("ComputeBudget111111111111111111111111111111")

# --- Anchor discriminators -------------------------------------------------
# sha256("global:<ix_name>")[:8] for instructions,
# sha256("event:<EventName>")[:8] for emitted events.

CREATE_IX_DISCRIMINATOR = bytes([24, 30, 200, 40, 5, 28, 7, 119])
BUY_IX_DISCRIMINATOR = bytes([102, 6, 61, 18, 1, 218, 235, 234])
SELL_IX_DISCRIMINATOR = bytes([51, 230, 133, 164, 1, 127, 131, 173])

CREATE_EVENT_DISCRIMINATOR = bytes([27, 114, 169, 77, 222, 235, 99, 118])
TRADE_EVENT_DISCRIMINATOR = bytes([189, 219, 127, 211, 78, 230, 97, 238])

# Anchor wraps emitted events in a self-CPI whose instruction data is
# ANCHOR_CPI_EVENT_PREFIX || <event discriminator> || <borsh payload>.
ANCHOR_CPI_EVENT_PREFIX = bytes([228, 69, 165, 46, 81, 203, 154, 29])

# --- PDA seeds -------------------------------------------------------------

GLOBAL_SEED = b"global"
BONDING_CURVE_SEED = b"bonding-curve"
CREATOR_VAULT_SEED = b"creator-vault"
EVENT_AUTHORITY_SEED = b"__event_authority"
GLOBAL_VOLUME_ACCUMULATOR_SEED = b"global_volume_accumulator"
USER_VOLUME_ACCUMULATOR_SEED = b"user_volume_accumulator"
FEE_CONFIG_SEED = b"fee_config"

# --- Derived, program-wide singletons --------------------------------------
# Derived once at import; these never change for a given program ID.

GLOBAL_ACCOUNT: Pubkey = Pubkey.find_program_address([GLOBAL_SEED], PUMP_FUN_PROGRAM)[0]
EVENT_AUTHORITY: Pubkey = Pubkey.find_program_address(
    [EVENT_AUTHORITY_SEED], PUMP_FUN_PROGRAM
)[0]
GLOBAL_VOLUME_ACCUMULATOR: Pubkey = Pubkey.find_program_address(
    [GLOBAL_VOLUME_ACCUMULATOR_SEED], PUMP_FUN_PROGRAM
)[0]
# The fee config lives under the separate fee program but is seeded by the
# pump.fun program ID.
FEE_CONFIG: Pubkey = Pubkey.find_program_address(
    [FEE_CONFIG_SEED, bytes(PUMP_FUN_PROGRAM)], PUMP_FUN_FEE_PROGRAM
)[0]

# --- Token / curve defaults ------------------------------------------------

LAMPORTS_PER_SOL = 1_000_000_000
PUMP_TOKEN_DECIMALS = 6

# Fallback initial curve state, used only if the on-chain `global` account
# cannot be read at startup. These are pump.fun's long-standing launch values.
DEFAULT_INITIAL_VIRTUAL_TOKEN_RESERVES = 1_073_000_000_000_000
DEFAULT_INITIAL_VIRTUAL_SOL_RESERVES = 30 * LAMPORTS_PER_SOL
DEFAULT_INITIAL_REAL_TOKEN_RESERVES = 793_100_000_000_000
DEFAULT_FEE_BASIS_POINTS = 100  # 1%
DEFAULT_CREATOR_FEE_BASIS_POINTS = 5  # 0.05%

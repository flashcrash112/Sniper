"""Bonding-curve math for pump.fun buys.

The pump.fun `buy` instruction is quoted in *tokens*: you say how many base
units you want and how much SOL you are willing to pay at most. So to spend a
fixed amount of SOL we have to invert the curve ourselves.

The curve is a constant product over virtual reserves:

    k = virtual_sol_reserves * virtual_token_reserves

Spending `sol_in` lamports moves the SOL side up and the token side down:

    tokens_out = virtual_token_reserves
               - k / (virtual_sol_reserves + sol_in)

which is equivalent to, and computed here as, the integer-safe form:

    tokens_out = sol_in * virtual_token_reserves // (virtual_sol_reserves + sol_in)

Fees are charged on top of the SOL that goes into the curve, so a `max_sol_cost`
budget of B lamports buys against B / (1 + fee_rate) lamports of curve input.
"""

from __future__ import annotations

from dataclasses import dataclass

from .constants import (
    DEFAULT_CREATOR_FEE_BASIS_POINTS,
    DEFAULT_FEE_BASIS_POINTS,
    DEFAULT_INITIAL_REAL_TOKEN_RESERVES,
    DEFAULT_INITIAL_VIRTUAL_SOL_RESERVES,
    DEFAULT_INITIAL_VIRTUAL_TOKEN_RESERVES,
)


@dataclass(frozen=True, slots=True)
class CurveState:
    """The reserve values a buy is quoted against."""

    virtual_token_reserves: int
    virtual_sol_reserves: int
    real_token_reserves: int
    fee_basis_points: int = DEFAULT_FEE_BASIS_POINTS
    creator_fee_basis_points: int = DEFAULT_CREATOR_FEE_BASIS_POINTS

    @classmethod
    def initial(cls) -> "CurveState":
        """The state every pump.fun coin starts at, before any trade lands."""
        return cls(
            virtual_token_reserves=DEFAULT_INITIAL_VIRTUAL_TOKEN_RESERVES,
            virtual_sol_reserves=DEFAULT_INITIAL_VIRTUAL_SOL_RESERVES,
            real_token_reserves=DEFAULT_INITIAL_REAL_TOKEN_RESERVES,
        )

    @property
    def total_fee_basis_points(self) -> int:
        return self.fee_basis_points + self.creator_fee_basis_points


def tokens_for_sol(state: CurveState, sol_in_lamports: int) -> int:
    """Tokens (base units) received for `sol_in_lamports` of *curve input*.

    `sol_in_lamports` is the amount reaching the curve, i.e. already net of
    protocol and creator fees.
    """
    if sol_in_lamports <= 0:
        return 0
    tokens = (sol_in_lamports * state.virtual_token_reserves) // (
        state.virtual_sol_reserves + sol_in_lamports
    )
    # The curve can never hand out more than the real tokens it is holding.
    return min(tokens, state.real_token_reserves)


def sol_for_tokens(state: CurveState, token_amount: int) -> int:
    """Inverse of :func:`tokens_for_sol` — curve-input lamports for N tokens.

    Rounded up, matching the program's ceiling division, so the caller never
    under-budgets by one lamport.
    """
    if token_amount <= 0:
        return 0
    if token_amount >= state.virtual_token_reserves:
        raise ValueError("token_amount exceeds virtual token reserves")
    numerator = token_amount * state.virtual_sol_reserves
    denominator = state.virtual_token_reserves - token_amount
    return -(-numerator // denominator)  # ceil division


def apply_fee(state: CurveState, curve_input_lamports: int) -> int:
    """Total lamports debited for a given curve input, fees included."""
    fee = (curve_input_lamports * state.total_fee_basis_points + 9_999) // 10_000
    return curve_input_lamports + fee


def strip_fee(state: CurveState, budget_lamports: int) -> int:
    """Largest curve input whose fee-inclusive cost fits inside `budget_lamports`."""
    return (budget_lamports * 10_000) // (10_000 + state.total_fee_basis_points)


@dataclass(frozen=True, slots=True)
class BuyQuote:
    """A fully resolved buy, ready to be encoded into instruction data."""

    token_amount: int
    """The `amount` argument: base units we ask the program for."""

    max_sol_cost: int
    """The `max_sol_cost` argument: hard lamport ceiling, fees included."""

    expected_sol_cost: int
    """What we expect to pay if we land with no one in front of us."""

    expected_token_amount: int
    """Tokens we would get at the untouched price, before the slippage haircut."""


def quote_buy(
    state: CurveState,
    sol_budget_lamports: int,
    slippage_bps: int,
) -> BuyQuote:
    """Turn a SOL budget into pump.fun `buy` arguments.

    Slippage is applied in *both* directions, which is what makes the buy
    survive being second in the block:

    * the token request is cut by `slippage_bps`, so a price that moved against
      us still satisfies the program's cost check, and
    * `max_sol_cost` is raised by `slippage_bps`, so the extra lamports needed
      for that price are actually authorised.

    The result is a buy that fills anywhere inside the slippage band and reverts
    outside it, rather than one that reverts the moment anyone lands first.
    """
    if sol_budget_lamports <= 0:
        raise ValueError("sol_budget_lamports must be positive")
    if slippage_bps < 0:
        raise ValueError("slippage_bps must not be negative")

    curve_input = strip_fee(state, sol_budget_lamports)
    expected_tokens = tokens_for_sol(state, curve_input)
    if expected_tokens <= 0:
        raise ValueError("budget too small to buy a single token base unit")

    # Ask for fewer tokens than the quote so a worse price still clears.
    token_amount = (expected_tokens * 10_000) // (10_000 + slippage_bps)
    token_amount = max(token_amount, 1)

    # Authorise more SOL than the quote for the same reason.
    max_sol_cost = (sol_budget_lamports * (10_000 + slippage_bps)) // 10_000

    return BuyQuote(
        token_amount=token_amount,
        max_sol_cost=max_sol_cost,
        expected_sol_cost=apply_fee(state, sol_for_tokens(state, token_amount)),
        expected_token_amount=expected_tokens,
    )

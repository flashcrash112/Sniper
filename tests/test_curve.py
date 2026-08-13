import pytest

from sniper.constants import LAMPORTS_PER_SOL
from sniper.curve import (
    CurveState,
    apply_fee,
    quote_buy,
    sol_for_tokens,
    strip_fee,
    tokens_for_sol,
)


def test_initial_state_matches_pumpfun_launch_values():
    state = CurveState.initial()
    assert state.virtual_token_reserves == 1_073_000_000_000_000
    assert state.virtual_sol_reserves == 30 * LAMPORTS_PER_SOL


def test_tokens_for_sol_and_inverse_round_trip():
    state = CurveState.initial()
    sol_in = LAMPORTS_PER_SOL // 2
    tokens = tokens_for_sol(state, sol_in)
    assert tokens > 0
    # sol_for_tokens ceilings, so the round trip lands within a lamport.
    assert abs(sol_for_tokens(state, tokens) - sol_in) <= 1


def test_larger_buys_get_a_worse_price_per_token():
    state = CurveState.initial()
    small = tokens_for_sol(state, LAMPORTS_PER_SOL)
    large = tokens_for_sol(state, 10 * LAMPORTS_PER_SOL)
    assert large < small * 10  # price impact is real and in the right direction


def test_tokens_never_exceed_real_reserves():
    state = CurveState.initial()
    assert tokens_for_sol(state, 10_000 * LAMPORTS_PER_SOL) == state.real_token_reserves


def test_fee_helpers_are_inverses():
    state = CurveState.initial()
    budget = LAMPORTS_PER_SOL
    curve_input = strip_fee(state, budget)
    assert apply_fee(state, curve_input) <= budget


def test_quote_leaves_headroom_in_both_directions():
    state = CurveState.initial()
    budget = LAMPORTS_PER_SOL
    quote = quote_buy(state, budget, slippage_bps=1000)

    # We ask for fewer tokens than the untouched price would give us...
    assert quote.token_amount < quote.expected_token_amount
    # ...and authorise more SOL than the position size.
    assert quote.max_sol_cost > budget
    assert quote.max_sol_cost == budget * 11000 // 10000


def test_quote_survives_a_frontrun_within_slippage():
    """The whole point of the haircut: someone lands ahead of us and we still fill."""
    state = CurveState.initial()
    quote = quote_buy(state, LAMPORTS_PER_SOL, slippage_bps=1000)

    # A 0.5 SOL buy lands first, moving the price against us.
    moved = CurveState(
        virtual_token_reserves=state.virtual_token_reserves
        - tokens_for_sol(state, LAMPORTS_PER_SOL // 2),
        virtual_sol_reserves=state.virtual_sol_reserves + LAMPORTS_PER_SOL // 2,
        real_token_reserves=state.real_token_reserves,
    )
    cost_now = apply_fee(moved, sol_for_tokens(moved, quote.token_amount))
    assert cost_now <= quote.max_sol_cost, "buy would revert against a small frontrun"


def test_quote_reverts_outside_the_slippage_band():
    state = CurveState.initial()
    quote = quote_buy(state, LAMPORTS_PER_SOL, slippage_bps=100)  # 1%

    # A 20 SOL buy lands first — far outside a 1% band.
    moved = CurveState(
        virtual_token_reserves=state.virtual_token_reserves
        - tokens_for_sol(state, 20 * LAMPORTS_PER_SOL),
        virtual_sol_reserves=state.virtual_sol_reserves + 20 * LAMPORTS_PER_SOL,
        real_token_reserves=state.real_token_reserves,
    )
    cost_now = apply_fee(moved, sol_for_tokens(moved, quote.token_amount))
    assert cost_now > quote.max_sol_cost, "slippage cap failed to protect us"


def test_zero_and_negative_inputs():
    state = CurveState.initial()
    assert tokens_for_sol(state, 0) == 0
    assert sol_for_tokens(state, 0) == 0
    with pytest.raises(ValueError):
        quote_buy(state, 0, 100)

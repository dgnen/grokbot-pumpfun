"""Bonding-curve math for pump.fun.

Before this module the pipeline treated a buy as filling at the quote
price. That is wrong three times over: the venue takes a fee, the buy
moves the price against the buyer, and on the way out the same happens
in reverse. On a curve with a few dozen SOL of reserve, a 0.5 SOL order
is a noticeable share of liquidity, not a speck.

Practical point: without these corrections dry-run shows profit that
will not exist in live, and the live-on decision is made from dry-run.
Better the numbers be duller, but real.

The curve is constant product on virtual reserves:

    k = sol_reserves * token_reserves = const

A buy adds SOL and takes tokens, a sell the reverse. The fee is taken
from the inbound side on a buy and from the outbound side on a sell.

CAVEAT: the constants below are the pump.fun program parameters as they
were at the time of writing. The program is updated. Before enabling
live, check them against the on-chain `global` account state; do not
trust this file.
"""

from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel, Field

# Starting virtual reserves of a fresh curve.
INITIAL_VIRTUAL_SOL = 30.0
INITIAL_VIRTUAL_TOKENS = 1_073_000_191.0

# Full issuance and the part that is actually sold on the curve.
TOTAL_SUPPLY = 1_000_000_000.0
CURVE_TOKEN_SUPPLY = 793_100_000.0

# Real SOL the curve holds by the time it migrates to Raydium.
CURVE_COMPLETION_SOL = 85.0

# Venue fee on every trade, percent.
DEFAULT_TRADE_FEE_PCT = 1.0


class CurveState(BaseModel):
    """Virtual curve reserves. All in SOL and whole tokens."""

    sol_reserves: float = INITIAL_VIRTUAL_SOL
    token_reserves: float = INITIAL_VIRTUAL_TOKENS
    # The curve is finished, the token moved to Raydium. All math in
    # this module no longer applies to it from this point.
    complete: bool = False

    @property
    def is_valid(self) -> bool:
        return self.sol_reserves > 0 and self.token_reserves > 0

    @property
    def k(self) -> float:
        return self.sol_reserves * self.token_reserves

    @property
    def spot_price(self) -> float:
        """Price of an infinitesimal trade. This is what we traded on before."""
        if not self.is_valid:
            return 0.0
        return self.sol_reserves / self.token_reserves

    @property
    def real_sol(self) -> float:
        """How many real SOL have already been put into the curve."""
        return max(0.0, self.sol_reserves - INITIAL_VIRTUAL_SOL)

    @property
    def progress(self) -> float:
        """Curve fill, 0..1. By real SOL, not virtual."""
        return max(0.0, min(1.0, self.real_sol / CURVE_COMPLETION_SOL))

    @classmethod
    def from_spot_price(cls, spot: float) -> CurveState | None:
        """Reserves from spot price alone.

        The product of reserves on the curve is constant, so the pair
        (sol, tokens) is recovered uniquely:
            sol = sqrt(k * spot),  tokens = sqrt(k / spot).
        This is an identity, not an approximation — until the token
        moves to Raydium.
        """
        if spot <= 0:
            return None
        k = INITIAL_VIRTUAL_SOL * INITIAL_VIRTUAL_TOKENS
        return cls(sol_reserves=math.sqrt(k * spot), token_reserves=math.sqrt(k / spot))

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> CurveState | None:
        """Reserves from a provider response. None if they are not there.

        Providers return them in lamports and in raw token units
        (6 decimals), so we convert to human numbers.
        """
        sol_raw = data.get("virtual_sol_reserves")
        token_raw = data.get("virtual_token_reserves")
        if not sol_raw or not token_raw:
            return None
        try:
            state = cls(
                sol_reserves=float(sol_raw) / 1e9,
                token_reserves=float(token_raw) / 1e6,
                complete=bool(data.get("complete") or data.get("raydium_pool")),
            )
        except (TypeError, ValueError):
            return None
        return state if state.is_valid else None


class Quote(BaseModel):
    """What you actually get if you send this order now."""

    ok: bool = True
    reason: str = ""

    sol_in: float = 0.0          # on a buy — how much SOL leaves in total
    sol_out: float = 0.0         # on a sell — how much SOL lands in hand
    tokens: float = 0.0          # tokens received or sold
    fee_sol: float = 0.0

    spot_price: float = 0.0      # price before the trade
    avg_price: float = 0.0       # average fill price — PnL is counted on this
    price_after: float = 0.0
    impact_pct: float = 0.0      # how much worse the average is than the quote

    state_after: CurveState = Field(default_factory=CurveState)


def buy_quote(
    state: CurveState,
    sol_in: float,
    fee_pct: float = DEFAULT_TRADE_FEE_PCT,
) -> Quote:
    """Buy for `sol_in` SOL: how many tokens and at what average price.

    The fee is taken from inbound SOL: less reaches the curve than is
    debited from the wallet, and the average price is counted on the
    debit — that is what decides whether the position is in the black.
    """
    if sol_in <= 0:
        return Quote(ok=False, reason="zero-size order")
    if not state.is_valid:
        return Quote(ok=False, reason="curve reserves unknown")

    fee = sol_in * max(0.0, fee_pct) / 100.0
    sol_to_curve = sol_in - fee
    if sol_to_curve <= 0:
        return Quote(ok=False, reason="fee consumes the entire order")

    new_sol = state.sol_reserves + sol_to_curve
    new_tokens = state.k / new_sol
    tokens_out = state.token_reserves - new_tokens
    if tokens_out <= 0:
        return Quote(ok=False, reason="curve does not yield tokens for this order")

    after = CurveState(sol_reserves=new_sol, token_reserves=new_tokens)
    avg_price = sol_in / tokens_out
    spot = state.spot_price
    return Quote(
        sol_in=sol_in,
        tokens=tokens_out,
        fee_sol=fee,
        spot_price=spot,
        avg_price=avg_price,
        price_after=after.spot_price,
        impact_pct=(avg_price / spot - 1.0) * 100.0 if spot > 0 else 0.0,
        state_after=after,
    )


def sell_quote(
    state: CurveState,
    tokens_in: float,
    fee_pct: float = DEFAULT_TRADE_FEE_PCT,
) -> Quote:
    """Sell `tokens_in` tokens: how much SOL is left after the fee."""
    if tokens_in <= 0:
        return Quote(ok=False, reason="zero-size order")
    if not state.is_valid:
        return Quote(ok=False, reason="curve reserves unknown")

    new_tokens = state.token_reserves + tokens_in
    new_sol = state.k / new_tokens
    gross = state.sol_reserves - new_sol
    if gross <= 0:
        return Quote(ok=False, reason="curve does not yield SOL for this order")

    fee = gross * max(0.0, fee_pct) / 100.0
    net = gross - fee
    after = CurveState(sol_reserves=new_sol, token_reserves=new_tokens)
    avg_price = net / tokens_in
    spot = state.spot_price
    return Quote(
        sol_out=net,
        tokens=tokens_in,
        fee_sol=fee,
        spot_price=spot,
        avg_price=avg_price,
        price_after=after.spot_price,
        impact_pct=(1.0 - avg_price / spot) * 100.0 if spot > 0 else 0.0,
        state_after=after,
    )


def max_sol_for_impact(
    state: CurveState,
    max_impact_pct: float,
    fee_pct: float = DEFAULT_TRADE_FEE_PCT,
) -> float:
    """Largest buy that fits inside the given price impact.

    Derived exactly, no search. Let S and T be reserves, f the fee
    share, s what reaches the curve. Then

        tokens_out = T - k/(S+s) = T·s/(S+s)
        avg = sol_in/tokens_out = (S+s) / (T·(1-f))
        avg/spot = (1 + s/S) / (1-f)

    so the limiting reserve share is s/S = (1+impact)·(1-f) - 1, and
    the order itself is that divided by (1-f): the fee never reaches
    the curve, but it is in the average price.
    """
    if not state.is_valid or max_impact_pct <= 0:
        return 0.0
    fee_share = max(0.0, min(0.99, fee_pct / 100.0))

    share = (1.0 + max_impact_pct / 100.0) * (1.0 - fee_share) - 1.0
    if share <= 0:
        return 0.0          # the fee alone already eats the entire allowance
    return state.sol_reserves * share / (1.0 - fee_share)


def round_trip_cost_pct(
    state: CurveState,
    sol_in: float,
    fee_pct: float = DEFAULT_TRADE_FEE_PCT,
) -> float:
    """What percent an entry and immediate exit will cost.

    This is the floor below which a trade has no point: if the expected
    move is less than the cost of getting in and out, there is nothing
    to trade.
    """
    buy = buy_quote(state, sol_in, fee_pct)
    if not buy.ok:
        return 100.0
    sell = sell_quote(buy.state_after, buy.tokens, fee_pct)
    if not sell.ok:
        return 100.0
    return (1.0 - sell.sol_out / sol_in) * 100.0


def state_from_any(data: dict[str, Any], market_cap_sol: float = 0.0) -> CurveState | None:
    """Curve state from anything: reserves, market cap, price."""
    state = CurveState.from_api(data)
    if state is not None:
        return state
    spot = price_from_reserves(data)
    if spot <= 0 and market_cap_sol > 0:
        spot = market_cap_sol / TOTAL_SUPPLY
    restored = CurveState.from_spot_price(spot)
    if restored is not None:
        restored.complete = bool(data.get("complete") or data.get("raydium_pool"))
    return restored


def price_from_reserves(data: dict[str, Any]) -> float:
    """Spot price from a provider response, with a market-cap fallback."""
    state = CurveState.from_api(data)
    if state is not None:
        return state.spot_price
    market_cap = data.get("market_cap") or data.get("usd_market_cap")
    if market_cap:
        try:
            return float(market_cap) / TOTAL_SUPPLY
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def progress_from_sol(sol_in_curve: float) -> float:
    """Curve fill from SOL volume alone — as the socket reports it."""
    if sol_in_curve <= 0:
        return 0.0
    real = max(0.0, sol_in_curve - INITIAL_VIRTUAL_SOL)
    return max(0.0, min(1.0, real / CURVE_COMPLETION_SOL))


def tokens_for_market_cap(market_cap_sol: float) -> float:
    """Inverse estimate: how many tokens per SOL at this market cap."""
    if market_cap_sol <= 0:
        return 0.0
    return TOTAL_SUPPLY / market_cap_sol


def sanity_check() -> dict[str, float]:
    """Numbers that show the module is not computing nonsense.

    Useful to call by hand after updating the program constants.
    """
    fresh = CurveState()
    return {
        "spot_price": fresh.spot_price,
        "impact_0.5_sol": buy_quote(fresh, 0.5).impact_pct,
        "round_trip_0.5_sol": round_trip_cost_pct(fresh, 0.5),
        "max_sol_for_3pct": max_sol_for_impact(fresh, 3.0),
        "tokens_for_1_sol": buy_quote(fresh, 1.0).tokens,
    }


assert math.isclose(CurveState().k, INITIAL_VIRTUAL_SOL * INITIAL_VIRTUAL_TOKENS)

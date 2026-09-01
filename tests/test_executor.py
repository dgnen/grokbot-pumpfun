"""Dry-run execution: the same fee and slippage as live.

These tests exist so dry-run does not become optimistic again. If it
buys at the quote, all reporting lies in one direction, and the live
decision is made on profit that was never there.
"""

import httpx
import pytest

from src.curve import INITIAL_VIRTUAL_SOL, CurveState
from src.executor import (
    DRY_RUN_TX,
    DryRunExecutor,
    LiveExecutor,
    build_executor,
    new_position,
)
from src.models import Config, Position, Token

LIVE_CURVE = {"virtual_sol_reserves": 45_000_000_000,
              "virtual_token_reserves": 715_333_460_666_667}


def config(**market) -> Config:
    cfg = Config()
    for key, value in market.items():
        setattr(cfg.market, key, value)
    return cfg


def client(payload: dict | None) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload if payload is not None else {})

    return httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(handler))


def token(**overrides) -> Token:
    base = {"mint": "Mint1", "name": "Cat", "symbol": "CAT", "creator": "C1",
            "market_cap_sol": 60.0}
    base.update(overrides)
    return Token(**base)


def position(tokens: float = 1_000_000.0, spent: float = 0.5) -> Position:
    return Position(mint="Mint1", symbol="CAT", creator="C1", entry_price=spent / tokens,
                    peak_price=spent / tokens, sol_spent=spent, token_amount=tokens,
                    opened_at=1.0, tx_hash=DRY_RUN_TX)


# --- buy ------------------------------------------------------------------


async def test_buy_pays_worse_than_quote():
    """The average fill must be worse than the quote: otherwise the fee
    and the order's own price impact were lost somewhere."""
    executor = DryRunExecutor(config(), client(LIVE_CURVE))
    spot = CurveState.from_api(LIVE_CURVE).spot_price

    result = await executor.buy(token(), 0.4)
    assert result.ok
    assert result.price > spot
    assert result.impact_pct > 0
    assert result.fee_sol == pytest.approx(0.4 * 0.01)
    assert result.tx_hash == DRY_RUN_TX


async def test_buy_tokens_match_the_curve():
    executor = DryRunExecutor(config(), client(LIVE_CURVE))
    result = await executor.buy(token(), 0.4)
    # price × amount = spent, to the cent
    assert result.price * result.token_amount == pytest.approx(result.sol_amount, rel=1e-12)


async def test_buy_refused_above_impact_cap():
    executor = DryRunExecutor(config(max_price_impact_pct=1.5), client(LIVE_CURVE))
    result = await executor.buy(token(), 5.0)
    assert not result.ok
    assert "price impact" in result.error
    assert result.impact_pct > 1.5


async def test_buy_refused_without_curve_data():
    executor = DryRunExecutor(config(), client({}))
    result = await executor.buy(token(market_cap_sol=0.0), 0.4)
    assert not result.ok
    assert "curve state unknown" in result.error


async def test_buy_falls_back_to_market_cap():
    """No reserves, but market cap is known — curve state is recovered
    from it exactly, because the reserve product is constant."""
    executor = DryRunExecutor(config(), client({}))
    result = await executor.buy(token(market_cap_sol=60.0), 0.2)
    assert result.ok
    assert result.token_amount > 0


async def test_zero_size_refused():
    executor = DryRunExecutor(config(), client(LIVE_CURVE))
    assert not (await executor.buy(token(), 0.0)).ok


# --- sell -----------------------------------------------------------------


async def test_sell_receives_less_than_quote():
    executor = DryRunExecutor(config(), client(LIVE_CURVE))
    spot = CurveState.from_api(LIVE_CURVE).spot_price
    result = await executor.sell(position())
    assert result.ok
    assert result.price < spot
    assert result.sol_amount > 0


async def test_partial_sell_takes_its_share():
    executor = DryRunExecutor(config(), client(LIVE_CURVE))
    pos = position(tokens=1_000_000.0)
    result = await executor.sell(pos, fraction=0.6)
    assert result.token_amount == pytest.approx(600_000.0)


async def test_dust_tail_is_sold_whole():
    """Leaving less than a percent in the position is pointless: it is
    dust that only gets in the way of the books."""
    executor = DryRunExecutor(config(), client(LIVE_CURVE))
    result = await executor.sell(position(tokens=1_000_000.0), fraction=0.995)
    assert result.token_amount == pytest.approx(1_000_000.0)


async def test_sell_fraction_clamped():
    executor = DryRunExecutor(config(), client(LIVE_CURVE))
    pos = position(tokens=1_000_000.0)
    assert (await executor.sell(pos, fraction=5.0)).token_amount == pytest.approx(1_000_000.0)
    assert not (await executor.sell(pos, fraction=0.0)).ok


async def test_sell_without_curve_refused():
    executor = DryRunExecutor(config(), client({}))
    result = await executor.sell(position())
    assert not result.ok


# --- price and state ------------------------------------------------------


async def test_price_returns_spot():
    executor = DryRunExecutor(config(), client(LIVE_CURVE))
    assert await executor.price("Mint1") == pytest.approx(
        CurveState.from_api(LIVE_CURVE).spot_price
    )


async def test_price_zero_when_unknown():
    executor = DryRunExecutor(config(), client({}))
    assert await executor.price("Mint1") == 0.0


async def test_curve_progress_excludes_virtual():
    executor = DryRunExecutor(config(), client(LIVE_CURVE))
    state = await executor.curve("Mint1")
    assert state is not None
    assert state.real_sol == pytest.approx(45.0 - INITIAL_VIRTUAL_SOL)


# --- modes ----------------------------------------------------------------


def test_build_executor_picks_by_mode():
    assert isinstance(build_executor(config()), DryRunExecutor)
    live = config()
    live.mode = "live"
    assert isinstance(build_executor(live), LiveExecutor)


async def test_live_executor_is_a_deliberate_stub():
    executor = LiveExecutor(config(), client(LIVE_CURVE))
    with pytest.raises(NotImplementedError, match="intentionally unimplemented"):
        await executor.buy(token(), 0.4)
    with pytest.raises(NotImplementedError, match="intentionally unimplemented"):
        await executor.sell(position())


async def test_live_executor_can_still_quote():
    """Order quoting is live even in live mode: max_sol_cost and
    min_sol_output come from it once sending is implemented."""
    executor = LiveExecutor(config(), client(LIVE_CURVE))
    state = await executor.curve("Mint1")
    assert executor.plan_buy(state, 0.4).ok
    assert executor.plan_sell(state, 1_000_000.0).ok


def test_new_position_carries_context():
    from src.executor import ExecutionResult

    result = ExecutionResult(ok=True, price=1e-7, token_amount=5_000_000.0,
                             sol_amount=0.5, tx_hash=DRY_RUN_TX)
    pos = new_position(token(), result, score=0.81)
    assert pos.creator == "C1"
    assert pos.peak_price == pos.entry_price == 1e-7
    assert pos.score == 0.81
    assert pos.realized_sol == 0.0 and pos.partials == 0

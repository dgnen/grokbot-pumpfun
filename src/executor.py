"""Trade execution on Solana.

INTENTIONALLY A STUB for transaction submission: `LiveExecutor` raises
NotImplementedError, with a list of steps to fill in by hand. Code that
signs transactions with a private key is not generated here.

Everything else is real and, more importantly, honest: dry-run executes
against the curve math in `curve.py` — with fee, slippage, and the
order's own price impact. It used to fill at the quote price, and
dry-run showed profit that never happens in live.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx
from pydantic import BaseModel, Field

from .curve import (
    TOTAL_SUPPLY,
    CurveState,
    buy_quote,
    price_from_reserves,
    sell_quote,
    state_from_any,
)
from .models import Config, Position, Token

log = logging.getLogger(__name__)

DRY_RUN_TX = "dry_run"

__all__ = [
    "TOTAL_SUPPLY",
    "BaseExecutor",
    "DryRunExecutor",
    "ExecutionResult",
    "LiveExecutor",
    "build_executor",
    "new_position",
    "price_from_reserves",
]


class ExecutionResult(BaseModel):
    """Outcome of an execution attempt."""

    ok: bool
    tx_hash: str = ""
    price: float = 0.0           # average fill price, not the quote
    token_amount: float = 0.0
    sol_amount: float = 0.0
    fee_sol: float = 0.0
    impact_pct: float = 0.0
    error: str = ""
    state_after: CurveState | None = Field(default=None)


class BaseExecutor:
    """Shared piece: quotes, curve state, execution math."""

    def __init__(self, config: Config, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self.market = config.market
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> BaseExecutor:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.config.data.rest_url,
                timeout=self.config.data.request_timeout,
            )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _coin(self, mint: str) -> dict[str, Any]:
        if self._client is None:
            return {}
        try:
            resp = await self._client.get(f"/coins/{mint}")
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            log.warning("data for %s unavailable: %s", mint, exc)
            return {}
        return data if isinstance(data, dict) else {}

    async def curve(self, mint: str, market_cap_sol: float = 0.0) -> CurveState | None:
        """Current curve state. None if there is nothing to restore from."""
        return state_from_any(await self._coin(mint), market_cap_sol)

    async def price(self, mint: str) -> float:
        """Spot price. Exit rules use this — they track market movement,
        not the fill of a particular order."""
        state = await self.curve(mint)
        return state.spot_price if state else 0.0

    async def buy(self, token: Token, size_sol: float) -> ExecutionResult:
        raise NotImplementedError

    async def sell(self, position: Position, fraction: float = 1.0) -> ExecutionResult:
        raise NotImplementedError

    # -- math shared by both modes ----------------------------------------

    def plan_buy(self, state: CurveState, size_sol: float) -> ExecutionResult:
        quote = buy_quote(state, size_sol, self.market.trade_fee_pct)
        if not quote.ok:
            return ExecutionResult(ok=False, error=quote.reason)
        if quote.impact_pct > self.market.max_price_impact_pct:
            return ExecutionResult(
                ok=False,
                error=(f"price impact {quote.impact_pct:.2f}% exceeds "
                       f"ceiling {self.market.max_price_impact_pct:.2f}%"),
                impact_pct=quote.impact_pct,
            )
        return ExecutionResult(
            ok=True,
            price=quote.avg_price,
            token_amount=quote.tokens,
            sol_amount=size_sol,
            fee_sol=quote.fee_sol,
            impact_pct=quote.impact_pct,
            state_after=quote.state_after,
        )

    def plan_sell(self, state: CurveState, tokens: float) -> ExecutionResult:
        quote = sell_quote(state, tokens, self.market.trade_fee_pct)
        if not quote.ok:
            return ExecutionResult(ok=False, error=quote.reason)
        return ExecutionResult(
            ok=True,
            price=quote.avg_price,
            token_amount=tokens,
            sol_amount=quote.sol_out,
            fee_sol=quote.fee_sol,
            impact_pct=quote.impact_pct,
            state_after=quote.state_after,
        )

    @staticmethod
    def _portion(position: Position, fraction: float) -> float:
        """How many tokens we sell. A tail under one percent is taken whole:
        leaving dust in the position is pointless, it only messes up accounting."""
        fraction = max(0.0, min(1.0, fraction))
        tokens = position.token_amount * fraction
        if position.token_amount - tokens < position.token_amount * 0.01:
            tokens = position.token_amount
        return tokens


class DryRunExecutor(BaseExecutor):
    """Walks the full path except submitting the transaction."""

    async def buy(self, token: Token, size_sol: float) -> ExecutionResult:
        state = await self.curve(token.mint, token.market_cap_sol)
        if state is None:
            # A position with an unknown entry price is unmanageable: no
            # exit rule can fire on it.
            log.warning("buy %s cancelled: curve state unknown", token.mint[:8])
            return ExecutionResult(ok=False, error="curve state unknown")

        result = self.plan_buy(state, size_sol)
        if not result.ok:
            log.warning("buy %s cancelled: %s", token.mint[:8], result.error)
            return result

        result.tx_hash = DRY_RUN_TX
        log.info("[dry-run] bought %s: %.4f SOL -> %.0f tokens at %.12f "
                 "(fee %.4f SOL, impact %.2f%%)",
                 token.mint[:8], size_sol, result.token_amount, result.price,
                 result.fee_sol, result.impact_pct)
        return result

    async def sell(self, position: Position, fraction: float = 1.0) -> ExecutionResult:
        state = await self.curve(position.mint)
        if state is None:
            return ExecutionResult(ok=False, error="curve state unknown")

        tokens = self._portion(position, fraction)
        if state.complete:
            # The curve is gone: the token trades on Raydium with its own
            # liquidity, and constant-product math no longer applies.
            # Price at spot with no impact and mark it honestly as an estimate.
            log.warning("%s already on Raydium: proceeds priced at spot, "
                        "no slippage — this is an estimate, not a quote",
                        position.mint[:8])
            gross = tokens * state.spot_price
            fee = gross * self.market.trade_fee_pct / 100.0
            return ExecutionResult(
                ok=True, tx_hash=DRY_RUN_TX, price=state.spot_price,
                token_amount=tokens, sol_amount=gross - fee, fee_sol=fee,
            )

        result = self.plan_sell(state, tokens)
        if not result.ok:
            return result

        result.tx_hash = DRY_RUN_TX
        log.info("[dry-run] sold %s: %.0f tokens -> %.4f SOL at %.12f "
                 "(fee %.4f SOL, impact %.2f%%)",
                 position.mint[:8], tokens, result.sol_amount, result.price,
                 result.fee_sol, result.impact_pct)
        return result


class LiveExecutor(BaseExecutor):
    """Real transaction submission. Intentionally unimplemented.

    Order math is already ready: `plan_buy` and `plan_sell` give the
    expected token amount, average price, and price impact — `max_sol_cost`
    and `min_sol_output` are taken from those with the needed tolerance.
    """

    def __init__(self, config: Config, client: httpx.AsyncClient | None = None) -> None:
        super().__init__(config, client)
        self.rpc_url = config.solana.rpc_url
        self.jito = config.solana.jito

    async def buy(self, token: Token, size_sol: float) -> ExecutionResult:
        # TODO(live): buy on the pump.fun bonding curve.
        #  1. Load a Keypair from config.solana.wallet_private_key (solders.keypair).
        #  2. Fetch curve accounts: bonding_curve, associated_bonding_curve,
        #     global, fee_recipient — and create the buyer's ATA if missing.
        #  3. Take the math from self.plan_buy(state, size_sol): expected tokens
        #     and average price already include fee and slippage.
        #     max_sol_cost = size_sol * (1 + tolerance), tolerance around 1-2%.
        #  4. Build the `buy` instruction of program
        #     6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P and ComputeBudget:
        #     unit price and limit.
        #  5. If config.solana.jito.enabled — add a tip transfer
        #     (jito.tip_lamports) to a tip account and send a bundle to
        #     jito.block_engine_url; otherwise send_transaction via RPC.
        #  6. Wait for confirmation, return ExecutionResult with the real
        #     tx_hash, fill price, and tokens received.
        #     Subtract the Jito tip from sol_amount: it is also a trade cost.
        raise NotImplementedError(
            "LiveExecutor.buy is intentionally unimplemented: write the "
            "transaction submission yourself before enabling mode: live"
        )

    async def sell(self, position: Position, fraction: float = 1.0) -> ExecutionResult:
        # TODO(live): sell. Same scheme as buy, but the `sell` instruction,
        #  min_sol_output from self.plan_sell(state, tokens) with downward
        #  tolerance, and close the ATA after a full exit (not on a partial).
        raise NotImplementedError(
            "LiveExecutor.sell is intentionally unimplemented: write the "
            "transaction submission yourself before enabling mode: live"
        )


def build_executor(config: Config, client: httpx.AsyncClient | None = None) -> BaseExecutor:
    """Executor for the mode in the config."""
    if config.is_live:
        log.warning("live mode: using LiveExecutor")
        return LiveExecutor(config, client)
    return DryRunExecutor(config, client)


def new_position(token: Token, result: ExecutionResult, score: float) -> Position:
    return Position(
        mint=token.mint,
        symbol=token.symbol,
        creator=token.creator,
        entry_price=result.price,
        peak_price=result.price,
        sol_spent=result.sol_amount,
        token_amount=result.token_amount,
        opened_at=time.time(),
        tx_hash=result.tx_hash,
        score=score,
    )

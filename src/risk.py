"""Risk manager: five limiters and position size.

Last gate before execution, and the only one that asks Grok nothing.
All thresholds come from the config:

1. SOL ceiling per trade
2. daily loss limit (hit — the pipeline sits until the next day)
3. max trades per day
4. max concurrent open positions
5. stop-loss in percent, watched by a background task

Position size is proportional to the score, but not above the ceiling
and not more than 30% of the remaining daily loss budget.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from .models import Config, Position, RiskConfig, TradeDecision
from .state import PipelineState, StateStore, describe

log = logging.getLogger(__name__)

# Share of the remaining daily limit that one trade may risk.
MAX_SHARE_OF_REMAINING_BUDGET = 0.30

# Below this size a trade has no point: fees and tips will eat it.
MIN_TRADE_SOL = 0.01


class RiskManager:
    """Day state, open positions, and the size decision."""

    def __init__(
        self,
        config: Config,
        clock: Callable[[], float] = time.time,
        store: StateStore | None = None,
    ) -> None:
        self.config = config
        self.risk: RiskConfig = config.risk
        self.clock = clock
        self.store = store
        self.day = self._today()
        self.trades_today = 0
        self.realized_pnl_sol = 0.0          # negative = loss
        self.positions: dict[str, Position] = {}
        self.grok_calls_today = 0
        self.losing_streak = 0
        self.cooldown_until = 0.0

    # -- state on disk -----------------------------------------------------

    def restore(self) -> bool:
        """Lift state from disk. True if something was restored.

        Positions are always restored: they are really open on-chain,
        however much time has passed. Day counters — only if the file
        is from today's day: another day's limits do not bind us.
        """
        if self.store is None:
            return False
        state = self.store.load()
        if state is None:
            return False

        self.positions = dict(state.positions)
        self.losing_streak = state.losing_streak
        self.cooldown_until = state.cooldown_until
        if state.day == self.day:
            self.trades_today = state.trades_today
            self.realized_pnl_sol = state.realized_pnl_sol
            self.grok_calls_today = state.grok_calls_today
        else:
            log.info("state is from %s, today is %s — resetting day counters",
                     state.day or "?", self.day)
        log.info("state restored: %s", describe(state))
        if self.halted:
            log.warning("after restore the daily loss limit is already spent — "
                        "no trading today")
        return True

    def persist(self) -> None:
        """Save state. Called after every money change."""
        if self.store is None:
            return
        self.store.save(
            PipelineState(
                day=self.day,
                trades_today=self.trades_today,
                realized_pnl_sol=self.realized_pnl_sol,
                grok_calls_today=self.grok_calls_today,
                losing_streak=self.losing_streak,
                cooldown_until=self.cooldown_until,
                positions=self.positions,
            )
        )

    # -- day ---------------------------------------------------------------

    def _today(self) -> str:
        return datetime.fromtimestamp(self.clock(), tz=UTC).strftime("%Y-%m-%d")

    def roll_day_if_needed(self) -> bool:
        """New day — reset counters. Open positions are left alone."""
        today = self._today()
        if today != self.day:
            log.info("new day %s: counters reset (was %d trades, PnL %.4f)",
                     today, self.trades_today, self.realized_pnl_sol)
            self.day = today
            self.trades_today = 0
            self.realized_pnl_sol = 0.0
            self.grok_calls_today = 0
            self.persist()
            return True
        return False

    # -- state -------------------------------------------------------------

    @property
    def daily_loss(self) -> float:
        """Loss for the day as a positive number. Profit -> 0."""
        return max(0.0, -self.realized_pnl_sol)

    @property
    def remaining_loss_budget(self) -> float:
        return max(0.0, self.risk.daily_loss_limit_sol - self.daily_loss)

    @property
    def halted(self) -> bool:
        """Daily loss limit spent — no trading until the day ends."""
        self.roll_day_if_needed()
        return self.daily_loss >= self.risk.daily_loss_limit_sol

    @property
    def cooling_down(self) -> bool:
        """Pause after a losing streak.

        The daily limit catches a slow bleed, but not a fast streak:
        three stops in a row usually mean a hostile regime, not bad
        luck. The pause keeps us from feeding it the rest of the daily
        budget in ten minutes.
        """
        return self.clock() < self.cooldown_until

    @property
    def cooldown_left_seconds(self) -> float:
        return max(0.0, self.cooldown_until - self.clock())

    @property
    def open_count(self) -> int:
        return len(self.positions)

    @property
    def exposure_sol(self) -> float:
        """How much SOL is in the market now. Counted on residual cost:
        what was already taken by a partial take is no longer a risk."""
        return sum(position.sol_spent for position in self.positions.values())

    @property
    def remaining_exposure(self) -> float:
        return max(0.0, self.risk.max_total_exposure_sol - self.exposure_sol)

    # -- decision ----------------------------------------------------------

    def position_size(self, score: float, liquidity_cap: float | None = None) -> float:
        """Position size: proportional to the score and capped by four
        ceilings — the trade ceiling, remaining daily limit, free
        exposure, and curve liquidity.

        On liquidity: an order that moves the price by percents buys
        from itself — part of the expected move is spent on its own
        slippage before the market decides anything.

        On exposure: three positions at the ceiling are no longer three
        small bets, they are one large one, because memecoins fall
        together.
        """
        by_score = self.risk.max_sol_per_trade * max(0.0, min(1.0, score))
        by_budget = self.remaining_loss_budget * MAX_SHARE_OF_REMAINING_BUDGET
        limits = [by_score, by_budget, self.remaining_exposure]
        if liquidity_cap is not None and liquidity_cap > 0:
            limits.append(liquidity_cap)
        return round(min(limits), 6)

    def evaluate(
        self, mint: str, score: float, liquidity_cap: float | None = None
    ) -> TradeDecision:
        """Whether to let the trade through, and for how much."""
        self.roll_day_if_needed()

        if self.halted:
            return TradeDecision(
                approved=False,
                reason=f"daily_loss_limit_hit ({self.daily_loss:.4f} SOL)",
            )
        if self.cooling_down:
            return TradeDecision(
                approved=False,
                reason=(f"cooldown_after_losses ({self.losing_streak} in a row, "
                        f"{self.cooldown_left_seconds / 60:.0f} min left)"),
            )
        if self.trades_today >= self.risk.max_trades_per_day:
            return TradeDecision(
                approved=False,
                reason=f"max_trades_per_day ({self.trades_today}/{self.risk.max_trades_per_day})",
            )
        if self.open_count >= self.risk.max_open_positions:
            return TradeDecision(
                approved=False,
                reason=f"max_open_positions ({self.open_count}/{self.risk.max_open_positions})",
            )
        if mint in self.positions:
            return TradeDecision(approved=False, reason="already_open")
        if self.remaining_exposure < MIN_TRADE_SOL:
            return TradeDecision(
                approved=False,
                reason=(f"max_total_exposure ({self.exposure_sol:.4f}/"
                        f"{self.risk.max_total_exposure_sol:.4f} SOL)"),
            )

        size = self.position_size(score, liquidity_cap)
        if size < MIN_TRADE_SOL:
            return TradeDecision(
                approved=False,
                reason=f"size_too_small ({size:.6f} SOL)",
                size_sol=size,
            )
        return TradeDecision(approved=True, size_sol=size, reason="ok")

    # -- bookkeeping -------------------------------------------------------

    def register_open(self, position: Position) -> None:
        self.roll_day_if_needed()
        self.positions[position.mint] = position
        self.trades_today += 1
        self.persist()

    def register_partial(self, mint: str, pnl_sol: float) -> None:
        """Partial exit: money into the daily total, position stays open."""
        self.realized_pnl_sol += pnl_sol
        self.persist()

    def register_close(self, mint: str, pnl_sol: float) -> Position | None:
        position = self.positions.pop(mint, None)
        self.realized_pnl_sol += pnl_sol
        self._update_streak(pnl_sol)
        self.persist()
        if self.halted:
            log.warning("daily loss limit spent, trading halted until %s",
                        "the next UTC day")
        return position

    def _update_streak(self, pnl_sol: float) -> None:
        """Count the trade outcome into the losing streak and, if due, pause."""
        if pnl_sol >= 0:
            self.losing_streak = 0
            return
        self.losing_streak += 1
        threshold = self.risk.cooldown_after_losses
        if threshold and self.losing_streak >= threshold and self.risk.cooldown_minutes:
            self.cooldown_until = self.clock() + self.risk.cooldown_minutes * 60.0
            log.warning("%d losses in a row — pause for %.0f minutes: a stop streak "
                        "usually means a hostile regime, not bad luck",
                        self.losing_streak, self.risk.cooldown_minutes)

    def snapshot(self) -> dict[str, float | int | str]:
        return {
            "day": self.day,
            "trades_today": self.trades_today,
            "open_positions": self.open_count,
            "exposure_sol": round(self.exposure_sol, 6),
            "realized_pnl_sol": round(self.realized_pnl_sol, 6),
            "remaining_loss_budget": round(self.remaining_loss_budget, 6),
            "halted": self.halted,
            "grok_calls_today": self.grok_calls_today,
            "losing_streak": self.losing_streak,
            "cooldown_left_seconds": round(self.cooldown_left_seconds, 1),
        }


# --------------------------------------------------------------------------
# Position exits
# --------------------------------------------------------------------------

# Rule order is priority order. First we save money, then we take
# profit, then we protect profit, and only then we close on time: a
# position that is going up must not close on the timer.
EXIT_REASONS = ("stop_loss", "take_profit", "trailing_stop", "max_hold")


class Tick(BaseModel):
    """One price observation of a position."""

    price: float = 0.0
    graduated: bool = False       # token moved from the curve to Raydium

    @classmethod
    def of(cls, value: Tick | float | None) -> Tick:
        """We also accept a bare number: the price callback is sometimes simple."""
        if isinstance(value, Tick):
            return value
        return cls(price=float(value or 0.0))


class ExitSignal(BaseModel):
    """Reason to exit, the justification, and what share of the position to sell."""

    reason: str
    detail: str = ""
    fraction: float = 1.0


def pnl_pct(position: Position, price: float) -> float:
    if position.entry_price <= 0:
        return 0.0
    return (price - position.entry_price) / position.entry_price * 100.0


def stop_loss_triggered(position: Position, price: float, stop_loss_pct: float) -> bool:
    if position.entry_price <= 0 or price <= 0 or stop_loss_pct <= 0:
        return False
    return -pnl_pct(position, price) >= stop_loss_pct


def exit_signal(
    position: Position,
    price: float,
    risk: RiskConfig,
    now: float | None = None,
) -> ExitSignal | None:
    """Whether it is time to exit and why. None — hold on.

    Stop-loss here is only one of four rules. Without the rest a
    position that grew 3x has no way to close in the green — it just
    waits until it rolls back to the stop.
    """
    if price <= 0 or position.entry_price <= 0:
        return None

    change = pnl_pct(position, price)

    if risk.stop_loss_pct and -change >= risk.stop_loss_pct:
        return ExitSignal(reason="stop_loss", detail=f"{change:+.1f}% from entry")

    # A move to Raydium is good news and also the end of our math: the
    # curve is gone, we no longer control the price. Exit at once, do
    # not wait for rules that count on the curve to go blind.
    if position.graduated:
        return ExitSignal(reason="graduated", detail=f"moved to Raydium, {change:+.1f}%")

    # The first take-profit may be partial: take the main piece and
    # leave a tail for the trail. The rule does not fire again —
    # otherwise the position would sell in pieces on every tick.
    if risk.take_profit_pct and change >= risk.take_profit_pct and position.partials == 0:
        return ExitSignal(
            reason="take_profit",
            detail=f"{change:+.1f}% from entry",
            fraction=max(0.0, min(1.0, risk.take_profit_fraction)),
        )

    # Trailing works only above entry: below that the stop-loss owns
    # the position, otherwise two rules would fight over the same drawdown.
    peak = max(position.peak_price, price)
    if risk.trailing_stop_pct and peak > position.entry_price:
        drawdown = (peak - price) / peak * 100.0
        if drawdown >= risk.trailing_stop_pct:
            return ExitSignal(
                reason="trailing_stop",
                detail=f"pullback {drawdown:.1f}% from peak {pnl_pct(position, peak):+.1f}%",
            )

    if risk.max_hold_seconds and position.opened_at:
        held = (now or time.time()) - position.opened_at
        if held >= risk.max_hold_seconds:
            return ExitSignal(
                reason="max_hold",
                detail=f"{held / 60:.0f} min in position, {change:+.1f}%",
            )

    return None


class PositionWatcher:
    """Background task: polls prices of open positions and calls the sell.

    Price and sell come in as callbacks — no RPC and no executor are
    pulled in here, so this is tested without a network.
    """

    # How much the peak must grow before we write it to disk. Writing
    # state on every tick is pointless, and losing the peak on restart
    # means restarting the trail from the current price.
    PEAK_PERSIST_STEP = 1.01

    # This many passes in a row without a quote — the position is
    # blind: exit rules do not work on it, and staying silent is not
    # allowed.
    BLIND_AFTER = 3

    def __init__(
        self,
        manager: RiskManager,
        price_fn: Callable[[str], Awaitable[Any]],
        sell_fn: Callable[[Position, float, str, float], Awaitable[None]],
    ) -> None:
        self.manager = manager
        self.price_fn = price_fn
        self.sell_fn = sell_fn
        self.price_failures: dict[str, int] = {}
        self._task: asyncio.Task | None = None

    @property
    def blind(self) -> list[str]:
        """Positions that have had no price for a while. Exits on them do not work."""
        return [mint for mint, misses in self.price_failures.items()
                if misses >= self.BLIND_AFTER and mint in self.manager.positions]

    async def check_once(self) -> list[str]:
        """One pass over open positions. Returns closed mints."""
        triggered: list[str] = []
        persist_needed = False

        for position in list(self.manager.positions.values()):
            try:
                tick = Tick.of(await self.price_fn(position.mint))
            except Exception as exc:
                log.warning("price for %s unavailable: %s", position.mint, exc)
                tick = Tick()
            price = tick.price
            if tick.graduated and not position.graduated:
                log.info("%s moved to Raydium — exiting, our rules do not work there",
                         position.mint[:8])
                position.graduated = True
            if price <= 0:
                self._miss(position.mint)
                continue
            self.price_failures.pop(position.mint, None)

            if price > position.peak_price:
                persist_needed = persist_needed or (
                    price >= position.peak_price * self.PEAK_PERSIST_STEP
                )
                position.peak_price = price

            signal = exit_signal(position, price, self.manager.risk)
            if signal is None:
                continue

            log.info("exit %s on rule %s (%s), fraction %.0f%%: "
                     "entry %.12f, now %.12f",
                     position.mint[:8], signal.reason, signal.detail,
                     signal.fraction * 100, position.entry_price, price)
            await self.sell_fn(position, price, signal.reason, signal.fraction)
            if position.mint not in self.manager.positions:
                triggered.append(position.mint)
            persist_needed = False        # the sell will persist state itself

        if persist_needed:
            self.manager.persist()
        return triggered

    def _miss(self, mint: str) -> None:
        """Count a pass without a quote and speak up when there are too many."""
        misses = self.price_failures.get(mint, 0) + 1
        self.price_failures[mint] = misses
        if misses == self.BLIND_AFTER:
            log.error("position %s has no price for %d passes in a row — exit "
                      "rules on it are not working now", mint[:8], misses)

    async def run(self) -> None:
        interval = self.manager.risk.stop_loss_poll_seconds
        while True:
            try:
                await self.check_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("position watch crashed on a pass: %s", exc)
            await asyncio.sleep(interval)

    def start(self) -> asyncio.Task:
        self._task = asyncio.create_task(self.run(), name="position-watcher")
        return self._task

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

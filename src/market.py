"""Market pulse: what the pipeline sees with its own eyes.

The timing agent is supposed to judge the backdrop, but before this
module it was fed the pipeline's internal counters — how many tokens
are in the buffer and how many positions are open. Market regime cannot
be read from that, and the model was inventing more than it was
judging.

Here we collect what is known for certain because we observed it
ourselves: how many launches per minute come through the socket, what
share reach review, how much SOL they gather, how our last trades
ended. Nothing external, no BTC quotes — only our own observation
window, and the agent is told outright that this is what it sees.

All windows are sliding: for a process that lives for days, all-time
stats are useless — they smear yesterday's calm over today's storm.
"""

from __future__ import annotations

import statistics
import time
from collections import deque
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

# Observation window over the launch stream.
LAUNCH_WINDOW_SECONDS = 900.0

# How many recent closes to keep in outcome stats.
OUTCOME_MEMORY = 50


class MarketPulse:
    """Sliding stats of the launch stream and our own outcomes."""

    def __init__(
        self,
        window_seconds: float = LAUNCH_WINDOW_SECONDS,
        outcome_memory: int = OUTCOME_MEMORY,
    ) -> None:
        self.window = window_seconds
        self.launches: deque[tuple[float, float]] = deque()   # (when, SOL in curve)
        self.passed: deque[float] = deque()                   # when it reached review
        self.bought: deque[float] = deque()
        self.outcomes: deque[float] = deque(maxlen=outcome_memory)   # pnl_pct
        self.rugs: deque[float] = deque(maxlen=outcome_memory)       # 1.0 rug, 0.0 not

    # -- input -------------------------------------------------------------

    def record_launch(self, sol_in_curve: float = 0.0, now: float | None = None) -> None:
        self.launches.append((now or time.time(), max(0.0, sol_in_curve)))
        self._prune(now)

    def record_passed(self, now: float | None = None) -> None:
        """Launch survived the monitor filter and went to paid review."""
        self.passed.append(now or time.time())
        self._prune(now)

    def record_bought(self, now: float | None = None) -> None:
        self.bought.append(now or time.time())
        self._prune(now)

    def record_outcome(self, pnl_pct: float, rug_loss_pct: float = 60.0) -> None:
        self.outcomes.append(pnl_pct)
        self.rugs.append(1.0 if -pnl_pct >= rug_loss_pct else 0.0)

    def seed_from_log(self, records: Iterable[dict[str, Any]], rug_loss_pct: float = 60.0) -> int:
        """Lift outcomes from the log: after a restart memory must not be empty."""
        closes = [r for r in records if r.get("type") == "close" and r.get("final", True)]
        memory = self.outcomes.maxlen or len(closes)
        for record in closes[-memory:]:
            self.record_outcome(float(record.get("pnl_pct") or 0.0), rug_loss_pct)
        return len(self.outcomes)

    def _prune(self, now: float | None = None) -> None:
        cutoff = (now or time.time()) - self.window
        while self.launches and self.launches[0][0] < cutoff:
            self.launches.popleft()
        while self.passed and self.passed[0] < cutoff:
            self.passed.popleft()
        while self.bought and self.bought[0] < cutoff:
            self.bought.popleft()

    # -- output ------------------------------------------------------------

    def snapshot(self, now: float | None = None) -> dict[str, Any]:
        """Numbers for the agent. All from our own observation window."""
        now = now or time.time()
        self._prune(now)
        minutes = self.window / 60.0

        sols = [sol for _, sol in self.launches if sol > 0]
        launches = len(self.launches)

        data: dict[str, Any] = {
            "window_minutes": round(minutes, 1),
            "utc_hour": datetime.fromtimestamp(now, tz=UTC).hour,
            "launches_per_minute": round(launches / minutes, 2) if minutes else 0.0,
            "launches_in_window": launches,
            "share_that_reached_review": (
                round(len(self.passed) / launches, 4) if launches else 0.0
            ),
            "buys_in_window": len(self.bought),
            "median_sol_in_curve": round(statistics.median(sols), 3) if sols else 0.0,
        }

        if self.outcomes:
            wins = [pct for pct in self.outcomes if pct > 0]
            data.update({
                "closed_trades_in_memory": len(self.outcomes),
                "win_rate": round(len(wins) / len(self.outcomes), 4),
                "median_pnl_pct": round(statistics.median(self.outcomes), 2),
                "rug_rate": round(sum(self.rugs) / len(self.rugs), 4),
            })
        else:
            data["closed_trades_in_memory"] = 0

        return data

    def is_thin(self) -> bool:
        """The stream is almost empty — judging the market from it is unfair."""
        return len(self.launches) < 5

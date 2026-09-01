"""Agent 3: market moment.

Evaluates not the token, but the backdrop: sentiment, whether a meme season
is on, what volumes on pump.fun look like right now, whether there are
anomalies. The answer is the same for all tokens within the window, so the
result is cached for `timing_cache_seconds` (15 minutes by default) —
otherwise every launch would pay for the same conclusion.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, ClassVar

from ..models import Config, TimingResult
from .base import JSON_ONLY, GrokAgent

TIMING_PROMPT = f"""You are an analyst of the market regime for Solana memecoins.
You evaluate not a specific token, but the backdrop it is launching against.

IMPORTANT about your data. You are not given a market summary, but the results
of the bot's own observation over the last window: how many launches per minute
passed through the socket, what share reached review, how much SOL they manage
to raise, how the bot's own recent trades ended. This is all that is known
reliably. You have no external quotes, news, or BTC data — do not
pretend you do, and do not invent events.

Fields you receive:
  launches_per_minute, launches_in_window — intensity of the new-token flow;
  share_that_reached_review — what fraction of launches passed the base filter;
  median_sol_in_curve — how much a typical launch manages to raise;
  win_rate, median_pnl_pct, rug_rate — outcomes of the bot's recent
  trades, if any;
  utc_hour — time of day; memecoin liquidity depends on it noticeably;
  sparse_data — the flow is almost empty, conclusions are unreliable.

Give three scores from 0.0 to 1.0 and a list of anomalies:
- market_sentiment: overall sentiment. High when launches are rising
  liquidity and trades close in the green; low when the rug rate
  is growing and results go negative.
- meme_season: whether fresh launches reach notable volumes. Rely on
  median SOL in the curve and on the share that reached review, not on feelings.
- volume_level: flow intensity relative to what usually happens
  at this UTC hour.
- anomalies: short labels of what is breaking normal trading —
  "flow_dried_up", "consecutive_rugs", "night_lull", "launches_not_filling",
  "sparse_data". Empty list if the backdrop is ordinary.

Rules:
- when sparse_data = true, put scores no higher than 0.5 and add the label
  "sparse_data": trading blind is worse than skipping;
- empty trade history is not a good backdrop, it is an absence of information;
- do not explain the numbers with external causes you did not see.

Response format:
{{
  "market_sentiment": 0.0-1.0,
  "meme_season": 0.0-1.0,
  "volume_level": 0.0-1.0,
  "anomalies": ["labels"],
  "reasoning": "2-3 sentences referencing specific numbers from the input"
}}

{JSON_ONLY}"""


class TimingAgent(GrokAgent):
    name: ClassVar[str] = "timing"
    version: ClassVar[str] = "timing-2"
    prompt: ClassVar[str] = TIMING_PROMPT
    result_model: ClassVar[type] = TimingResult

    def __init__(
        self,
        config: Config,
        client: Any | None = None,
        ops: Any | None = None,
    ) -> None:
        super().__init__(config, client, ops)
        self.cache_seconds = config.scoring.timing_cache_seconds
        self._cached: TimingResult | None = None
        self._lock = asyncio.Lock()

    def build_user_message(self, market_snapshot: dict[str, Any] | None = None) -> str:
        payload = {
            "requested_unix": int(time.time()),
            "observations": market_snapshot or {},
        }
        return json.dumps(payload, ensure_ascii=False)

    def fallback(self, reason: str) -> TimingResult:
        return TimingResult.pessimistic(reason)

    # -- cache -------------------------------------------------------------

    def cache_is_fresh(self, now: float | None = None) -> bool:
        if self._cached is None:
            return False
        now = now or time.time()
        return (now - self._cached.fetched_at) < self.cache_seconds

    async def get(self, market_snapshot: dict[str, Any] | None = None) -> TimingResult:
        """Fresh market assessment: from cache or a new call.

        The lock is needed so a batch of tokens arriving at once does not fire
        three identical parallel requests.
        """
        if self.cache_is_fresh():
            return self._cached  # type: ignore[return-value]
        async with self._lock:
            if self.cache_is_fresh():
                return self._cached  # type: ignore[return-value]
            result: TimingResult = await self.run(market_snapshot)
            result.fetched_at = time.time()
            # Do not cache a pessimistic fallback: a failure should not lock
            # the market for a full 15 minutes.
            if "agent_failure" not in result.anomalies:
                self._cached = result
            return result

    def invalidate(self) -> None:
        self._cached = None

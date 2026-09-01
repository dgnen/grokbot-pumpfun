"""Agent 4: adversarial check.

The last line before money and the only one that runs on the strong
model. It is forbidden to look for reasons to buy: it receives the conclusions of all
previous agents and looks for where they contradict each other and what they
missed.

approve: false is a normal, expected outcome. A call error also
becomes approve: false.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from ..curve import buy_quote
from ..models import Analysis, CheckerResult
from .base import JSON_ONLY, GrokAgent

CHECKER_PROMPT = f"""You are a risk officer who signs off on or blocks
a memecoin buy. Your job is NOT to find reasons to buy. Your job
is to find reasons NOT to buy.

You are given a full review: metrics, the wallet auditor's conclusion, the
meme-potential score, market state, and the final scoring.

Look for:
1. Contradictions between signals. High meme potential with low organic
   buyer share. A good curve with concentration in the top 5. Strong scoring
   assembled from one component while the others fail.
2. Red flags that previous agents did not mark or undervalued.
3. Weak evidence: low auditor confidence, few trades,
   missing data paired with confident conclusions.
4. A market backdrop in which even a good token will not move.
5. The economics of the trade itself. The "plan" block contains what will actually
   happen: order size, what entry and immediate exit will cost
   (round_trip_cost_pct), how much the order itself will move the price
   (price_impact_pct), and where the exits sit. If the round-trip cost
   is comparable to the move the trade is aiming for, or the order
   moves the price by percentages — that is a reason to refuse, even when the token
   itself looks decent.

Decision rules:
- If in doubt — approve: false.
- Any triggered auditor flag with organic_buyer_share below 0.5 —
  approve: false.
- Do not approve on the basis of one strong component.

Response format:
{{
  "approve": true|false,
  "reason": "one or two sentences, the main reason for the decision",
  "flags": ["short labels of problems found"],
  "confidence": 0.0-1.0
}}

{JSON_ONLY}"""


class CheckerAgent(GrokAgent):
    name: ClassVar[str] = "checker"
    version: ClassVar[str] = "checker-2"
    prompt: ClassVar[str] = CHECKER_PROMPT
    result_model: ClassVar[type] = CheckerResult
    use_checker_model: ClassVar[bool] = True

    def build_user_message(self, analysis: Analysis) -> str:
        token = analysis.token
        payload = {
            "token": {
                "mint": token.mint,
                "name": token.name,
                "symbol": token.symbol,
                "description": token.description,
                "links": {
                    "twitter": token.twitter,
                    "telegram": token.telegram,
                    "website": token.website,
                },
                "age_seconds": round(token.age_seconds),
                "unique_buyers": token.unique_buyers,
                "curve_progress": round(token.curve_progress, 4),
            },
            "metrics": analysis.metrics.model_dump(),
            "auditor": analysis.audit.model_dump() if analysis.audit else None,
            "narrative": analysis.narrative.model_dump() if analysis.narrative else None,
            "timing": analysis.timing.model_dump() if analysis.timing else None,
            "scores": analysis.scores.model_dump(),
            "plan": self._plan(analysis),
        }
        return json.dumps(payload, ensure_ascii=False)

    def _plan(self, analysis: Analysis) -> dict[str, Any]:
        """Economics of the trade we are about to make."""
        risk = self.config.risk
        plan: dict[str, Any] = {
            "size_sol": round(analysis.plan.size_sol, 6) if analysis.plan else None,
            "risk_approved": analysis.plan.approved if analysis.plan else None,
            "round_trip_cost_pct": analysis.metrics.round_trip_cost_pct,
            "curve_liquidity_sol": analysis.metrics.curve_liquidity_sol,
            "take_profit_pct": risk.take_profit_pct,
            "stop_loss_pct": risk.stop_loss_pct,
            "max_hold_minutes": round(risk.max_hold_seconds / 60, 1),
        }
        if analysis.curve is not None and analysis.plan is not None:
            quote = buy_quote(analysis.curve, analysis.plan.size_sol,
                              self.config.market.trade_fee_pct)
            if quote.ok:
                plan["price_impact_pct"] = round(quote.impact_pct, 3)
        return plan

    def fallback(self, reason: str) -> CheckerResult:
        return CheckerResult.pessimistic(reason)

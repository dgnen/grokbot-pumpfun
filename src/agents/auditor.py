"""Agent 1: wallet audit.

Metrics see aggregates — shares, counters, averages. The auditor looks at the raw
trade stream and finds what aggregates lose: identical amounts,
intervals under five seconds, wallets that move together, sales
to oneself, the creator preparing to dump.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from ..models import AuditResult, Holder, Token, TokenMetrics, Trade
from .base import JSON_ONLY, GrokAgent

AUDITOR_PROMPT = f"""You are a forensic analyst of on-chain activity on pump.fun.
You are given raw trades and a holder list for a newly launched memecoin. Your job
is to find manipulation that aggregated metrics do not show.

Look specifically for:
1. Coordinated buys: similar-sized amounts, intervals between
   buys under 5 seconds, a repeating rhythm, wallets with a shared
   funding source or sequential addresses.
2. Wash trading: the same wallet buying and selling, circular
   transfers, volume without growth in unique holders.
3. Creator dump prep: the creator buying more from other addresses,
   concentration in related wallets, splitting the position before exit.
4. Bundled launch: buys in the same block or in the first second of the
   token's life from several addresses at once.
5. Share of organic buyers — those who did not fall into any of the schemes above.

Be skeptical. When data is lacking, set the flag to true and lower
confidence; do not give the token the benefit of the doubt.

Response format:
{{
  "coordinated_buying": true|false,
  "wash_trading": true|false,
  "creator_dump_prep": true|false,
  "bundled_launch": true|false,
  "organic_buyer_share": 0.0-1.0,
  "confidence": 0.0-1.0,
  "flags": ["short labels of what was found"],
  "reasoning": "2-3 sentences with specifics: addresses, amounts, intervals"
}}

{JSON_ONLY}"""


class AuditorAgent(GrokAgent):
    name: ClassVar[str] = "auditor"
    version: ClassVar[str] = "auditor-1"
    prompt: ClassVar[str] = AUDITOR_PROMPT
    result_model: ClassVar[type] = AuditResult

    def build_user_message(
        self,
        token: Token,
        trades: list[Trade],
        holders: list[Holder],
        metrics: TokenMetrics | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "token": {
                "mint": token.mint,
                "symbol": token.symbol,
                "creator": token.creator,
                "age_seconds": round(token.age_seconds),
                "curve_progress": round(token.curve_progress, 4),
            },
            "trades": [
                {
                    "wallet": t.wallet,
                    "side": "buy" if t.is_buy else "sell",
                    "sol": round(t.sol_amount, 6),
                    "t": round(t.timestamp - token.created_timestamp, 2)
                    if token.created_timestamp
                    else t.timestamp,
                    "slot": t.slot,
                }
                for t in trades
            ],
            "holders": [
                {"address": h.address, "share": round(h.share, 5), "is_creator": h.is_creator}
                for h in holders
            ],
        }
        if metrics is not None:
            payload["computed_metrics"] = metrics.model_dump()
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def fallback(self, reason: str) -> AuditResult:
        return AuditResult.pessimistic(reason)

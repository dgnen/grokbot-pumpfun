"""Agent 2: meme potential.

Evaluates not on-chain activity, but the meme itself: description, ticker, image, links. The
question is one — is there a chance this will spread, or is it another clone of yesterday's.
"""

from __future__ import annotations

import json
from typing import ClassVar

from ..models import NarrativeResult, Token
from .base import JSON_ONLY, GrokAgent

NARRATIVE_PROMPT = f"""You are an analyst of crypto-Twitter meme culture. You evaluate
the meme potential of a token just launched on pump.fun.

Give four independent scores from 0.0 to 1.0:
- trend_fit: fit with the current trend. 1.0 — the topic is being talked about right now,
  0.0 — a dead or exhausted topic, a copy of yesterday's hype.
- virality: virality of the meme itself. Is the name memorable, does the
  preview image work, is there a joke people want to forward.
- community_signals: signs of a living community. Real links to
  Twitter and Telegram, a meaningful description, traces of a community that
  existed before launch. Empty or fake links — low score.
- launch_timing: timeliness. First on a new topic — high, the hundredth
  clone — low, a premature move on an unripe topic — medium.

Missing data is a low score, not a medium one. Score clones of popular
tickers strictly.

Response format:
{{
  "trend_fit": 0.0-1.0,
  "virality": 0.0-1.0,
  "community_signals": 0.0-1.0,
  "launch_timing": 0.0-1.0,
  "reasoning": "2-3 sentences explaining the scores"
}}

{JSON_ONLY}"""


class NarrativeAgent(GrokAgent):
    name: ClassVar[str] = "narrative"
    version: ClassVar[str] = "narrative-1"
    prompt: ClassVar[str] = NARRATIVE_PROMPT
    result_model: ClassVar[type] = NarrativeResult

    def build_user_message(self, token: Token, market_context: str | None = None) -> str:
        payload = {
            "name": token.name,
            "symbol": token.symbol,
            "description": token.description,
            "image_uri": token.image_uri,
            "links": {
                "twitter": token.twitter,
                "telegram": token.telegram,
                "website": token.website,
            },
            "age_seconds": round(token.age_seconds),
            "unique_buyers": token.unique_buyers,
            "market_cap_sol": round(token.market_cap_sol, 3),
        }
        if market_context:
            payload["market_context"] = market_context
        return json.dumps(payload, ensure_ascii=False)

    def fallback(self, reason: str) -> NarrativeResult:
        return NarrativeResult.pessimistic(reason)

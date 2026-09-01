"""REST analyzer of token metrics. Second stage, also no LLM.

Three requests to the data provider run in parallel via asyncio.gather:
token card, top holders, recent trades. After that everything is
counted in code — agents are expensive, and handing them a token whose
creator holds half the supply is pointless.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import statistics
from typing import Any

import httpx

from .curve import CurveState, round_trip_cost_pct, state_from_any
from .models import Config, Holder, MarketConfig, Token, TokenMetrics, Trade

log = logging.getLogger(__name__)

# A buy in the first N seconds of the token's life counts as a snipe.
SNIPER_WINDOW_SECONDS = 15.0

# How many recent trades we pull for analysis.
TRADE_LIMIT = 200
HOLDER_LIMIT = 50

# Unconditional vetoes. A weighted sum dilutes them: a token with the
# creator on a quarter of supply used to score an acceptable risk on
# the back of a healthy curve and live socials. Those conditions are
# compensated by nothing, so they set risk to the max instead of
# adding to it.
CREATOR_SHARE_VETO = 0.25
TOP5_SHARE_VETO = 0.80


class Analyzer:
    """Pulls raw data and folds it into TokenMetrics."""

    def __init__(self, config: Config, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self.data = config.data
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> Analyzer:
        if self._client is None:
            headers = {"Accept": "application/json"}
            if self.data.key:
                headers["Authorization"] = f"Bearer {self.data.key}"
            self._client = httpx.AsyncClient(
                base_url=self.data.rest_url,
                timeout=self.data.request_timeout,
                headers=headers,
            )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("Analyzer used outside `async with`")
        return self._client

    # -- network -----------------------------------------------------------

    async def _get(self, path: str, **params: Any) -> Any:
        try:
            resp = await self.client.get(path, params=params)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            log.warning("request %s failed: %s", path, exc)
            return None

    async def fetch(self, mint: str) -> tuple[dict[str, Any], list[Holder], list[Trade]]:
        """Card, holders, and trades — three parallel requests."""
        info, holders_raw, trades_raw = await asyncio.gather(
            self._get(f"/coins/{mint}"),
            self._get(f"/coins/{mint}/holders", limit=HOLDER_LIMIT),
            self._get(f"/trades/all/{mint}", limit=TRADE_LIMIT),
        )
        return (
            info or {},
            [parse_holder(h) for h in (holders_raw or []) if isinstance(h, dict)],
            [parse_trade(t) for t in (trades_raw or []) if isinstance(t, dict)],
        )

    async def analyze(self, token: Token) -> TokenMetrics:
        """Full pass: hit the network and compute metrics."""
        info, holders, trades = await self.fetch(token.mint)
        enrich_token(token, info)
        curve = state_from_any(info, token.market_cap_sol)
        return compute_metrics(
            token, holders, trades, curve, self.config.market,
            planned_sol=self.config.risk.max_sol_per_trade,
        )

    def passes(self, metrics: TokenMetrics) -> tuple[bool, str]:
        """Cutoff by risk score and tradability. Returns (passed, reason)."""
        market = self.config.market
        if metrics.trade_count == 0:
            return False, "no_trade_data"
        if metrics.curve_liquidity_sol < market.min_curve_liquidity_sol:
            # No way out of a thin curve: your own sell will crash the price.
            return False, "curve_too_thin"
        if (metrics.round_trip_cost_pct
                and metrics.round_trip_cost_pct > market.max_round_trip_cost_pct):
            return False, "round_trip_too_expensive"
        if metrics.risk_score > self.config.filter.max_risk_score:
            return False, "risk_score_too_high"
        return True, "ok"


# --------------------------------------------------------------------------
# Parsing provider responses
# --------------------------------------------------------------------------


def parse_holder(raw: dict[str, Any]) -> Holder:
    amount = float(raw.get("amount") or raw.get("balance") or 0.0)
    share = raw.get("share")
    if share is None:
        pct = raw.get("percentage")
        share = float(pct) / 100.0 if pct is not None else 0.0
    return Holder(
        address=str(raw.get("address") or raw.get("wallet") or raw.get("owner") or ""),
        amount=amount,
        share=float(share),
        is_creator=bool(raw.get("is_creator") or raw.get("isCreator")),
    )


def parse_trade(raw: dict[str, Any]) -> Trade:
    ts = float(raw.get("timestamp") or 0.0)
    if ts > 1e11:  # milliseconds
        ts /= 1000.0
    is_buy = raw.get("is_buy")
    if is_buy is None:
        is_buy = str(raw.get("txType", "buy")).lower() == "buy"
    return Trade(
        signature=raw.get("signature") or raw.get("tx"),
        wallet=str(raw.get("user") or raw.get("wallet") or raw.get("traderPublicKey") or ""),
        is_buy=bool(is_buy),
        sol_amount=float(raw.get("sol_amount") or raw.get("solAmount") or 0.0),
        token_amount=float(raw.get("token_amount") or raw.get("tokenAmount") or 0.0),
        timestamp=ts,
        slot=raw.get("slot"),
    )


def enrich_token(token: Token, info: dict[str, Any]) -> Token:
    """Fill in the token with what the socket event did not have."""
    if not info:
        return token
    token.description = token.description or info.get("description")
    token.image_uri = token.image_uri or info.get("image_uri") or info.get("image")
    token.twitter = token.twitter or info.get("twitter")
    token.telegram = token.telegram or info.get("telegram")
    token.website = token.website or info.get("website")
    token.creator = token.creator or info.get("creator")
    if info.get("market_cap") is not None:
        token.market_cap_sol = float(info["market_cap"])
    if info.get("virtual_sol_reserves") is not None:
        token.sol_in_curve = float(info["virtual_sol_reserves"]) / 1e9
    return token


# --------------------------------------------------------------------------
# Metrics (a pure function, so it can be run without a network)
# --------------------------------------------------------------------------


def compute_metrics(
    token: Token,
    holders: list[Holder],
    trades: list[Trade],
    curve: CurveState | None = None,
    market: MarketConfig | None = None,
    planned_sol: float = 0.0,
) -> TokenMetrics:
    """Fold the raw material into metrics and a 0..10 risk score.

    If the curve state is known, the cost of getting in and out lands
    here too: on a thin curve it eats the move the trade was for, and
    that has to be visible before the decision, not after.
    """
    buys = [t for t in trades if t.is_buy]
    sells = [t for t in trades if not t.is_buy]
    wallets = {t.wallet for t in trades if t.wallet}

    top5_share = sum(h.share for h in sorted(holders, key=lambda h: h.share, reverse=True)[:5])
    creator_share = next(
        (
            h.share
            for h in holders
            if h.is_creator or (token.creator and h.address == token.creator)
        ),
        0.0,
    )

    sniper_count = _count_snipers(token, buys)
    diversity = _wallet_diversity(buys)
    socials = _social_signals(token)
    curve_health = _curve_health(buys)

    buy_sell_ratio = len(buys) / len(sells) if sells else float(len(buys))

    risk = _risk_score(
        top5_share=top5_share,
        creator_share=creator_share,
        sniper_count=sniper_count,
        diversity=diversity,
        socials=socials,
        curve_health=curve_health,
        trade_count=len(trades),
    )
    veto = _veto_reason(creator_share, top5_share)
    if veto:
        log.info("%s vetoed unconditionally: %s", token.mint[:8], veto)
        risk = 10.0

    liquidity = curve.real_sol if curve else 0.0
    cost = (
        round_trip_cost_pct(curve, planned_sol, (market or MarketConfig()).trade_fee_pct)
        if curve and planned_sol > 0
        else 0.0
    )

    return TokenMetrics(
        curve_liquidity_sol=round(liquidity, 4),
        round_trip_cost_pct=round(cost, 4),
        top5_share=round(min(1.0, top5_share), 4),
        creator_share=round(min(1.0, creator_share), 4),
        sniper_count=sniper_count,
        wallet_diversity=round(diversity, 4),
        social_signals=round(socials, 4),
        curve_health=round(curve_health, 4),
        buy_sell_ratio=round(buy_sell_ratio, 4),
        unique_wallets=len(wallets),
        trade_count=len(trades),
        risk_score=round(risk, 2),
    )


def _count_snipers(token: Token, buys: list[Trade]) -> int:
    if not token.created_timestamp:
        return 0
    cutoff = token.created_timestamp + SNIPER_WINDOW_SECONDS
    return len({t.wallet for t in buys if t.timestamp and t.timestamp <= cutoff})


def _wallet_diversity(buys: list[Trade]) -> float:
    """Share of unique wallets among buys, with a penalty for volume
    concentrated in one wallet."""
    if not buys:
        return 0.0
    wallets = [t.wallet for t in buys if t.wallet]
    if not wallets:
        return 0.0
    uniqueness = len(set(wallets)) / len(wallets)

    volume: dict[str, float] = {}
    for t in buys:
        volume[t.wallet] = volume.get(t.wallet, 0.0) + t.sol_amount
    total = sum(volume.values())
    concentration = max(volume.values()) / total if total else 1.0
    return max(0.0, min(1.0, uniqueness * (1.0 - concentration)))


def _social_signals(token: Token) -> float:
    score = 0.0
    if token.twitter:
        score += 0.4
    if token.telegram:
        score += 0.3
    if token.website:
        score += 0.2
    if token.description and len(token.description) > 20:
        score += 0.1
    return min(1.0, score)


def _curve_health(buys: list[Trade]) -> float:
    """An even curve fill beats a spike: we count the spread of buy
    sizes and the evenness of gaps between them."""
    if len(buys) < 3:
        return 0.0
    amounts = [t.sol_amount for t in buys if t.sol_amount > 0]
    if len(amounts) < 3:
        return 0.0

    mean = statistics.fmean(amounts)
    spread = statistics.pstdev(amounts) / mean if mean else 1.0
    size_health = max(0.0, min(1.0, 1.0 - abs(spread - 0.6)))

    stamps = sorted(t.timestamp for t in buys if t.timestamp)
    if len(stamps) >= 3:
        gaps = [b - a for a, b in itertools.pairwise(stamps) if b > a]
        if gaps:
            gap_mean = statistics.fmean(gaps)
            gap_spread = statistics.pstdev(gaps) / gap_mean if gap_mean else 1.0
            pace_health = max(0.0, min(1.0, 1.0 - gap_spread / 2.0))
        else:
            pace_health = 0.0
    else:
        pace_health = 0.0

    return max(0.0, min(1.0, 0.6 * size_health + 0.4 * pace_health))


def _veto_reason(creator_share: float, top5_share: float) -> str | None:
    """Condition under which the rest of the metrics no longer matter."""
    if creator_share >= CREATOR_SHARE_VETO:
        return f"creator holds {creator_share:.0%} of supply"
    if top5_share >= TOP5_SHARE_VETO:
        return f"top-5 wallets hold {top5_share:.0%}"
    return None


def _risk_score(
    *,
    top5_share: float,
    creator_share: float,
    sniper_count: int,
    diversity: float,
    socials: float,
    curve_health: float,
    trade_count: int,
) -> float:
    """0..10, higher is worse. Weights are set so that any single red
    flag (creator with half the supply, top-5 near 80%) by itself
    pushes the token past the cutoff."""
    risk = 0.0
    risk += min(3.0, top5_share * 3.75)          # >80% top-5 -> 3.0
    risk += min(3.0, creator_share * 10.0)       # >30% with the creator -> 3.0
    risk += min(2.0, sniper_count * 0.25)        # 8 snipers -> 2.0
    risk += (1.0 - diversity) * 1.5
    risk += (1.0 - curve_health) * 1.0
    risk += (1.0 - socials) * 0.5
    if trade_count < 10:
        risk += 1.0                              # little data, less trust
    return max(0.0, min(10.0, risk))

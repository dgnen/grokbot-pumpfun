"""Pydantic models of the pipeline.

Config models live here too: the config is read once at start and then
travels the pipeline as a typed object, not a dict.

Secrets are SecretStr: they will not land in logs, a traceback, or a
state dump, even if the model is printed whole somewhere.
"""

from __future__ import annotations

import copy
import os
import time
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from .curve import CurveState

# --------------------------------------------------------------------------
# Token and metrics
# --------------------------------------------------------------------------


class Token(BaseModel):
    """A new token on the pump.fun bonding curve."""

    model_config = ConfigDict(extra="allow")

    mint: str
    name: str | None = None
    symbol: str | None = None
    description: str | None = None
    image_uri: str | None = None
    metadata_uri: str | None = None
    twitter: str | None = None
    telegram: str | None = None
    website: str | None = None

    creator: str | None = None
    created_timestamp: float = 0.0

    unique_buyers: int = 0
    curve_progress: float = 0.0          # 0..1, share of the curve bought
    market_cap_sol: float = 0.0
    sol_in_curve: float = 0.0

    @property
    def age_seconds(self) -> float:
        if not self.created_timestamp:
            return 0.0
        return max(0.0, time.time() - self.created_timestamp)

    @property
    def has_metadata(self) -> bool:
        return bool(self.name) and bool(self.image_uri)

    @property
    def has_socials(self) -> bool:
        return any([self.twitter, self.telegram, self.website])


class Holder(BaseModel):
    """A token holder."""

    model_config = ConfigDict(extra="allow")

    address: str
    amount: float = 0.0
    share: float = 0.0                   # share of total supply, 0..1
    is_creator: bool = False


class Trade(BaseModel):
    """A trade on the curve."""

    model_config = ConfigDict(extra="allow")

    signature: str | None = None
    wallet: str
    is_buy: bool = True
    sol_amount: float = 0.0
    token_amount: float = 0.0
    timestamp: float = 0.0
    slot: int | None = None


class TokenMetrics(BaseModel):
    """Metrics computed in code in analyzer.py. No LLM."""

    top5_share: float = 0.0              # top-5 wallet share, 0..1
    creator_share: float = 0.0           # creator share, 0..1
    sniper_count: int = 0                # buys in the first seconds of life
    wallet_diversity: float = 0.0        # 0..1, higher is more diverse
    social_signals: float = 0.0          # 0..1, presence and quality of links
    curve_health: float = 0.0            # 0..1, evenness of curve fill
    buy_sell_ratio: float = 0.0
    unique_wallets: int = 0
    trade_count: int = 0
    curve_liquidity_sol: float = 0.0     # real SOL in the curve
    round_trip_cost_pct: float = 0.0     # cost of entry plus exit
    risk_score: float = 10.0             # 0..10, higher is worse

    @property
    def quality(self) -> float:
        """Composite metrics quality 0..1 — the `metrics` scoring component."""
        return max(0.0, min(1.0, 1.0 - self.risk_score / 10.0))


# --------------------------------------------------------------------------
# Agent responses
# --------------------------------------------------------------------------


class AuditResult(BaseModel):
    """Audit agent: patterns that aggregated metrics do not show."""

    coordinated_buying: bool = True
    wash_trading: bool = True
    creator_dump_prep: bool = True
    bundled_launch: bool = True
    organic_buyer_share: float = 0.0     # 0..1
    confidence: float = 0.0              # 0..1
    flags: list[str] = Field(default_factory=list)
    reasoning: str = ""

    @property
    def score(self) -> float:
        """0..1: organic share minus a penalty for each flag that fired."""
        penalties = sum(
            0.25
            for flag in (
                self.coordinated_buying,
                self.wash_trading,
                self.creator_dump_prep,
                self.bundled_launch,
            )
            if flag
        )
        return max(0.0, min(1.0, self.organic_buyer_share - penalties))

    @classmethod
    def pessimistic(cls, reason: str) -> AuditResult:
        """Fallback on error: everything is bad, no organic flow."""
        return cls(
            coordinated_buying=True,
            wash_trading=True,
            creator_dump_prep=True,
            bundled_launch=True,
            organic_buyer_share=0.0,
            confidence=0.0,
            flags=["agent_failure"],
            reasoning=reason,
        )


class NarrativeResult(BaseModel):
    """Narrative agent: meme potential."""

    trend_fit: float = 0.0               # trend fit, 0..1
    virality: float = 0.0                # virality, 0..1
    community_signals: float = 0.0       # signs of a living community, 0..1
    launch_timing: float = 0.0           # launch timeliness, 0..1
    reasoning: str = ""

    @property
    def score(self) -> float:
        return max(
            0.0,
            min(
                1.0,
                (self.trend_fit + self.virality + self.community_signals + self.launch_timing)
                / 4.0,
            ),
        )

    @classmethod
    def pessimistic(cls, reason: str) -> NarrativeResult:
        return cls(reasoning=reason)


class TimingResult(BaseModel):
    """Timing agent: market state, not a particular token."""

    market_sentiment: float = 0.0        # 0..1
    meme_season: float = 0.0             # 0..1
    volume_level: float = 0.0            # 0..1
    anomalies: list[str] = Field(default_factory=list)
    reasoning: str = ""
    fetched_at: float = 0.0

    @property
    def score(self) -> float:
        base = (self.market_sentiment + self.meme_season + self.volume_level) / 3.0
        penalty = 0.1 * len(self.anomalies)
        return max(0.0, min(1.0, base - penalty))

    @classmethod
    def pessimistic(cls, reason: str) -> TimingResult:
        return cls(anomalies=["agent_failure"], reasoning=reason)


class CheckerResult(BaseModel):
    """Adversarial checker: looks for reasons NOT to buy."""

    approve: bool = False
    reason: str = ""
    flags: list[str] = Field(default_factory=list)
    confidence: float = 0.0

    @classmethod
    def pessimistic(cls, reason: str) -> CheckerResult:
        """A check error is a reject, not a silent pass."""
        return cls(approve=False, reason=reason, flags=["agent_failure"], confidence=0.0)


# --------------------------------------------------------------------------
# Scoring, decision, position
# --------------------------------------------------------------------------


class Scores(BaseModel):
    """Broken-out scoring: components and the total."""

    audit: float = 0.0
    narrative: float = 0.0
    timing: float = 0.0
    metrics: float = 0.0
    total: float = 0.0


class TradeDecision(BaseModel):
    """Risk-gate decision."""

    approved: bool
    size_sol: float = 0.0
    reason: str = ""


class Analysis(BaseModel):
    """Everything the pipeline learned about the token by decision time."""

    token: Token
    metrics: TokenMetrics = Field(default_factory=TokenMetrics)
    curve: CurveState | None = None   # curve state at review time
    audit: AuditResult | None = None
    narrative: NarrativeResult | None = None
    timing: TimingResult | None = None
    scores: Scores = Field(default_factory=Scores)
    plan: TradeDecision | None = None   # what we intend to do
    checker: CheckerResult | None = None


class Position(BaseModel):
    """An open position."""

    mint: str
    symbol: str | None = None
    creator: str | None = None
    entry_price: float = 0.0
    peak_price: float = 0.0          # high since entry, for trailing
    sol_spent: float = 0.0
    token_amount: float = 0.0
    opened_at: float = 0.0
    tx_hash: str = ""
    score: float = 0.0
    realized_sol: float = 0.0        # proceeds from already closed parts
    partials: int = 0                # how many times we exited partially
    graduated: bool = False          # token moved to Raydium, curve is gone


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


class SecretModel(BaseModel):
    """Base for sections with secrets: assigning a string coerces to SecretStr."""

    model_config = ConfigDict(validate_assignment=True)


class GrokConfig(SecretModel):
    api_key: SecretStr = SecretStr("")
    base_url: str = "https://api.x.ai/v1/chat/completions"
    fast_model: str = "grok-4-fast"
    checker_model: str = "grok-4"
    timeout_seconds: float = 30.0
    max_retries: int = 3
    retry_base_delay: float = 1.0

    @property
    def key(self) -> str:
        return self.api_key.get_secret_value()


class JitoConfig(BaseModel):
    enabled: bool = True
    block_engine_url: str = "https://mainnet.block-engine.jito.wtf/api/v1/bundles"
    tip_lamports: int = 1_000_000


class SolanaConfig(SecretModel):
    rpc_url: str = "https://api.mainnet-beta.solana.com"
    wallet_private_key: SecretStr = SecretStr("")
    jito: JitoConfig = Field(default_factory=JitoConfig)

    @property
    def wallet_key(self) -> str:
        return self.wallet_private_key.get_secret_value()


class DataConfig(SecretModel):
    api_key: SecretStr = SecretStr("")
    rest_url: str = "https://frontend-api.pump.fun"
    ws_url: str = "wss://pumpportal.fun/api/data"
    request_timeout: float = 10.0

    @property
    def key(self) -> str:
        return self.api_key.get_secret_value()


class MarketConfig(BaseModel):
    """Microstructure: what a trade costs and which curve is even worth touching."""

    trade_fee_pct: float = 1.0            # venue fee on every trade
    max_price_impact_pct: float = 3.0     # ceiling on our own order's price impact
    min_curve_liquidity_sol: float = 3.0  # thinner — nowhere to exit
    max_round_trip_cost_pct: float = 5.0  # entry plus exit dearer than this — skip


class RiskConfig(BaseModel):
    max_sol_per_trade: float = 0.5
    daily_loss_limit_sol: float = 2.0
    max_trades_per_day: int = 20
    max_open_positions: int = 3
    max_total_exposure_sol: float = 1.5   # this many SOL in the market at once, max
    cooldown_after_losses: int = 3        # this many losses in a row — pause; 0 disables
    cooldown_minutes: float = 30.0
    stop_loss_pct: float = 30.0
    stop_loss_poll_seconds: float = 15.0
    # Exits up and on time. 0 in any of them disables the rule.
    take_profit_pct: float = 120.0
    take_profit_fraction: float = 0.6     # what share to sell on take-profit
    trailing_stop_pct: float = 35.0       # pullback from the peak, counted only above entry
    max_hold_seconds: float = 3600.0      # a memecoin that did not move in an hour will not


class FilterConfig(BaseModel):
    min_unique_buyers: int = 5
    max_curve_progress: float = 0.40
    require_metadata: bool = True
    min_age_seconds: float = 120.0
    max_risk_score: float = 7.0
    min_total_score: float = 0.65
    # Memory of creators. 0 in block_creator_after_rugs disables the rule.
    block_creator_after_rugs: int = 1
    rug_loss_pct: float = 60.0            # a loss from this level counts as a rug
    one_position_per_creator: bool = True
    forget_creators_after_days: float = 30.0


class ScoringWeights(BaseModel):
    audit: float = 0.30
    narrative: float = 0.25
    timing: float = 0.15
    metrics: float = 0.30


class ScoringConfig(BaseModel):
    weights: ScoringWeights = Field(default_factory=ScoringWeights)
    timing_cache_seconds: float = 900.0


class AlertsConfig(SecretModel):
    """Outbound notifications. Empty webhook_url — off."""

    # A token is usually baked into the URL, so this is a secret, not a string.
    webhook_url: SecretStr = SecretStr("")
    events: list[str] = Field(
        default_factory=lambda: [
            "started", "buy", "close", "rug", "breaker", "halted", "blind", "cooldown"
        ]
    )
    timeout_seconds: float = 10.0
    max_per_minute: int = 20


class LoggingConfig(BaseModel):
    path: str = "logs/trades.jsonl"
    level: str = "INFO"
    max_bytes: int = 50_000_000          # JSONL rotation, 0 — do not rotate
    backups: int = 5


class OpsConfig(BaseModel):
    """Ops settings: what a process that lives for days needs."""

    state_path: str = "state/pipeline.json"   # survives a restart
    reputation_path: str = "state/creators.json"   # memory of creators
    health_port: int = 0                      # 0 — health endpoint off
    health_host: str = "127.0.0.1"
    heartbeat_seconds: float = 300.0          # liveness line in the log
    shutdown_grace_seconds: float = 30.0      # how long to wait for in-flight tokens
    max_grok_calls_per_day: int = 2000        # ceiling on agent spend
    grok_max_concurrency: int = 4
    grok_calls_per_minute: int = 60
    breaker_failures: int = 8                 # in a row, until the circuit opens
    breaker_cooldown_seconds: float = 120.0


# --------------------------------------------------------------------------
# Config as a whole
# --------------------------------------------------------------------------

# Placeholders from config.example.yaml. Caught before start, not in prod.
PLACEHOLDER_MARKERS = ("YOUR", "CHANGEME", "xxx", "<", "example")

# Environment variables beat the file: in a container secrets arrive this
# way, not by editing yaml inside the image.
ENV_OVERRIDES: dict[str, tuple[str, ...]] = {
    "GROKBOT_MODE": ("mode",),
    "GROKBOT_GROK_API_KEY": ("grok", "api_key"),
    "GROKBOT_DATA_API_KEY": ("data", "api_key"),
    "GROKBOT_WALLET_PRIVATE_KEY": ("solana", "wallet_private_key"),
    "GROKBOT_RPC_URL": ("solana", "rpc_url"),
    "GROKBOT_LOG_PATH": ("logging", "path"),
    "GROKBOT_LOG_LEVEL": ("logging", "level"),
    "GROKBOT_STATE_PATH": ("ops", "state_path"),
    "GROKBOT_HEALTH_PORT": ("ops", "health_port"),
    "GROKBOT_ALERT_WEBHOOK": ("alerts", "webhook_url"),
}


# Events the pipeline can send to a webhook.
ALERT_EVENTS = frozenset({
    "started", "stopped", "buy", "close", "rug",
    "breaker", "halted", "stalled", "blind", "cooldown",
})


class ConfigError(RuntimeError):
    """Config is not fit to start. The list of problems is in the argument."""


def is_placeholder(value: str) -> bool:
    """A value from the template, not a real secret."""
    if not value.strip():
        return True
    return any(marker.lower() in value.lower() for marker in PLACEHOLDER_MARKERS)


def mask(secret: str) -> str:
    """How a secret looks in logs: the tail can be recognized, not used."""
    if not secret:
        return "<empty>"
    if len(secret) <= 8:
        return "***"
    return f"{secret[:4]}…{secret[-4:]} ({len(secret)} chars)"


def _deep_set(target: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    node = target
    for key in path[:-1]:
        child = node.get(key)
        if not isinstance(child, dict):
            child = {}
            node[key] = child
        node = child
    node[path[-1]] = value


class Config(BaseModel):
    mode: Literal["dry-run", "live"] = "dry-run"
    grok: GrokConfig = Field(default_factory=GrokConfig)
    solana: SolanaConfig = Field(default_factory=SolanaConfig)
    data: DataConfig = Field(default_factory=DataConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    filter: FilterConfig = Field(default_factory=FilterConfig)
    market: MarketConfig = Field(default_factory=MarketConfig)
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    alerts: AlertsConfig = Field(default_factory=AlertsConfig)
    ops: OpsConfig = Field(default_factory=OpsConfig)

    @property
    def is_live(self) -> bool:
        return self.mode == "live"

    # -- load --------------------------------------------------------------

    @classmethod
    def load(
        cls,
        path: str | Path = "config.yaml",
        env: dict[str, str] | None = None,
    ) -> Config:
        """Read yaml and overlay environment variables on top."""
        raw: dict[str, Any] = yaml.safe_load(Path(path).read_text()) or {}
        return cls.from_raw(raw, env)

    @classmethod
    def from_raw(cls, raw: dict[str, Any], env: dict[str, str] | None = None) -> Config:
        environ = os.environ if env is None else env
        # Deep copy: overlaying env must not mutate the caller's dict.
        merged = copy.deepcopy(raw)
        for name, path in ENV_OVERRIDES.items():
            value = environ.get(name)
            if value is not None and value != "":
                _deep_set(merged, path, value)
        return cls.model_validate(merged)

    # -- checks before start ----------------------------------------------

    def problems(self) -> tuple[list[str], list[str]]:
        """(errors, warnings). An error — we do not start at all."""
        errors: list[str] = []
        warnings: list[str] = []

        if is_placeholder(self.grok.key):
            errors.append(
                "grok.api_key is not set (or is still a placeholder) — "
                "no agent works without it, including dry-run"
            )
        for name in ("fast_model", "checker_model"):
            if not getattr(self.grok, name).strip():
                errors.append(f"grok.{name} is empty")
        if self.grok.timeout_seconds <= 0:
            errors.append("grok.timeout_seconds must be greater than zero")
        if self.grok.max_retries < 1:
            errors.append("grok.max_retries must be at least 1")

        risk = self.risk
        if risk.max_sol_per_trade <= 0:
            errors.append("risk.max_sol_per_trade must be greater than zero")
        if risk.daily_loss_limit_sol <= 0:
            errors.append("risk.daily_loss_limit_sol must be greater than zero")
        if risk.max_trades_per_day < 1:
            errors.append("risk.max_trades_per_day must be at least 1")
        if risk.max_open_positions < 1:
            errors.append("risk.max_open_positions must be at least 1")
        if risk.max_total_exposure_sol <= 0:
            errors.append("risk.max_total_exposure_sol must be greater than zero")
        if risk.cooldown_after_losses < 0:
            errors.append("risk.cooldown_after_losses cannot be negative")
        if risk.cooldown_minutes < 0:
            errors.append("risk.cooldown_minutes cannot be negative")
        if risk.max_total_exposure_sol < risk.max_sol_per_trade:
            warnings.append(
                f"risk.max_total_exposure_sol ({risk.max_total_exposure_sol}) is less "
                f"than the one-trade ceiling ({risk.max_sol_per_trade}) — size will "
                "always be cut by the overall limit"
            )
        if not 0 < risk.stop_loss_pct < 100:
            errors.append("risk.stop_loss_pct must be in the interval (0, 100)")
        if risk.stop_loss_poll_seconds <= 0:
            errors.append("risk.stop_loss_poll_seconds must be greater than zero")
        if risk.take_profit_pct < 0:
            errors.append("risk.take_profit_pct cannot be negative")
        if not 0 < risk.take_profit_fraction <= 1:
            errors.append("risk.take_profit_fraction must be in the interval (0, 1]")
        if not 0 <= risk.trailing_stop_pct < 100:
            errors.append("risk.trailing_stop_pct must be in the interval [0, 100)")
        if risk.max_hold_seconds < 0:
            errors.append("risk.max_hold_seconds cannot be negative")
        if risk.take_profit_pct and risk.take_profit_pct <= risk.stop_loss_pct:
            warnings.append(
                f"take_profit_pct ({risk.take_profit_pct}) is not greater than "
                f"stop_loss_pct ({risk.stop_loss_pct}) — on that asymmetry a "
                "winning streak will not cover a losing one"
            )

        flt = self.filter
        if not 0.0 <= flt.min_total_score <= 1.0:
            errors.append("filter.min_total_score must be in the interval [0, 1]")
        if not 0.0 < flt.max_curve_progress <= 1.0:
            errors.append("filter.max_curve_progress must be in the interval (0, 1]")
        if not 0.0 <= flt.max_risk_score <= 10.0:
            errors.append("filter.max_risk_score must be in the interval [0, 10]")
        if flt.min_age_seconds < 0:
            errors.append("filter.min_age_seconds cannot be negative")
        if not 0 < flt.rug_loss_pct <= 100:
            errors.append("filter.rug_loss_pct must be in the interval (0, 100]")
        if flt.block_creator_after_rugs < 0:
            errors.append("filter.block_creator_after_rugs cannot be negative")

        market = self.market
        if not 0 <= market.trade_fee_pct < 100:
            errors.append("market.trade_fee_pct must be in the interval [0, 100)")
        if market.max_price_impact_pct <= 0:
            errors.append("market.max_price_impact_pct must be greater than zero")
        if market.max_price_impact_pct <= market.trade_fee_pct:
            errors.append(
                f"market.max_price_impact_pct ({market.max_price_impact_pct}) is not "
                f"greater than the fee ({market.trade_fee_pct}): the fee alone "
                "already spends the whole allowance, and no order will pass"
            )
        if market.min_curve_liquidity_sol < 0:
            errors.append("market.min_curve_liquidity_sol cannot be negative")
        if market.max_round_trip_cost_pct <= market.trade_fee_pct * 2:
            warnings.append(
                f"market.max_round_trip_cost_pct ({market.max_round_trip_cost_pct}) "
                f"is not greater than two fees ({market.trade_fee_pct * 2}) — "
                "at that threshold no trade will pass"
            )

        weights = self.scoring.weights
        if sum(max(0.0, w) for w in weights.model_dump().values()) <= 0:
            errors.append("all scoring.weights are zero or negative")

        unknown = [e for e in self.alerts.events if e not in ALERT_EVENTS]
        if unknown:
            errors.append(
                f"alerts.events: unknown events {unknown}; "
                f"allowed {sorted(ALERT_EVENTS)}"
            )
        if self.alerts.max_per_minute < 1:
            errors.append("alerts.max_per_minute must be at least 1")

        if self.ops.max_grok_calls_per_day < 1:
            errors.append("ops.max_grok_calls_per_day must be at least 1")
        if self.ops.grok_max_concurrency < 1:
            errors.append("ops.grok_max_concurrency must be at least 1")

        if self.is_live:
            if is_placeholder(self.solana.wallet_key):
                errors.append("mode: live, but solana.wallet_private_key is not set")
            if not self.solana.rpc_url.startswith("https://"):
                errors.append("solana.rpc_url in live must be https")
            if risk.max_sol_per_trade > 5.0:
                warnings.append(
                    f"risk.max_sol_per_trade = {risk.max_sol_per_trade} SOL — "
                    "large for a memecoin on the curve, double-check"
                )

        if is_placeholder(self.data.key):
            warnings.append(
                "data.api_key is not set — the public endpoint serves data with "
                "limits, there will be gaps on the stream"
            )
        if flt.min_total_score < 0.5:
            warnings.append(
                f"filter.min_total_score = {flt.min_total_score} — a low bar, "
                "noticeably more tokens will reach the checker and spend will grow"
            )
        return errors, warnings

    def check_ready(self) -> list[str]:
        """Raise ConfigError if we cannot start. Return warnings."""
        errors, warnings = self.problems()
        if errors:
            raise ConfigError(
                "config is not fit to start:\n  - " + "\n  - ".join(errors)
            )
        return warnings

    # -- output ------------------------------------------------------------

    def redacted(self) -> dict[str, Any]:
        """Config dump safe for the log: secrets are masked."""
        data = self.model_dump(mode="json")
        data["grok"]["api_key"] = mask(self.grok.key)
        data["data"]["api_key"] = mask(self.data.key)
        data["solana"]["wallet_private_key"] = mask(self.solana.wallet_key)
        data["alerts"]["webhook_url"] = mask(self.alerts.webhook_url.get_secret_value())
        return data

    def summary(self) -> str:
        """One line for the start log."""
        return (
            f"mode={self.mode} "
            f"grok={self.grok.fast_model}/{self.grok.checker_model} "
            f"key={mask(self.grok.key)} "
            f"risk={self.risk.max_sol_per_trade}SOL/trade "
            f"limit={self.risk.daily_loss_limit_sol}SOL/day "
            f"score>={self.filter.min_total_score} "
            f"state={self.ops.state_path}"
        )

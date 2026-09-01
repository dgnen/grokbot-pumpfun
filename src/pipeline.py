"""Orchestrator: wires every stage into one flow.

    monitor → analyzer → auditor → narrative → timing → scoring →
    checker → risk-gate → execution

Each stage either passes the token on or writes a skip with a reason and
stops there. Expensive stages sit after cheap ones: only what survived
the code filter, metrics, three fast agents, and the scoring threshold
reaches grok-4.

The process is meant to run for days: state survives a restart, SIGTERM
stops it cleanly, Grok spend is capped, and liveness is visible outside
via /healthz.

Run:
    python -m src.pipeline --config config.yaml
    python -m src.pipeline --config config.yaml --check          # check only
    python -m src.pipeline --config config.yaml --i-understand-the-risk   # for live
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from .agents import AuditorAgent, CheckerAgent, NarrativeAgent, TimingAgent
from .alerts import Notifier
from .analyzer import Analyzer, compute_metrics, enrich_token
from .curve import max_sol_for_impact, state_from_any
from .executor import BaseExecutor, build_executor, new_position
from .log import TradeLog, read_log, setup_logging
from .market import MarketPulse
from .models import Analysis, Config, ConfigError, Position, Token
from .monitor import LaunchMonitor
from .ops import (
    GrokOps,
    HealthServer,
    Heartbeat,
    Metrics,
    cancel_and_wait,
    drain,
    install_signal_handlers,
)
from .reputation import ReputationBook
from .risk import PositionWatcher, RiskManager, Tick
from .scoring import compute_scores, passes_threshold, weakest_component
from .state import InstanceLock, StateStore

log = logging.getLogger("pipeline")

# How many tokens we review at once. More — we hit Grok limits.
MAX_CONCURRENT_TOKENS = 4

# This long with no socket event — we treat the stream as stalled and
# report it in /healthz. Launches on pump.fun run continuously.
STALL_SECONDS = 600.0


def unmatched_intents(records: list[dict[str, Any]], tail: int = 200) -> list[str]:
    """Buy intents that were not followed by a buy or a reject.

    We only look at the log tail: older mismatches have already been
    reviewed by hand, and we care about what the last run left hanging.
    """
    pending: dict[str, bool] = {}
    for record in records[-tail:]:
        mint = record.get("mint")
        if not mint:
            continue
        kind = record.get("type")
        if kind == "intent":
            pending[mint] = True
        elif kind in ("buy", "skip", "close"):
            pending.pop(mint, None)
    return list(pending)


class Pipeline:
    """Holds agents, risk state, and the log; runs tokens through the stages."""

    def __init__(self, config: Config, store: StateStore | None = None) -> None:
        self.config = config
        self.metrics = Metrics()
        self.trade_log = TradeLog.from_config(config)
        self.store = store if store is not None else StateStore(config.ops.state_path)
        self.lock = InstanceLock(config.ops.state_path)
        self.risk = RiskManager(config, store=self.store)
        self.reputation = ReputationBook.load(config.ops.reputation_path)
        self.notifier = Notifier(config.alerts)
        self.pulse = MarketPulse()
        self.grok_ops = GrokOps(config, self.metrics)

        self._grok_client = httpx.AsyncClient(
            timeout=config.grok.timeout_seconds,
            limits=httpx.Limits(max_connections=config.ops.grok_max_concurrency * 2),
        )
        self.auditor = AuditorAgent(config, self._grok_client, self.grok_ops)
        self.narrative = NarrativeAgent(config, self._grok_client, self.grok_ops)
        self.timing = TimingAgent(config, self._grok_client, self.grok_ops)
        self.checker = CheckerAgent(config, self._grok_client, self.grok_ops)

        self.analyzer = Analyzer(config)
        self.executor: BaseExecutor = build_executor(config)
        self.monitor = LaunchMonitor(config, on_skip=self._log_monitor_skip)
        self.watcher = PositionWatcher(self.risk, self._price, self._sell)
        self.health = HealthServer(
            config.ops.health_host, config.ops.health_port, self.status, self.metrics
        )
        self.heartbeat = Heartbeat(config.ops.heartbeat_seconds, self._heartbeat_status)

        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_TOKENS)
        self._tasks: set[asyncio.Task] = set()
        self._stopping = asyncio.Event()
        self._started_at = time.time()
        self._last_event_at = time.time()
        self._alerted: dict[str, bool] = {
            "breaker": False, "halted": False, "stalled": False,
            "blind": False, "cooldown": False,
        }

    # -- lifecycle ---------------------------------------------------------

    async def __aenter__(self) -> Pipeline:
        await self.analyzer.__aenter__()
        await self.executor.__aenter__()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.watcher.stop()
        await self.heartbeat.stop()
        await self.health.stop()
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.analyzer.__aexit__(*exc)
        await self.executor.__aexit__(*exc)
        await self._grok_client.aclose()

    def restore(self) -> None:
        """Restore the previous run's state: positions, daily limits, reputation.

        Past trade outcomes are restored too: the timing agent must not
        treat history as empty after every restart.
        """
        records = list(read_log(self.config.logging.path))
        seeded = self.pulse.seed_from_log(records, self.config.filter.rug_loss_pct)
        if seeded:
            log.info("seeded market memory with %d past outcomes", seeded)
        for mint in unmatched_intents(records):
            log.error("buy intent for %s left on disk with no buy record — "
                      "the process may have died mid-execution. Check the wallet: "
                      "a position may exist that the bot does not know about", mint[:8])
            self.notifier.notify(
                "stalled", f"unclosed buy intent for {mint[:8]} after restart",
                mint=mint,
            )
        forgotten = self.reputation.forget_older_than(self.config.filter.forget_creators_after_days)
        log.info("reputation book: %s%s", self.reputation.summary(),
                 f", forgot {forgotten} stale" if forgotten else "")
        if self.risk.restore():
            # Grok call budget continues from the same place, otherwise
            # a restart loop would burn the daily limit in an hour.
            self.grok_ops.budget.spent = self.risk.grok_calls_today
            for mint, position in self.risk.positions.items():
                log.info("position under watch after restart: %s, entry %.12f, %.4f SOL",
                         mint[:8], position.entry_price, position.sol_spent)

    async def serve(self) -> int:
        """Full lifecycle: start, run, clean shutdown."""
        if not self.lock.acquire():
            log.error("start cancelled: state is held by another process. "
                      "Two bots on one wallet will overwrite each other's positions")
            return 2
        install_signal_handlers(self.request_stop)
        self.restore()
        await self.health.start()
        self.watcher.start()
        self.heartbeat.start()
        log.info("pipeline started: %s", self.config.summary())
        self.notifier.notify(
            "started", f"pipeline started, mode {self.config.mode}",
            open_positions=self.risk.open_count, mode=self.config.mode,
        )

        consumer = asyncio.create_task(self._consume(), name="monitor-consumer")
        stopper = asyncio.create_task(self._stopping.wait(), name="stop-signal")
        await asyncio.wait({consumer, stopper}, return_when=asyncio.FIRST_COMPLETED)

        await cancel_and_wait(stopper)
        await cancel_and_wait(consumer)
        await self.shutdown()
        return 0

    def request_stop(self, reason: str = "stop") -> None:
        """Called by the signal handler. A second signal does not speed up exit."""
        if not self._stopping.is_set():
            log.info("got %s — stopping cleanly", reason)
            self._stopping.set()
        else:
            log.warning("%s again, already stopping", reason)

    async def shutdown(self) -> None:
        """Finish in-flight work, persist state, close connections."""
        done, cancelled = await drain(set(self._tasks), self.config.ops.shutdown_grace_seconds)
        self._tasks.clear()
        self._sync_counters()
        self.risk.persist()
        self._save_reputation()
        await self.watcher.stop()
        await self.heartbeat.stop()
        await self.health.stop()
        self.lock.release()
        log.info(
            "stopped: finished %d, cancelled %d, open positions %d, "
            "trades today %d, PnL %+.4f SOL, Grok calls %d",
            done, cancelled, self.risk.open_count, self.risk.trades_today,
            self.risk.realized_pnl_sol, self.grok_ops.budget.spent,
        )
        self.notifier.notify(
            "stopped",
            f"stopped: open positions {self.risk.open_count}, "
            f"PnL today {self.risk.realized_pnl_sol:+.4f} SOL",
            open_positions=self.risk.open_count,
        )
        await self.notifier.aclose()
        if self.risk.positions:
            log.warning("positions remain open: %s — stop-loss does not work "
                        "until the process is up again",
                        ", ".join(m[:8] for m in self.risk.positions))

    async def _consume(self) -> None:
        """Read the monitor stream and hand tokens off for processing."""
        async for token in self.monitor.stream():
            self._last_event_at = time.time()
            self.metrics.inc("tokens_seen")
            self.pulse.record_launch(token.sol_in_curve)
            self._check_transitions()
            if self._stopping.is_set():
                break
            if self.risk.halted:
                self.metrics.inc("skip_risk_halted")
                self.trade_log.skip(token, stage="risk", reason="daily_loss_limit_hit")
                continue
            task = asyncio.create_task(self._guarded(token), name=f"token-{token.mint[:8]}")
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _guarded(self, token: Token) -> None:
        async with self._semaphore:
            try:
                await self.process(token)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("token %s crashed processing: %s", token.mint, exc)
                self.metrics.inc("errors")
                self.trade_log.skip(token, stage="pipeline",
                                    reason="internal_error", detail=str(exc))

    # -- stages ------------------------------------------------------------

    async def process(self, token: Token) -> Analysis | None:
        """One token from metrics to buy. None if skipped."""
        log.info("reviewing %s (%s), buyers %d",
                 token.symbol or "?", token.mint[:8], token.unique_buyers)
        self.reputation.observe(token.creator)
        self.pulse.record_passed()

        # 1.5. Memory of the creator. Free stage before every paid one:
        # an address that already rugged goes no further.
        blocked = self._creator_verdict(token)
        if blocked:
            return self._reject(Analysis(token=token), stage="reputation",
                                reason="creator_blocked", detail=blocked)

        # 2. Analyzer: network in parallel, metrics in code.
        info, holders, trades = await self.analyzer.fetch(token.mint)
        enrich_token(token, info)
        curve = state_from_any(info, token.market_cap_sol)
        metrics = compute_metrics(
            token, holders, trades, curve, self.config.market,
            planned_sol=self.config.risk.max_sol_per_trade,
        )
        analysis = Analysis(token=token, metrics=metrics, curve=curve)

        ok, reason = self.analyzer.passes(metrics)
        if not ok:
            return self._reject(analysis, stage="analyzer", reason=reason,
                                detail=f"risk_score={metrics.risk_score}")

        # 3-5. Fast agents in parallel. Timing usually comes from cache.
        analysis.audit, analysis.narrative, analysis.timing = await asyncio.gather(
            self.auditor.run(token, trades, holders, metrics),
            self.narrative.run(token),
            self.timing.get(self._market_snapshot()),
        )

        # 6. Scoring in code.
        analysis.scores = compute_scores(analysis, self.config)
        ok, reason = passes_threshold(analysis.scores, self.config)
        if not ok:
            name, value = weakest_component(analysis.scores)
            return self._reject(analysis, stage="scoring", reason=reason,
                                detail=f"weakest {name}={value:.3f}")

        # The trade plan is computed before the checker: it needs to see
        # what entry and exit will cost, not only how good the token is.
        liquidity_cap = (
            max_sol_for_impact(curve, self.config.market.max_price_impact_pct,
                               self.config.market.trade_fee_pct)
            if curve else 0.0
        )
        analysis.plan = self.risk.evaluate(token.mint, analysis.scores.total, liquidity_cap)

        # 7. Adversarial checker on the strong model.
        analysis.checker = await self.checker.run(analysis)
        if not analysis.checker.approve:
            return self._reject(
                analysis, stage="checker", reason="checker_rejected",
                detail=f"{analysis.checker.reason} [{', '.join(analysis.checker.flags)}]",
            )

        # 8. Risk gate. Recalculated after the checker: while the strong
        # model thought, other positions may have opened and limits changed.
        decision = self.risk.evaluate(token.mint, analysis.scores.total, liquidity_cap)
        if not decision.approved:
            return self._reject(analysis, stage="risk", reason=decision.reason)

        # 9. Execution. Intent is recorded before send: if the process
        # dies between execution and booking, a trace stays on disk.
        self._sync_counters()
        self.trade_log.intent(analysis, size_sol=decision.size_sol)
        try:
            result = await self.executor.buy(token, decision.size_sol)
        except NotImplementedError as exc:
            # Live executor is a stub by design: this must be loud,
            # not swallowed as an ordinary stage error.
            log.error("execution not implemented: %s", exc)
            return self._reject(analysis, stage="executor", reason="executor_not_implemented",
                                detail=str(exc))
        if not result.ok:
            return self._reject(analysis, stage="executor", reason="execution_failed",
                                detail=result.error)

        position = new_position(token, result, analysis.scores.total)
        self._sync_counters()
        self.risk.register_open(position)
        self.reputation.record_open(token.creator)
        self.trade_log.buy(analysis, size_sol=decision.size_sol,
                           entry_price=result.price, tx_hash=result.tx_hash,
                           prompt_versions=self.prompt_versions())
        self.metrics.inc("buys")
        self.pulse.record_bought()
        self.metrics.gauge("open_positions", self.risk.open_count)
        log.info("BOUGHT %s for %.4f SOL, score %.3f, tx %s",
                 token.symbol or token.mint[:8], decision.size_sol,
                 analysis.scores.total, result.tx_hash)
        self.notifier.notify(
            "buy",
            f"bought {token.symbol or token.mint[:8]} for {decision.size_sol:.4f} SOL, "
            f"score {analysis.scores.total:.3f}",
            mint=token.mint, size_sol=decision.size_sol,
            score=analysis.scores.total, tx=result.tx_hash,
        )
        return analysis

    def _reject(
        self, analysis: Analysis, *, stage: str, reason: str, detail: str | None = None
    ) -> Analysis | None:
        """Stage reject: metric, log record, end of review."""
        self.metrics.inc(f"skip_{stage}")
        self.trade_log.skip(
            analysis.token, stage=stage, reason=reason, detail=detail,
            scores=analysis.scores if analysis.scores.total else None,
        )
        return None

    # -- state and observability -------------------------------------------

    def _heartbeat_status(self) -> dict[str, Any]:
        """Snapshot for the heartbeat, catching transitions along the way.

        During a stall there are no socket events, and no other reason
        to notice it either: the heartbeat is the only remaining tick.
        """
        status = self.status()
        self._check_transitions(status)
        return status

    def _check_transitions(self, status: dict[str, Any] | None = None) -> None:
        """Send a notification on a state change, not on every tick."""
        if not self.notifier.enabled:
            return
        status = status or self.status()
        edges = {
            "breaker": (
                status["breaker"] == "open",
                "Grok circuit opened — pipeline is not buying",
                "Grok circuit closed, work continues",
            ),
            "halted": (
                bool(status["halted"]),
                f"daily loss limit spent ({self.risk.daily_loss:.4f} SOL), "
                "no trading today",
                "new day, trading resumed",
            ),
            "stalled": (
                bool(status["stalled"]),
                "launch stream stalled: no events from the socket",
                "launch stream recovered",
            ),
            "cooldown": (
                bool(status["cooldown_left_seconds"]),
                f"{self.risk.losing_streak} losses in a row — pause for "
                f"{status['cooldown_left_seconds'] / 60:.0f} min",
                "pause after a losing streak ended",
            ),
            "blind": (
                bool(status["blind_positions"]),
                f"no prices for {status['blind_positions']} open positions — "
                "stop-loss and take-profit on them are not working now",
                "prices for positions are arriving again",
            ),
        }
        for name, (active, on_text, off_text) in edges.items():
            if active and not self._alerted[name]:
                self.notifier.notify(name, on_text, **{name: True})
            elif not active and self._alerted[name]:
                self.notifier.notify(name, off_text, **{name: False})
            self._alerted[name] = active

    def _creator_verdict(self, token: Token) -> str | None:
        """Reason not to deal with this token's creator, or None."""
        flt = self.config.filter
        verdict = self.reputation.verdict(token.creator, flt.block_creator_after_rugs)
        if verdict:
            return verdict
        if flt.one_position_per_creator and token.creator:
            same = [p.mint[:8] for p in self.risk.positions.values()
                    if p.creator == token.creator]
            if same:
                # Two tokens from one deployer are one bet, not two:
                # they usually rug together.
                return f"creator already has an open position ({', '.join(same)})"
        return None

    def _save_reputation(self) -> None:
        self.reputation.save(self.config.ops.reputation_path)

    def prompt_versions(self) -> dict[str, str]:
        """Prompt versions this decision was made with.

        Editing a prompt changes agent behavior, while log records look
        the same. Without this mark, tuning weights from the log compares
        decisions from two different bots.
        """
        return {
            agent.name: agent.version
            for agent in (self.auditor, self.narrative, self.timing, self.checker)
        }

    def _sync_counters(self) -> None:
        """Copy Grok spend into the state that will land on disk."""
        self.risk.grok_calls_today = self.grok_ops.budget.spent

    def status(self) -> dict[str, Any]:
        """Snapshot for /healthz and the heartbeat. Contains nothing secret."""
        stalled = (time.time() - self._last_event_at) > STALL_SECONDS
        breaker = self.grok_ops.breaker.state
        blind = bool(self.watcher.blind)
        state = "degraded" if breaker == "open" or stalled or blind else "ok"
        return {
            "status": state,
            "mode": self.config.mode,
            "uptime_seconds": round(self.metrics.uptime_seconds, 1),
            "stalled": stalled,
            "seconds_since_event": round(time.time() - self._last_event_at, 1),
            "in_flight": len(self._tasks),
            "pending_launches": len(self.monitor.pending),
            "open_positions": self.risk.open_count,
            "exposure_sol": round(self.risk.exposure_sol, 6),
            "blind_positions": len(self.watcher.blind),
            "trades_today": self.risk.trades_today,
            "realized_pnl_sol": round(self.risk.realized_pnl_sol, 6),
            "halted": self.risk.halted,
            "cooldown_left_seconds": round(self.risk.cooldown_left_seconds, 1),
            "losing_streak": self.risk.losing_streak,
            "breaker": breaker,
            "grok_budget_remaining": self.grok_ops.budget.remaining,
            "grok_tokens_in": self.grok_ops.tokens_in,
            "grok_tokens_out": self.grok_ops.tokens_out,
            "blocked_creators": sum(
                1 for r in self.reputation.creators.values() if r.is_known_bad
            ),
            "alerts": self.notifier.snapshot(),
        }

    def _market_snapshot(self) -> dict[str, Any]:
        """Observations going to the timing agent. Measured only."""
        data = self.pulse.snapshot()
        data.update({
            "pending_launches": len(self.monitor.pending),
            "open_positions": self.risk.open_count,
            "trades_today": self.risk.trades_today,
            "pnl_today_sol": round(self.risk.realized_pnl_sol, 4),
            "sparse_data": self.pulse.is_thin(),
        })
        return data

    def _log_monitor_skip(self, token: Token, reason: str) -> None:
        self.metrics.inc("skip_monitor")
        self.pulse.record_launch(token.sol_in_curve)
        self.trade_log.skip(token, stage="monitor", reason=reason)

    async def _price(self, mint: str) -> Tick:
        """Position price plus a flag that the token left the curve."""
        state = await self.executor.curve(mint)
        if state is None:
            return Tick()
        return Tick(price=state.spot_price, graduated=state.complete)

    async def _sell(
        self,
        position: Position,
        price: float,
        reason: str = "stop_loss",
        fraction: float = 1.0,
    ) -> None:
        """Exit a position in full or in part: sell, compute, record.

        Cost basis is split in proportion to tokens sold, so a partial
        take does not distort the remaining tail's result.
        """
        try:
            result = await self.executor.sell(position, fraction)
        except NotImplementedError as exc:
            log.error("sell not implemented, position %s stays open: %s",
                      position.mint[:8], exc)
            self.metrics.inc("sell_not_implemented")
            return

        tokens_before = position.token_amount or 1.0
        sold = result.token_amount if result.ok else tokens_before * fraction
        share = max(0.0, min(1.0, sold / tokens_before))
        final = share >= 0.999

        proceeds = result.sol_amount if result.ok else sold * price
        cost_basis = position.sol_spent * share
        pnl = proceeds - cost_basis
        exit_price = result.price or price
        change_pct = (
            (exit_price - position.entry_price) / position.entry_price * 100.0
            if position.entry_price else 0.0
        )

        self._sync_counters()
        if final:
            total_pnl = pnl + position.realized_sol - (position.sol_spent - cost_basis)
            self.risk.register_close(position.mint, pnl_sol=pnl)
            self.reputation.record_close(
                position.creator, pnl_sol=total_pnl, pnl_pct=change_pct,
                rug_loss_pct=self.config.filter.rug_loss_pct,
            )
            self._save_reputation()
            self.pulse.record_outcome(change_pct, self.config.filter.rug_loss_pct)
        else:
            position.token_amount -= sold
            position.sol_spent -= cost_basis
            position.realized_sol += proceeds
            position.partials += 1
            self.risk.register_partial(position.mint, pnl_sol=pnl)
            log.info("partially closed %s: %.0f%% of position, %.4f SOL cost basis left",
                     position.mint[:8], share * 100, position.sol_spent)

        self.trade_log.close(position, exit_price=exit_price, pnl_sol=pnl, reason=reason,
                             tx_hash=result.tx_hash, fraction=share, final=final)
        self.metrics.inc("closes" if final else "partial_closes")
        self.metrics.inc(f"exit_{reason}")
        self.metrics.gauge("open_positions", self.risk.open_count)
        log.info("CLOSED %s by rule %s, PnL %+.4f SOL", position.mint[:8], reason, pnl)
        self.notifier.notify(
            "close",
            f"closed {position.symbol or position.mint[:8]} by rule {reason}: "
            f"{pnl:+.4f} SOL ({change_pct:+.1f}%)",
            mint=position.mint, reason=reason, pnl_sol=round(pnl, 6),
            pnl_pct=round(change_pct, 2),
        )
        if final and -change_pct >= self.config.filter.rug_loss_pct:
            self.notifier.notify(
                "rug",
                f"creator {(position.creator or '?')[:8]} rugged "
                f"{position.symbol or position.mint[:8]} ({change_pct:+.1f}%) — "
                "their next tokens are skipped at entry",
                creator=position.creator, mint=position.mint,
                pnl_pct=round(change_pct, 2),
            )
        self._check_transitions()


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


LIVE_WARNING = """
================================================================
  LIVE MODE

  The pipeline will send REAL transactions with the real wallet
  from config.yaml. Memecoins on a bonding curve lose their value
  completely and routinely. The per-trade ceiling is {max_sol} SOL,
  the daily loss limit is {daily} SOL — these are guards, not a
  guarantee.

  A live start requires the --i-understand-the-risk flag.
================================================================
"""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="grokbot-pumpfun")
    parser.add_argument("--config", default="config.yaml", help="path to config")
    parser.add_argument("--check", action="store_true",
                        help="check the config and exit, start nothing")
    parser.add_argument("--i-understand-the-risk", action="store_true",
                        help="required to start in live mode")
    return parser.parse_args(argv)


def load_and_check(args: argparse.Namespace) -> Config:
    """Read the config, check fitness, explain a refusal in plain language."""
    path = Path(args.config)
    if not path.exists():
        raise SystemExit(f"No config at {path}. Copy config.example.yaml to config.yaml.")

    try:
        config = Config.load(path)
    except Exception as exc:
        raise SystemExit(f"Config {path} is unreadable: {exc}") from exc

    try:
        warnings = config.check_ready()
    except ConfigError as exc:
        raise SystemExit(str(exc)) from exc

    for warning in warnings:
        print(f"WARNING: {warning}", file=sys.stderr)

    if config.is_live:
        print(LIVE_WARNING.format(
            max_sol=config.risk.max_sol_per_trade,
            daily=config.risk.daily_loss_limit_sol,
        ), file=sys.stderr)
        if not getattr(args, "i_understand_the_risk", False):
            raise SystemExit(
                "Refused: mode: live without --i-understand-the-risk. "
                "Either set mode: dry-run, or confirm with the flag."
            )
    return config


async def amain(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_and_check(args)
    setup_logging(config)

    if args.check:
        print(json.dumps(config.redacted(), ensure_ascii=False, indent=2))
        print("\nConfig is ready to start.", file=sys.stderr)
        return 0

    async with Pipeline(config) as pipeline:
        return await pipeline.serve()


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(amain(argv))
    except KeyboardInterrupt:            # if the signal arrived before handlers were installed
        print("\nstopped", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

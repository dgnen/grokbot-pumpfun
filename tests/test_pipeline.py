"""End-to-end pipeline run in dry-run on mocked transport.

Checks the wiring: stages run in order, a refusal at any stage is
written to the log with the stage name, and dry-run sends no
transaction.
"""

import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest

from src.log import read_log
from src.models import Config, Token
from src.pipeline import Pipeline, load_and_check, main, parse_args
from src.state import StateStore


def grok_handler(responses: dict[str, str]):
    """Return different JSON depending on the agent's system prompt."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        system = body["messages"][0]["content"]
        for marker, content in responses.items():
            if marker in system:
                return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})
        raise AssertionError(f"unexpected prompt: {system[:60]}")

    return handler


GOOD_AUDIT = json.dumps({
    "coordinated_buying": False, "wash_trading": False, "creator_dump_prep": False,
    "bundled_launch": False, "organic_buyer_share": 0.95, "confidence": 0.9,
    "flags": [], "reasoning": "clean",
})
GOOD_NARRATIVE = json.dumps({
    "trend_fit": 0.9, "virality": 0.9, "community_signals": 0.9,
    "launch_timing": 0.9, "reasoning": "living meme",
})
GOOD_TIMING = json.dumps({
    "market_sentiment": 0.9, "meme_season": 0.9, "volume_level": 0.9,
    "anomalies": [], "reasoning": "backdrop is good",
})
APPROVE = json.dumps({"approve": True, "reason": "ok", "flags": [], "confidence": 0.9})
REJECT = json.dumps({"approve": False, "reason": "organic share does not match",
                     "flags": ["contradiction"], "confidence": 0.9})


# Mutable curve reserves in the mock: tests crash the price through them.
# 45 virtual SOL = 15 real, k is preserved: the curve is live and tradeable.
LIVE_CURVE = (45_000_000_000, 715_333_460_666_667)
CURVE = {"sol": LIVE_CURVE[0], "tokens": LIVE_CURVE[1]}


@pytest.fixture(autouse=True)
def _reset_curve():
    CURVE["sol"], CURVE["tokens"] = LIVE_CURVE
    yield


def move_price(factor: float) -> None:
    """Move the price by `factor`: below one is a dump, above one is a rally."""
    CURVE["sol"] = int(CURVE["sol"] * factor)


def data_handler(request: httpx.Request) -> httpx.Response:
    """Data provider: holders, trades, token card."""
    path = request.url.path
    if path.endswith("/holders"):
        return httpx.Response(200, json=[
            {"address": f"h{i}", "share": 0.02, "amount": 1000} for i in range(20)
        ])
    if "/trades/all/" in path:
        base = time.time() - 600
        return httpx.Response(200, json=[
            {"user": f"w{i}", "txType": "buy", "solAmount": 0.3 + i * 0.02,
             "timestamp": base + i * 20, "signature": f"s{i}"}
            for i in range(30)
        ])
    return httpx.Response(200, json={
        "description": "the internet's cutest cat",
        "twitter": "https://x.com/cat", "telegram": "https://t.me/cat",
        "website": "https://cat.fun",
        "virtual_sol_reserves": CURVE["sol"],
        "virtual_token_reserves": CURVE["tokens"],
    })


@pytest.fixture
def config(tmp_path) -> Config:
    cfg = Config()
    cfg.mode = "dry-run"
    cfg.grok.api_key = "xai-test-key-1234567890"
    cfg.grok.retry_base_delay = 0.0
    cfg.logging.path = str(tmp_path / "trades.jsonl")
    cfg.ops.state_path = str(tmp_path / "state.json")
    cfg.ops.reputation_path = str(tmp_path / "creators.json")
    cfg.filter.min_total_score = 0.65
    return cfg


LIVE_YAML = """
mode: live
grok:
  api_key: xai-real-key-1234
solana:
  wallet_private_key: 5xRealKey
"""

DRY_YAML = """
mode: dry-run
grok:
  api_key: xai-real-key-1234
"""


def wire(pipeline: Pipeline, checker_answer: str) -> None:
    """Replace all network transport with mocks."""
    grok = httpx.AsyncClient(transport=httpx.MockTransport(grok_handler({
        "forensic analyst": GOOD_AUDIT,
        "meme culture": GOOD_NARRATIVE,
        "market regime": GOOD_TIMING,
        "risk officer": checker_answer,
    })))
    for agent in (pipeline.auditor, pipeline.narrative, pipeline.timing, pipeline.checker):
        agent._client = grok
    data = httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(data_handler))
    pipeline.analyzer._client = data
    pipeline.executor._client = data


def fresh_token() -> Token:
    return Token(
        mint="Mint1111", name="Cat", symbol="CAT", image_uri="https://i",
        creator="Creator1", created_timestamp=time.time() - 600,
        unique_buyers=12, curve_progress=0.2, market_cap_sol=30.0,
    )


async def test_dry_run_buys_and_logs_full_context(config):
    pipeline = Pipeline(config)
    wire(pipeline, APPROVE)
    analysis = await pipeline.process(fresh_token())

    assert analysis is not None
    assert analysis.checker.approve
    assert pipeline.risk.open_count == 1

    records = list(read_log(config.logging.path))
    buys = [r for r in records if r["type"] == "buy"]
    assert len(buys) == 1
    buy = buys[0]
    assert buy["tx_hash"] == "dry_run"          # no real transaction
    assert buy["mode"] == "dry-run"
    assert buy["scores"]["total"] >= config.filter.min_total_score
    assert buy["audit"]["organic_buyer_share"] == 0.95
    assert buy["narrative"] and buy["timing"] and buy["checker"]
    assert buy["metrics"]["trade_count"] == 30
    assert buy["entry_price"] > 0


async def test_checker_veto_stops_the_buy(config):
    pipeline = Pipeline(config)
    wire(pipeline, REJECT)
    assert await pipeline.process(fresh_token()) is None
    assert pipeline.risk.open_count == 0

    records = list(read_log(config.logging.path))
    assert [r["type"] for r in records] == ["skip"]
    assert records[0]["stage"] == "checker"
    assert "contradiction" in records[0]["detail"]


async def test_risk_gate_stops_the_buy(config):
    config.risk.max_open_positions = 0
    pipeline = Pipeline(config)
    wire(pipeline, APPROVE)
    assert await pipeline.process(fresh_token()) is None

    records = list(read_log(config.logging.path))
    assert records[-1]["stage"] == "risk"
    assert records[-1]["reason"].startswith("max_open_positions")


async def test_high_threshold_stops_before_checker(config):
    """The scoring threshold skips the strong model: the checker must not be called."""
    config.filter.min_total_score = 0.99
    pipeline = Pipeline(config)
    wire(pipeline, APPROVE)
    pipeline.checker._client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: (_ for _ in ()).throw(AssertionError("checker called for nothing"))
        )
    )
    assert await pipeline.process(fresh_token()) is None
    records = list(read_log(config.logging.path))
    assert records[-1]["stage"] == "scoring"
    assert "weakest" in records[-1]["detail"]


async def test_stop_loss_closes_position_and_logs_pnl(config):
    pipeline = Pipeline(config)
    wire(pipeline, APPROVE)
    await pipeline.process(fresh_token())
    position = pipeline.risk.positions["Mint1111"]

    await pipeline._sell(position, price=position.entry_price * 0.5)

    assert pipeline.risk.open_count == 0
    closes = [r for r in read_log(config.logging.path) if r["type"] == "close"]
    assert len(closes) == 1
    assert closes[0]["reason"] == "stop_loss"
    assert closes[0]["tx_hash"] == "dry_run"


# --- restart and shutdown -------------------------------------------------


async def test_restart_picks_up_open_position(config):
    """A restarted process must not buy the same token again."""
    config.filter.one_position_per_creator = False   # this test is specifically about the risk gate
    first = Pipeline(config)
    wire(first, APPROVE)
    await first.process(fresh_token())
    assert first.risk.open_count == 1

    second = Pipeline(config)
    wire(second, APPROVE)
    second.restore()
    assert second.risk.open_count == 1
    assert await second.process(fresh_token()) is None

    records = list(read_log(config.logging.path))
    assert records[-1]["stage"] == "risk"
    assert records[-1]["reason"] == "already_open"


async def test_restart_continues_grok_budget(config):
    """Otherwise a restart loop would burn the daily call budget in an hour."""
    first = Pipeline(config)
    wire(first, APPROVE)
    await first.process(fresh_token())
    spent = first.grok_ops.budget.spent
    assert spent >= 4                      # auditor, narrative, timing, checker
    await first.shutdown()

    second = Pipeline(config)
    second.restore()
    assert second.grok_ops.budget.spent == spent


async def test_shutdown_persists_state(config):
    pipeline = Pipeline(config)
    wire(pipeline, APPROVE)
    await pipeline.process(fresh_token())
    await pipeline.shutdown()

    saved = StateStore(config.ops.state_path).load()
    assert saved is not None
    assert "Mint1111" in saved.positions
    assert saved.trades_today == 1


async def test_stop_request_is_idempotent(config):
    pipeline = Pipeline(config)
    pipeline.request_stop("SIGTERM")
    pipeline.request_stop("SIGTERM")
    assert pipeline._stopping.is_set()


async def test_shutdown_finishes_work_in_flight(config):
    pipeline = Pipeline(config)
    wire(pipeline, APPROVE)
    task = asyncio.create_task(pipeline.process(fresh_token()))
    pipeline._tasks.add(task)
    await pipeline.shutdown()
    assert task.done()
    assert pipeline.risk.open_count == 1


# --- observability --------------------------------------------------------


async def test_status_is_ok_and_free_of_secrets(config):
    pipeline = Pipeline(config)
    wire(pipeline, APPROVE)
    await pipeline.process(fresh_token())
    status = pipeline.status()
    assert status["status"] == "ok"
    assert status["open_positions"] == 1
    assert status["trades_today"] == 1
    assert config.grok.key not in json.dumps(status, ensure_ascii=False)


async def test_status_degrades_when_breaker_opens(config):
    config.ops.breaker_failures = 1
    pipeline = Pipeline(config)
    pipeline.grok_ops.breaker.record_failure()
    assert pipeline.status()["status"] == "degraded"


async def test_status_degrades_when_stream_stalls(config):
    pipeline = Pipeline(config)
    pipeline._last_event_at -= 10_000
    status = pipeline.status()
    assert status["stalled"]
    assert status["status"] == "degraded"


async def test_metrics_count_stages(config):
    pipeline = Pipeline(config)
    wire(pipeline, REJECT)
    await pipeline.process(fresh_token())
    assert pipeline.metrics.counters["skip_checker"] == 1
    assert pipeline.metrics.counters["grok_ok_checker"] == 1


# --- live-mode guard ------------------------------------------------------


def test_live_without_flag_refuses(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(LIVE_YAML)
    with pytest.raises(SystemExit) as exc:
        load_and_check(parse_args(["--config", str(cfg)]))
    assert "--i-understand-the-risk" in str(exc.value)


def test_live_with_flag_allowed(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(LIVE_YAML)
    config = load_and_check(parse_args(["--config", str(cfg), "--i-understand-the-risk"]))
    assert config.is_live


def test_missing_config_refuses(tmp_path):
    with pytest.raises(SystemExit):
        load_and_check(parse_args(["--config", str(tmp_path / "missing.yaml")]))


def test_broken_yaml_refuses(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("mode: [unclosed\n")
    with pytest.raises(SystemExit) as exc:
        load_and_check(parse_args(["--config", str(cfg)]))
    assert "unreadable" in str(exc.value)


def test_invalid_config_refuses_before_start(tmp_path):
    """A bad config must fail at start, not an hour into trading."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(DRY_YAML + "risk:\n  max_sol_per_trade: 0\n")
    with pytest.raises(SystemExit) as exc:
        load_and_check(parse_args(["--config", str(cfg)]))
    assert "max_sol_per_trade" in str(exc.value)


def test_dry_run_needs_no_flag(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(DRY_YAML)
    assert not load_and_check(parse_args(["--config", str(cfg)])).is_live


def test_check_flag_exits_without_running(tmp_path, capsys):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(DRY_YAML)
    assert main(["--config", str(cfg), "--check"]) == 0
    printed = capsys.readouterr().out
    assert "xai-real-key-1234" not in printed
    assert "dry-run" in printed


async def test_live_executor_stub_does_not_crash_the_pipeline(config):
    """The live stub raises NotImplementedError — a stage refusal with
    a loud log line, not a process crash and not a silent buy."""
    config.mode = "live"
    pipeline = Pipeline(config)
    wire(pipeline, APPROVE)
    assert pipeline.executor.__class__.__name__ == "LiveExecutor"

    assert await pipeline.process(fresh_token()) is None
    assert pipeline.risk.open_count == 0
    records = list(read_log(config.logging.path))
    assert records[-1]["stage"] == "executor"
    assert records[-1]["reason"] == "executor_not_implemented"


# --- full lifecycle -------------------------------------------------------


async def test_serve_runs_then_stops_cleanly(config):
    """Start, process a token, expose health, SIGTERM, persist state."""
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    config.ops.health_port = port
    config.ops.heartbeat_seconds = 3600      # not needed in the test
    config.ops.shutdown_grace_seconds = 5

    pipeline = Pipeline(config)
    wire(pipeline, APPROVE)

    processed = asyncio.Event()

    async def fake_stream():
        yield fresh_token()
        processed.set()
        await asyncio.sleep(3600)            # the stream then just sits there

    pipeline.monitor.stream = fake_stream    # type: ignore[method-assign]

    async with pipeline:
        serving = asyncio.create_task(pipeline.serve())
        await asyncio.wait_for(processed.wait(), timeout=5)
        for _ in range(50):                  # wait until the token reaches a buy
            if pipeline.risk.open_count:
                break
            await asyncio.sleep(0.02)

        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n")
        await writer.drain()
        head, _, body = (await reader.read()).decode().partition("\r\n\r\n")
        writer.close()
        assert "200" in head.split("\r\n")[0]
        assert json.loads(body)["open_positions"] == 1

        pipeline.request_stop("SIGTERM")
        assert await asyncio.wait_for(serving, timeout=10) == 0

    saved = StateStore(config.ops.state_path).load()
    assert saved is not None and "Mint1111" in saved.positions
    assert [r["type"] for r in read_log(config.logging.path)] == ["intent", "buy"]


# --- creator memory -------------------------------------------------------


async def test_creator_who_rugged_is_blocked_next_time(config):
    """A rug is written to the book, and the next token from that address
    does not reach a single Grok call."""
    pipeline = Pipeline(config)
    wire(pipeline, APPROVE)
    await pipeline.process(fresh_token())
    position = pipeline.risk.positions["Mint1111"]

    move_price(0.1)                      # the token collapsed tenfold
    await pipeline._sell(position, price=await pipeline._price(position.mint),
                         reason="stop_loss")
    assert pipeline.reputation.creators["Creator1"].rugs == 1

    calls_before = pipeline.grok_ops.budget.spent
    other = fresh_token()
    other.mint = "Mint2222"
    assert await pipeline.process(other) is None
    assert pipeline.grok_ops.budget.spent == calls_before      # agents were not called

    records = list(read_log(config.logging.path))
    assert records[-1]["stage"] == "reputation"
    assert "rugged" in records[-1]["detail"]


async def test_blocklist_survives_restart(config):
    first = Pipeline(config)
    wire(first, APPROVE)
    await first.process(fresh_token())
    position = first.risk.positions["Mint1111"]
    move_price(0.05)
    await first._sell(position, price=await first._price(position.mint), reason="stop_loss")
    await first.shutdown()

    second = Pipeline(config)
    wire(second, APPROVE)
    second.restore()
    new_token = fresh_token()
    new_token.mint = "Mint3333"
    assert await second.process(new_token) is None
    assert list(read_log(config.logging.path))[-1]["stage"] == "reputation"


async def test_second_token_from_same_creator_is_one_bet(config):
    """Two tokens from one deployer rug together — that is one bet."""
    pipeline = Pipeline(config)
    wire(pipeline, APPROVE)
    await pipeline.process(fresh_token())

    second = fresh_token()
    second.mint = "Mint4444"
    assert await pipeline.process(second) is None
    records = list(read_log(config.logging.path))
    assert records[-1]["stage"] == "reputation"
    assert "already has an open position" in records[-1]["detail"]


async def test_moderate_loss_does_not_blacklist(config):
    config.filter.rug_loss_pct = 60.0
    pipeline = Pipeline(config)
    wire(pipeline, APPROVE)
    await pipeline.process(fresh_token())
    position = pipeline.risk.positions["Mint1111"]

    move_price(0.75)                     # minus 25%: ugly, but not a rug
    await pipeline._sell(position, price=await pipeline._price(position.mint),
                         reason="stop_loss")
    assert pipeline.reputation.creators["Creator1"].rugs == 0
    assert pipeline._creator_verdict(fresh_token()) is None


async def test_reputation_can_be_switched_off(config):
    config.filter.block_creator_after_rugs = 0
    config.filter.one_position_per_creator = False
    pipeline = Pipeline(config)
    wire(pipeline, APPROVE)
    await pipeline.process(fresh_token())
    position = pipeline.risk.positions["Mint1111"]
    move_price(0.05)
    await pipeline._sell(position, price=await pipeline._price(position.mint),
                         reason="stop_loss")

    CURVE["sol"] = LIVE_CURVE[0]           # the other token has its own curve
    other = fresh_token()
    other.mint = "Mint5555"
    assert await pipeline.process(other) is not None      # bought, despite the rug


# --- alerts ---------------------------------------------------------------


def wire_alerts(pipeline: Pipeline) -> list[dict]:
    """Enable alerts and collect them into a list instead of the network."""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(204)

    pipeline.config.alerts.webhook_url = "https://hooks.example/test"
    pipeline.notifier.config = pipeline.config.alerts
    pipeline.notifier._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    pipeline.notifier._owns_client = False   # the mock survives aclose between checks
    return seen


async def flush_alerts(pipeline: Pipeline) -> None:
    """Wait for the send: notify parks the task in the background and returns."""
    await pipeline.notifier.aclose()


async def test_buy_is_announced(config):
    pipeline = Pipeline(config)
    wire(pipeline, APPROVE)
    seen = wire_alerts(pipeline)
    await pipeline.process(fresh_token())
    await pipeline.notifier.aclose()

    buys = [event for event in seen if event["event"] == "buy"]
    assert len(buys) == 1
    assert "CAT" in buys[0]["text"]
    assert buys[0]["fields"]["mint"] == "Mint1111"


async def test_rug_is_announced_separately_from_close(config):
    pipeline = Pipeline(config)
    wire(pipeline, APPROVE)
    seen = wire_alerts(pipeline)
    await pipeline.process(fresh_token())
    position = pipeline.risk.positions["Mint1111"]

    move_price(0.05)
    await pipeline._sell(position, price=await pipeline._price(position.mint),
                         reason="stop_loss")
    await pipeline.notifier.aclose()

    kinds = [event["event"] for event in seen]
    assert kinds.count("close") == 1
    assert kinds.count("rug") == 1
    assert "skipped at entry" in next(e for e in seen if e["event"] == "rug")["text"]


async def test_ordinary_loss_is_not_announced_as_rug(config):
    pipeline = Pipeline(config)
    wire(pipeline, APPROVE)
    seen = wire_alerts(pipeline)
    await pipeline.process(fresh_token())
    position = pipeline.risk.positions["Mint1111"]

    move_price(0.8)
    await pipeline._sell(position, price=await pipeline._price(position.mint),
                         reason="stop_loss")
    await pipeline.notifier.aclose()
    assert "rug" not in [event["event"] for event in seen]


async def test_breaker_announced_once_per_transition(config):
    config.ops.breaker_failures = 1
    pipeline = Pipeline(config)
    seen = wire_alerts(pipeline)

    pipeline.grok_ops.breaker.record_failure()
    pipeline._check_transitions()
    pipeline._check_transitions()                  # second time we stay silent
    await flush_alerts(pipeline)
    breaker_events = [e for e in seen if e["event"] == "breaker"]
    assert len(breaker_events) == 1
    assert "Grok circuit opened" in breaker_events[0]["text"]

    pipeline.grok_ops.breaker.record_success()
    pipeline._check_transitions()
    await flush_alerts(pipeline)
    assert [e["text"] for e in seen if e["event"] == "breaker"][-1].endswith("work continues")


async def test_halt_is_announced(config):
    pipeline = Pipeline(config)
    seen = wire_alerts(pipeline)
    pipeline.risk.register_close("X", pnl_sol=-config.risk.daily_loss_limit_sol)
    pipeline._check_transitions()
    await pipeline.notifier.aclose()
    assert any(e["event"] == "halted" for e in seen)


async def test_alerts_off_by_default(config):
    pipeline = Pipeline(config)
    wire(pipeline, APPROVE)
    assert not pipeline.notifier.enabled
    await pipeline.process(fresh_token())          # nothing is sent and nothing crashes
    assert pipeline.notifier.snapshot() == {"sent": 0, "dropped": 0, "failed": 0}


# --- buy without a price --------------------------------------------------


async def test_token_without_curve_data_is_refused(config):
    """A position with an unknown entry price is unmanageable: no exit
    rule can fire on it, and it would sit open forever."""
    pipeline = Pipeline(config)
    wire(pipeline, APPROVE)
    CURVE["sol"] = 0                                # the provider returned no reserves
    no_price = fresh_token()
    no_price.market_cap_sol = 0.0                   # and there is no fallback estimate either

    assert await pipeline.process(no_price) is None
    assert pipeline.risk.open_count == 0
    records = list(read_log(config.logging.path))
    assert records[-1]["stage"] == "analyzer"
    assert records[-1]["reason"] == "curve_too_thin"


async def test_thin_curve_is_refused(config):
    """You cannot exit a curve with a couple of SOL: your own sell dumps the price."""
    config.market.min_curve_liquidity_sol = 5.0
    pipeline = Pipeline(config)
    wire(pipeline, APPROVE)
    CURVE["sol"] = 32_000_000_000                   # only 2 real SOL

    assert await pipeline.process(fresh_token()) is None
    records = list(read_log(config.logging.path))
    assert records[-1]["reason"] == "curve_too_thin"
    assert pipeline.grok_ops.budget.spent == 0      # it never reached the agents


async def test_blind_position_degrades_health(config):
    pipeline = Pipeline(config)
    wire(pipeline, APPROVE)
    await pipeline.process(fresh_token())

    pipeline.watcher.price_failures["Mint1111"] = pipeline.watcher.BLIND_AFTER
    status = pipeline.status()
    assert status["blind_positions"] == 1
    assert status["status"] == "degraded"


async def test_blind_position_is_announced(config):
    pipeline = Pipeline(config)
    wire(pipeline, APPROVE)
    seen = wire_alerts(pipeline)
    await pipeline.process(fresh_token())

    pipeline.watcher.price_failures["Mint1111"] = pipeline.watcher.BLIND_AFTER
    pipeline._check_transitions()
    await flush_alerts(pipeline)
    blind = [e for e in seen if e["event"] == "blind"]
    assert len(blind) == 1
    assert "are not working" in blind[0]["text"]


# --- partial take-profit --------------------------------------------------


async def test_partial_take_profit_keeps_the_tail(config):
    """Take the bulk and leave a tail for the trailing stop — that is the
    point of a partial exit. The position must stay open with a reduced cost basis."""
    config.risk.take_profit_pct = 50.0
    config.risk.take_profit_fraction = 0.6
    pipeline = Pipeline(config)
    wire(pipeline, APPROVE)
    await pipeline.process(fresh_token())
    position = pipeline.risk.positions["Mint1111"]
    spent_before, tokens_before = position.sol_spent, position.token_amount

    move_price(2.0)
    assert await pipeline.watcher.check_once() == []      # not closed in full

    assert "Mint1111" in pipeline.risk.positions
    assert position.partials == 1
    assert position.token_amount == pytest.approx(tokens_before * 0.4)
    assert position.sol_spent == pytest.approx(spent_before * 0.4, rel=0.01)  # leftover
    assert position.realized_sol > 0
    assert pipeline.risk.realized_pnl_sol > 0

    closes = [r for r in read_log(config.logging.path) if r["type"] == "close"]
    assert len(closes) == 1
    assert closes[0]["final"] is False
    assert closes[0]["fraction"] == pytest.approx(0.6)
    assert closes[0]["reason"] == "take_profit"


async def test_tail_closes_and_reputation_counts_once(config):
    config.risk.take_profit_pct = 50.0
    config.risk.take_profit_fraction = 0.6
    config.risk.trailing_stop_pct = 30.0
    pipeline = Pipeline(config)
    wire(pipeline, APPROVE)
    await pipeline.process(fresh_token())

    move_price(2.0)
    await pipeline.watcher.check_once()                   # partial take-profit
    move_price(0.5)                                       # pullback from the peak
    closed = await pipeline.watcher.check_once()

    assert closed == ["Mint1111"]
    assert pipeline.risk.open_count == 0
    assert pipeline.reputation.creators["Creator1"].closed == 1   # once, not twice

    closes = [r for r in read_log(config.logging.path) if r["type"] == "close"]
    assert [r["final"] for r in closes] == [False, True]


async def test_partial_profit_survives_restart(config):
    config.risk.take_profit_pct = 50.0
    config.risk.take_profit_fraction = 0.5
    first = Pipeline(config)
    wire(first, APPROVE)
    await first.process(fresh_token())
    move_price(2.0)
    await first.watcher.check_once()
    await first.shutdown()

    second = Pipeline(config)
    second.restore()
    restored = second.risk.positions["Mint1111"]
    assert restored.partials == 1
    assert restored.realized_sol > 0
    assert second.risk.realized_pnl_sol == pytest.approx(first.risk.realized_pnl_sol)


async def test_position_size_capped_by_liquidity(config):
    """On a thin curve the order is cut by the impact cap, not by scoring."""
    config.risk.max_sol_per_trade = 5.0
    config.market.max_price_impact_pct = 3.0
    pipeline = Pipeline(config)
    wire(pipeline, APPROVE)
    await pipeline.process(fresh_token())

    position = pipeline.risk.positions["Mint1111"]
    assert position.sol_spent < 2.0            # 45 SOL of reserve will not let you take five
    buys = [r for r in read_log(config.logging.path) if r["type"] == "buy"]
    assert buys[0]["size_sol"] == pytest.approx(position.sol_spent)


async def test_entry_price_includes_slippage(config):
    """The entry price in the log is the average fill, not the quote."""
    pipeline = Pipeline(config)
    wire(pipeline, APPROVE)
    analysis = await pipeline.process(fresh_token())
    assert analysis is not None

    spot = analysis.curve.spot_price
    buy = next(r for r in read_log(config.logging.path) if r["type"] == "buy")
    assert buy["entry_price"] > spot
    assert buy["metrics"]["round_trip_cost_pct"] > 0
    assert buy["metrics"]["curve_liquidity_sol"] == pytest.approx(15.0)


# --- data for the timing agent --------------------------------------------


async def test_timing_agent_gets_measured_data(config):
    """The market agent must see observations, not internal counters."""
    captured: list[dict] = []

    def grok_handler_capturing(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        system = body["messages"][0]["content"]
        if "market regime" in system:
            captured.append(json.loads(body["messages"][1]["content"]))
            return httpx.Response(200, json={"choices": [{"message": {"content": GOOD_TIMING}}]})
        content = {"forensic analyst": GOOD_AUDIT, "meme culture": GOOD_NARRATIVE,
                   "risk officer": APPROVE}
        for marker, answer in content.items():
            if marker in system:
                return httpx.Response(200, json={"choices": [{"message": {"content": answer}}]})
        raise AssertionError("unknown agent")

    pipeline = Pipeline(config)
    grok = httpx.AsyncClient(transport=httpx.MockTransport(grok_handler_capturing))
    for agent in (pipeline.auditor, pipeline.narrative, pipeline.timing, pipeline.checker):
        agent._client = grok
    data = httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(data_handler))
    pipeline.analyzer._client = data
    pipeline.executor._client = data

    for index in range(10):
        pipeline.pulse.record_launch(35.0 + index)
    await pipeline.process(fresh_token())

    assert captured, "timing agent was not asked"
    observations = captured[0]["observations"]
    assert observations["launches_in_window"] >= 10
    assert observations["median_sol_in_curve"] > 30
    assert "utc_hour" in observations
    assert observations["sparse_data"] is False


async def test_pulse_counts_launches_and_outcomes(config):
    pipeline = Pipeline(config)
    wire(pipeline, APPROVE)

    pipeline._log_monitor_skip(fresh_token(), "few_buyers")     # a launch that was filtered out
    await pipeline.process(fresh_token())                        # survived through to review
    assert pipeline.pulse.snapshot()["launches_in_window"] == 1
    assert pipeline.pulse.snapshot()["buys_in_window"] == 1

    position = pipeline.risk.positions["Mint1111"]
    move_price(0.05)
    await pipeline._sell(position, price=await pipeline._price(position.mint),
                         reason="stop_loss")
    assert pipeline.pulse.snapshot()["rug_rate"] == pytest.approx(1.0)


async def test_outcomes_restored_after_restart(config):
    first = Pipeline(config)
    wire(first, APPROVE)
    await first.process(fresh_token())
    position = first.risk.positions["Mint1111"]
    move_price(0.05)
    await first._sell(position, price=await first._price(position.mint), reason="stop_loss")
    await first.shutdown()

    second = Pipeline(config)
    second.restore()
    assert second.pulse.snapshot()["closed_trades_in_memory"] == 1


# --- move to Raydium ------------------------------------------------------


def graduated_handler(request: httpx.Request) -> httpx.Response:
    """The provider reports that the curve is finished."""
    if request.url.path.endswith("/holders") or "/trades/all/" in request.url.path:
        return data_handler(request)
    payload = json.loads(data_handler(request).content)
    payload["complete"] = True
    return httpx.Response(200, json=payload)


async def test_graduated_token_is_exited_not_left_blind(config):
    """The token moved to Raydium: the curve is gone, and rules that read
    it would go blind exactly when the position is most in profit."""
    pipeline = Pipeline(config)
    wire(pipeline, APPROVE)
    await pipeline.process(fresh_token())
    assert pipeline.risk.open_count == 1

    graduated = httpx.AsyncClient(base_url="http://test",
                                  transport=httpx.MockTransport(graduated_handler))
    pipeline.analyzer._client = graduated
    pipeline.executor._client = graduated

    assert await pipeline.watcher.check_once() == ["Mint1111"]
    assert pipeline.risk.open_count == 0

    closes = [r for r in read_log(config.logging.path) if r["type"] == "close"]
    assert closes[-1]["reason"] == "graduated"


async def test_graduation_flag_reaches_the_position(config):
    pipeline = Pipeline(config)
    wire(pipeline, APPROVE)
    await pipeline.process(fresh_token())
    position = pipeline.risk.positions["Mint1111"]
    assert not position.graduated

    tick = await pipeline._price("Mint1111")
    assert not tick.graduated

    pipeline.executor._client = httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(graduated_handler))
    assert (await pipeline._price("Mint1111")).graduated


# --- leftover buy intent --------------------------------------------------


async def test_intent_is_written_before_execution(config):
    pipeline = Pipeline(config)
    wire(pipeline, APPROVE)
    await pipeline.process(fresh_token())

    kinds = [r["type"] for r in read_log(config.logging.path)]
    assert kinds.index("intent") < kinds.index("buy")


def test_orphan_intent_is_reported_on_restart(config, caplog):
    """The process died between execution and bookkeeping: an intent is
    left on disk with no buy. That must not be silent — the wallet may
    hold tokens the bot does not know about."""
    log_path = Path(config.logging.path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(json.dumps(
        {"type": "intent", "mint": "OrphanedMint", "size_sol": 0.4, "ts": time.time()}
    ) + "\n")

    pipeline = Pipeline(config)
    with caplog.at_level("ERROR"):
        pipeline.restore()
    assert "Orphaned" in caplog.text


async def test_completed_intent_is_not_reported(config, caplog):
    pipeline = Pipeline(config)
    wire(pipeline, APPROVE)
    await pipeline.process(fresh_token())        # intent and buy together

    second = Pipeline(config)
    with caplog.at_level("ERROR"):
        second.restore()
    assert "buy intent" not in caplog.text


def test_unmatched_intents_pairs_records():
    from src.pipeline import unmatched_intents

    records = [
        {"type": "intent", "mint": "A"},
        {"type": "buy", "mint": "A"},
        {"type": "intent", "mint": "B"},
        {"type": "skip", "mint": "C"},
        {"type": "intent", "mint": "D"},
        {"type": "close", "mint": "D"},
    ]
    assert unmatched_intents(records) == ["B"]


# --- prompt versions ------------------------------------------------------


async def test_buy_records_prompt_versions(config):
    """A prompt edit changes agent behaviour, but the records look the same.
    Without a version tag, weight fitting on the log compares two different bots."""
    pipeline = Pipeline(config)
    wire(pipeline, APPROVE)
    await pipeline.process(fresh_token())

    buy = next(r for r in read_log(config.logging.path) if r["type"] == "buy")
    versions = buy["prompt_versions"]
    assert set(versions) == {"auditor", "narrative", "timing", "checker"}
    assert all(value for value in versions.values())


def test_prompt_versions_are_distinct(config):
    versions = Pipeline(config).prompt_versions()
    assert len(set(versions.values())) == len(versions)


async def test_plan_is_computed_before_the_checker(config):
    """The risk gate is computed twice: before the checker so it sees the
    economics, and after because limits may have moved while it thought."""
    pipeline = Pipeline(config)
    wire(pipeline, APPROVE)
    analysis = await pipeline.process(fresh_token())

    assert analysis is not None
    assert analysis.plan is not None
    assert analysis.plan.approved
    assert analysis.plan.size_sol == pytest.approx(
        pipeline.risk.positions["Mint1111"].sol_spent
    )


# --- one bot per state file -----------------------------------------------


async def test_second_instance_refuses_to_start(config, monkeypatch):
    """Two processes on one state file are two bots on one wallet."""
    import os

    first = Pipeline(config)
    assert first.lock.acquire()

    monkeypatch.setattr(os, "getpid", lambda: os.getppid())
    second = Pipeline(config)
    async with second:
        assert await second.serve() == 2

    first.lock.release()


async def test_lock_released_after_shutdown(config):
    pipeline = Pipeline(config)
    pipeline.lock.acquire()
    await pipeline.shutdown()
    assert not pipeline.lock.path.exists()


async def test_cooldown_blocks_new_buys(config):
    config.risk.cooldown_after_losses = 1
    config.risk.cooldown_minutes = 30.0
    pipeline = Pipeline(config)
    wire(pipeline, APPROVE)
    await pipeline.process(fresh_token())

    position = pipeline.risk.positions["Mint1111"]
    move_price(0.5)
    await pipeline._sell(position, price=await pipeline._price(position.mint),
                         reason="stop_loss")
    assert pipeline.risk.cooling_down

    move_price(2.0)
    other = fresh_token()
    other.mint = "Mint7777"
    other.creator = "Creator7"
    assert await pipeline.process(other) is None

    records = list(read_log(config.logging.path))
    assert records[-1]["reason"].startswith("cooldown_after_losses")
    assert pipeline.status()["losing_streak"] == 1

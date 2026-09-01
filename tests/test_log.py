"""JSONL log: rotation and reading. The process lives for days, the file always grows."""

import json

import pytest

from src.log import TradeLog, read_log
from src.models import Config, Position, Token


@pytest.fixture
def trade_log(tmp_path) -> TradeLog:
    return TradeLog(tmp_path / "trades.jsonl", mode="dry-run", max_bytes=400, backups=3)


def token() -> Token:
    return Token(mint="M" * 12, symbol="CAT", name="Cat")


def position() -> Position:
    return Position(mint="M", symbol="CAT", entry_price=1e-7, sol_spent=0.4,
                    token_amount=100.0, opened_at=1.0, tx_hash="dry_run")


def test_records_carry_mode_and_timestamp(trade_log):
    record = trade_log.skip(token(), stage="monitor", reason="few_buyers")
    assert record["mode"] == "dry-run"
    assert record["ts"] > 0
    assert next(iter(read_log(trade_log.path)))["reason"] == "few_buyers"


def test_rotation_kicks_in_at_threshold(trade_log):
    for _ in range(20):
        trade_log.skip(token(), stage="monitor", reason="few_buyers")
    rotated = trade_log.path.with_suffix(".jsonl.1")
    assert rotated.exists()
    assert trade_log.path.stat().st_size < trade_log.max_bytes


def test_rotation_keeps_only_configured_backups(trade_log):
    for _ in range(200):
        trade_log.skip(token(), stage="monitor", reason="few_buyers")
    suffixes = sorted(p.suffix for p in trade_log.path.parent.iterdir())
    assert suffixes.count(".1") <= 1
    assert not trade_log.path.with_suffix(".jsonl.4").exists()


def test_read_all_spans_rotated_files(tmp_path):
    # threshold is set so rotation happens but no copy is evicted yet:
    # we are checking the join, not eviction
    log = TradeLog(tmp_path / "trades.jsonl", max_bytes=2000, backups=3)
    trade_log = log
    for index in range(30):
        trade_log.skip(token(), stage="monitor", reason=f"reason-{index}")
    assert trade_log.path.with_suffix(".jsonl.1").exists()

    reasons = [r["reason"] for r in trade_log.read_all()]
    assert len(reasons) == 30
    assert reasons[0] == "reason-0"           # older records come first
    assert reasons[-1] == "reason-29"


def test_no_rotation_when_disabled(tmp_path):
    log = TradeLog(tmp_path / "trades.jsonl", max_bytes=0)
    for _ in range(50):
        log.skip(token(), stage="monitor", reason="few_buyers")
    assert not log.path.with_suffix(".jsonl.1").exists()


def test_close_record_computes_pnl_percent(trade_log):
    record = trade_log.close(position(), exit_price=0.7e-7, pnl_sol=-0.12, reason="stop_loss")
    assert record["pnl_pct"] == pytest.approx(-30.0)
    assert record["hold_seconds"] > 0


def test_broken_line_is_skipped_not_fatal(tmp_path):
    path = tmp_path / "trades.jsonl"
    path.write_text(json.dumps({"type": "skip", "mint": "A"}) + "\n{broken line\n" +
                    json.dumps({"type": "skip", "mint": "B"}) + "\n")
    assert [r["mint"] for r in read_log(path)] == ["A", "B"]


def test_from_config_uses_logging_section(tmp_path):
    cfg = Config()
    cfg.logging.path = str(tmp_path / "x.jsonl")
    cfg.logging.max_bytes = 123
    cfg.logging.backups = 2
    log = TradeLog.from_config(cfg)
    assert (log.max_bytes, log.backups, log.mode) == (123, 2, "dry-run")


# --- a log failure must not stop trading ----------------------------------


def test_write_failure_is_counted_not_raised(tmp_path, caplog):
    """A full disk is bad, but not a reason to leave open positions
    unattended."""
    log = TradeLog(tmp_path / "not-a-dir" / "trades.jsonl")
    log.path = tmp_path / "missing" / "missing" / "trades.jsonl"     # the directory does not exist

    with caplog.at_level("ERROR"):
        record = log.skip(token(), stage="monitor", reason="few_buyers")

    assert record["type"] == "skip"          # the caller got its record back
    assert log.write_failures == 1
    assert "failed" in caplog.text


def test_repeated_failures_do_not_spam_the_log(tmp_path, caplog):
    log = TradeLog(tmp_path / "trades.jsonl")
    log.path = tmp_path / "missing" / "trades.jsonl"
    with caplog.at_level("ERROR"):
        for _ in range(5):
            log.skip(token(), stage="monitor", reason="few_buyers")
    assert log.write_failures == 5
    assert caplog.text.count("failed") == 1     # only the first one


def test_intent_record_precedes_purchase(tmp_path):
    from src.models import Analysis, Scores
    from src.models import Token as T

    log = TradeLog(tmp_path / "trades.jsonl")
    analysis = Analysis(token=T(mint="M" * 8, symbol="CAT"), scores=Scores(total=0.8))
    record = log.intent(analysis, size_sol=0.4)
    assert record["type"] == "intent"
    assert record["size_sol"] == 0.4
    assert record["score"] == 0.8

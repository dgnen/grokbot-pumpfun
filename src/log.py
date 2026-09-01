"""JSONL logging. One record — one line.

Three record types:
  buy   — a purchase, with the full decision context (scoring, every
          agent's estimates, metrics, entry price);
  skip  — the token did not pass, with stage, reason, and detail;
  close — position close with PnL and hold time.

The log is the only source of truth about what the pipeline did and why.
Scoring is written broken down by component: without that, later you
cannot tell which agent pulled decisions down.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .models import Analysis, Config, Position, Scores, Token

log = logging.getLogger(__name__)


def setup_logging(config: Config) -> None:
    """Human-readable log on stderr. JSONL is written separately, to a file."""
    logging.basicConfig(
        level=getattr(logging, config.logging.level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)-16s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )


class TradeLog:
    """JSONL appender. Each record is flushed immediately — the process may die."""

    def __init__(
        self,
        path: str | Path,
        mode: str = "dry-run",
        max_bytes: int = 0,
        backups: int = 5,
    ) -> None:
        self.path = Path(path)
        self.mode = mode
        self.max_bytes = max(0, max_bytes)
        self.backups = max(1, backups)
        self.write_failures = 0
        with contextlib.suppress(OSError):
            self.path.parent.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_config(cls, config: Any) -> TradeLog:
        return cls(
            config.logging.path,
            mode=config.mode,
            max_bytes=config.logging.max_bytes,
            backups=config.logging.backups,
        )

    # -- rotation ----------------------------------------------------------

    def rotate_if_needed(self) -> bool:
        """Cut the file when it outgrows the threshold. Replay reads .1 and .2.

        Without this, a month of continuous JSONL grows to a size that
        will not open, and the disk fills in silence.
        """
        if not self.max_bytes or not self.path.exists():
            return False
        if self.path.stat().st_size < self.max_bytes:
            return False

        oldest = self.path.with_suffix(self.path.suffix + f".{self.backups}")
        oldest.unlink(missing_ok=True)
        for index in range(self.backups - 1, 0, -1):
            source = self.path.with_suffix(self.path.suffix + f".{index}")
            if source.exists():
                os.replace(source, self.path.with_suffix(self.path.suffix + f".{index + 1}"))
        os.replace(self.path, self.path.with_suffix(self.path.suffix + ".1"))
        log.info("log %s reached %d bytes — rotated", self.path, self.max_bytes)
        return True

    # -- write -------------------------------------------------------------

    def _write(self, record: dict[str, Any]) -> dict[str, Any]:
        """Write a line. A write failure does not stop trading.

        A full disk is bad, but not a reason to leave open positions
        unwatched. Missed records are counted, and the counter shows
        that the log is incomplete.
        """
        record.setdefault("ts", time.time())
        record.setdefault("mode", self.mode)
        try:
            self.rotate_if_needed()
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except (OSError, TypeError, ValueError) as exc:
            self.write_failures += 1
            if self.write_failures in (1, 10, 100):
                log.error("write to %s failed (%d-th): %s",
                          self.path, self.write_failures, exc)
        return record

    def buy(
        self,
        analysis: Analysis,
        *,
        size_sol: float,
        entry_price: float,
        tx_hash: str,
        prompt_versions: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        token = analysis.token
        return self._write(
            {
                "type": "buy",
                "mint": token.mint,
                "symbol": token.symbol,
                "name": token.name,
                "size_sol": round(size_sol, 6),
                "entry_price": entry_price,
                "tx_hash": tx_hash,
                "scores": analysis.scores.model_dump(),
                "audit": analysis.audit.model_dump() if analysis.audit else None,
                "narrative": analysis.narrative.model_dump() if analysis.narrative else None,
                "timing": analysis.timing.model_dump() if analysis.timing else None,
                "checker": analysis.checker.model_dump() if analysis.checker else None,
                "metrics": analysis.metrics.model_dump(),
                "prompt_versions": prompt_versions or {},
                "token": {
                    "age_seconds": round(token.age_seconds),
                    "unique_buyers": token.unique_buyers,
                    "curve_progress": round(token.curve_progress, 4),
                    "market_cap_sol": round(token.market_cap_sol, 4),
                    "creator": token.creator,
                },
            }
        )

    def intent(self, analysis: Analysis, *, size_sol: float) -> dict[str, Any]:
        """Record of a buy intent — before the transaction is sent.

        If the process dies between execution and booking the position,
        a trace stays on disk: an intent with no buy. On start that is
        visible, and the wallet can be checked by hand, instead of
        discovering extra tokens a week later.
        """
        return self._write({
            "type": "intent",
            "mint": analysis.token.mint,
            "symbol": analysis.token.symbol,
            "size_sol": round(size_sol, 6),
            "score": analysis.scores.total,
        })

    def skip(
        self,
        token: Token,
        *,
        stage: str,
        reason: str,
        detail: str | None = None,
        scores: Scores | None = None,
    ) -> dict[str, Any]:
        return self._write(
            {
                "type": "skip",
                "mint": token.mint,
                "symbol": token.symbol,
                "stage": stage,
                "reason": reason,
                "detail": detail,
                "scores": scores.model_dump() if scores else None,
            }
        )

    def close(
        self,
        position: Position,
        *,
        exit_price: float,
        pnl_sol: float,
        reason: str,
        tx_hash: str = "",
        fraction: float = 1.0,
        final: bool = True,
    ) -> dict[str, Any]:
        held = max(0.0, time.time() - position.opened_at) if position.opened_at else 0.0
        pnl_pct = (
            (exit_price - position.entry_price) / position.entry_price * 100.0
            if position.entry_price
            else 0.0
        )
        return self._write(
            {
                "type": "close",
                "mint": position.mint,
                "symbol": position.symbol,
                "creator": position.creator,
                "entry_price": position.entry_price,
                "exit_price": exit_price,
                "pnl_sol": round(pnl_sol, 6),
                "pnl_pct": round(pnl_pct, 2),
                "hold_seconds": round(held, 1),
                "reason": reason,
                "fraction": round(fraction, 4),
                "final": final,
                "tx_hash": tx_hash,
                "score": position.score,
            }
        )

    # -- read --------------------------------------------------------------

    def read(self) -> Iterator[dict[str, Any]]:
        yield from read_log(self.path)

    def read_all(self) -> Iterator[dict[str, Any]]:
        """The current file plus rotated copies, oldest to newest."""
        for index in range(self.backups, 0, -1):
            yield from read_log(self.path.with_suffix(self.path.suffix + f".{index}"))
        yield from read_log(self.path)


def read_log(path: str | Path) -> Iterator[dict[str, Any]]:
    """Line-by-line JSONL read. Broken lines are skipped with a warning."""
    file = Path(path)
    if not file.exists():
        return
    with file.open(encoding="utf-8") as fh:
        for number, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                log.warning("%s:%d — line did not parse, skipped", file, number)
                continue
            if isinstance(record, dict):
                yield record

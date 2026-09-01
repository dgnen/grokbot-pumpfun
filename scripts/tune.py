#!/usr/bin/env python3
"""Pick weights and a threshold from your own log.

Each log record stores scoring broken down by component, so the
total can be recomputed with different weights without calling the agents
again. The script answers two questions:

  * how the threshold changes the number of candidates and the result on closed trades;
  * which weights would have given a better result on the same material.

    python scripts/tune.py logs/trades.jsonl
    python scripts/tune.py logs/trades.jsonl --fine --top 20

HONEST CAVEAT, worth reading before you change the config.
The result is counted only on trades that were actually taken and
closed: what would have happened to a token filtered by the threshold, the log does not
know and cannot know. So the table shows what the threshold **keeps or loses** from
what is already known, not "how much you would have made". Weights fitted on
a couple dozen trades are fitting noise, not tuning.
"""

from __future__ import annotations

import argparse
import itertools
import sys
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.log import TradeLog, read_log
from src.models import Config

COMPONENTS = ("audit", "narrative", "timing", "metrics")
THRESHOLDS = (0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85)

# Below this number of closed trades any conclusions are coincidence.
MEANINGFUL_SAMPLE = 30


class Candidate(NamedTuple):
    """A token that reached scoring, and how it ended."""

    mint: str
    parts: tuple[float, float, float, float]
    bought: bool
    pnl_sol: float | None          # None — the position was never closed


def load_candidates(path: str, rotated: bool) -> list[Candidate]:
    source = TradeLog(path).read_all() if rotated else read_log(path)
    records = list(source)

    pnl: dict[str, float] = {}
    for record in records:
        if record.get("type") == "close":
            mint = record.get("mint", "")
            pnl[mint] = pnl.get(mint, 0.0) + float(record.get("pnl_sol") or 0.0)

    seen: set[str] = set()
    candidates: list[Candidate] = []
    for record in records:
        scores = record.get("scores")
        if not scores or record.get("type") == "close":
            continue
        mint = record.get("mint", "")
        if mint in seen:
            continue
        seen.add(mint)
        parts = tuple(float(scores.get(name) or 0.0) for name in COMPONENTS)
        bought = record.get("type") == "buy"
        candidates.append(
            Candidate(mint=mint, parts=parts, bought=bought,  # type: ignore[arg-type]
                      pnl_sol=pnl.get(mint) if bought else None)
        )
    return candidates


def prompt_versions_in(path: str, rotated: bool) -> set[str]:
    """Sets of prompt versions seen in buy records in the log."""
    source = TradeLog(path).read_all() if rotated else read_log(path)
    found: set[str] = set()
    for record in source:
        if record.get("type") != "buy":
            continue
        versions = record.get("prompt_versions") or {}
        if versions:
            found.add(", ".join(f"{k}={v}" for k, v in sorted(versions.items())))
    return found


def total(parts: tuple[float, ...], weights: tuple[float, ...]) -> float:
    return sum(part * weight for part, weight in zip(parts, weights, strict=True))


class Outcome(NamedTuple):
    passed: int
    kept_trades: int
    kept_pnl: float
    lost_trades: int
    lost_pnl: float

    @property
    def score(self) -> float:
        """Higher is better: kept profit minus missed profit."""
        return self.kept_pnl - max(0.0, self.lost_pnl)


def evaluate(
    candidates: list[Candidate], weights: tuple[float, ...], threshold: float
) -> Outcome:
    passed = kept = lost = 0
    kept_pnl = lost_pnl = 0.0
    for candidate in candidates:
        if total(candidate.parts, weights) >= threshold:
            passed += 1
            if candidate.pnl_sol is not None:
                kept += 1
                kept_pnl += candidate.pnl_sol
        elif candidate.pnl_sol is not None:
            lost += 1
            lost_pnl += candidate.pnl_sol
    return Outcome(passed, kept, round(kept_pnl, 6), lost, round(lost_pnl, 6))


def rank(row: tuple[Outcome, tuple[float, ...], float]) -> tuple[float, int, float]:
    """Sort key for weight sets.

    On a tie, prefer the one that kept more trades and
    leans less on a single component: a 1.00 weight on one score is
    almost always overfitting, not a finding.
    """
    outcome, weights, _ = row
    return (outcome.score, outcome.kept_trades, -max(weights))


def simplex(step: float) -> list[tuple[float, ...]]:
    """All four-weight sets with the given step that sum to one."""
    steps = round(1.0 / step)
    grid: list[tuple[float, ...]] = []
    for a, b, c in itertools.product(range(steps + 1), repeat=3):
        d = steps - a - b - c
        if d < 0:
            continue
        grid.append(tuple(round(x * step, 4) for x in (a, b, c, d)))
    return grid


def current_weights(config_path: str | None) -> tuple[float, ...]:
    if config_path and Path(config_path).exists():
        weights = Config.load(config_path, env={}).scoring.weights.model_dump()
    else:
        weights = Config().scoring.weights.model_dump()
    raw = [max(0.0, float(weights[name])) for name in COMPONENTS]
    stotal = sum(raw) or 1.0
    return tuple(round(value / stotal, 4) for value in raw)


def fmt_weights(weights: tuple[float, ...]) -> str:
    return " ".join(f"{name[:4]}={value:.2f}" for name, value in zip(COMPONENTS, weights,
                                                                    strict=True))


def main() -> int:
    parser = argparse.ArgumentParser(description="Pick weights and a threshold from the log")
    parser.add_argument("log", nargs="?", default="logs/trades.jsonl")
    parser.add_argument("--config", default="config.yaml", help="where to take current weights from")
    parser.add_argument("--rotated", action="store_true", help="include rotated copies")
    parser.add_argument("--fine", action="store_true", help="grid step 0.05 instead of 0.10")
    parser.add_argument("--top", type=int, default=10, help="how many sets to show")
    args = parser.parse_args()

    candidates = load_candidates(args.log, args.rotated)
    if not candidates:
        print(f"No scoring records in {args.log} — nothing to tune on.")
        return 1

    versions = prompt_versions_in(args.log, args.rotated)
    if len(versions) > 1:
        print("\n  CAUTION: the log has decisions made with different prompt versions:")
        for version in sorted(versions):
            print(f"    {version}")
        print("  These are records from different bots. Weights fitted on that mix")
        print("  do not belong to any of them.")

    closed = [c for c in candidates if c.pnl_sol is not None]
    weights = current_weights(args.config)

    print()
    print("=" * 68)
    print(f"  TUNE FROM LOG  {args.log}")
    print(f"  candidates with scoring: {len(candidates)}   "
          f"closed trades: {len(closed)}")
    print(f"  current weights: {fmt_weights(weights)}")
    print("=" * 68)

    if len(closed) < MEANINGFUL_SAMPLE:
        print(f"\n  CAUTION: {len(closed)} closed trades, that is fewer than "
              f"{MEANINGFUL_SAMPLE}.\n  Any fit on that material is fitting "
              "noise. Treat\n  the table as a description of what already "
              "happened, and nothing more.")

    # -- threshold at current weights --------------------------------------
    print("\nThreshold at current weights")
    print(f"  {'thresh':>6}  {'candidates':>10}  {'trades':>7}  {'their PnL':>10}  "
          f"{'cut':>8}  {'their PnL':>10}")
    for threshold in THRESHOLDS:
        outcome = evaluate(candidates, weights, threshold)
        print(f"  {threshold:>6.2f}  {outcome.passed:>10}  {outcome.kept_trades:>7}  "
              f"{outcome.kept_pnl:>+10.4f}  {outcome.lost_trades:>8}  "
              f"{outcome.lost_pnl:>+10.4f}")
    print("\n  \"cut\" — trades this threshold would NOT have let through;")
    print("  a negative PnL for them means the threshold would have saved that money.")

    if not closed:
        print("\nNo closed trades — nothing to compare weight sets on.")
        return 0

    # -- weight grid -------------------------------------------------------
    step = 0.05 if args.fine else 0.10
    grid = simplex(step)
    print(f"\nWeight search: {len(grid)} sets × {len(THRESHOLDS)} thresholds")

    # For each weight set keep only the best threshold: otherwise the top
    # of the table is the same set, cloned across the whole scale.
    best_by_weights: dict[tuple[float, ...], tuple[Outcome, tuple[float, ...], float]] = {}
    for candidate_weights in grid:
        for threshold in THRESHOLDS:
            outcome = evaluate(candidates, candidate_weights, threshold)
            if outcome.kept_trades == 0:
                continue          # a set that keeps no trades is useless
            row = (outcome, candidate_weights, threshold)
            current = best_by_weights.get(candidate_weights)
            if current is None or rank(row) > rank(current):
                best_by_weights[candidate_weights] = row

    results = sorted(best_by_weights.values(), key=rank, reverse=True)
    base = evaluate(candidates, weights, 0.65)

    print(f"\nTop {args.top} by \"kept profit minus missed\"")
    print(f"  {'#':>2}  {'weights':<38} {'thresh':>5}  {'trades':>6}  {'PnL':>10}")
    for index, (outcome, candidate_weights, threshold) in enumerate(results[:args.top], 1):
        print(f"  {index:>2}  {fmt_weights(candidate_weights):<38} {threshold:>5.2f}  "
              f"{outcome.kept_trades:>6}  {outcome.kept_pnl:>+10.4f}")

    print(f"\n  for comparison, current weights at threshold 0.65: "
          f"{base.kept_trades} trades, PnL {base.kept_pnl:+.4f}")
    print("\n  Again: what filtered tokens would have done, the log does not know.")
    print("  This table is about what already happened, not about future income.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

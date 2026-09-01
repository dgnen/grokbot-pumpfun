#!/usr/bin/env python3
"""Log replay: what the pipeline bought, what it skipped, and with what outcome.

    python scripts/replay.py logs/trades.jsonl
    python scripts/replay.py logs/trades.jsonl --since 2026-08-26
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.log import TradeLog, read_log

BUCKETS = [(0.0, 0.5), (0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.01)]

# Stages in the order a token passes through them. The funnel is built from these:
# you can see where the flow ends and which stage actually decides.
STAGES = [
    ("monitor", "monitor"),
    ("reputation", "creator memory"),
    ("analyzer", "analyzer"),
    ("scoring", "scoring"),
    ("checker", "checker"),
    ("risk", "risk gate"),
    ("executor", "execution"),
    ("pipeline", "processing failures"),
]


def parse_since(value: str | None) -> float:
    if not value:
        return 0.0
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC).timestamp()


def bucket_of(score: float) -> str:
    for low, high in BUCKETS:
        if low <= score < high:
            return f"{low:.1f}-{min(high, 1.0):.1f}"
    return "?"


def bar(count: int, total: int, width: int = 28) -> str:
    if not total:
        return ""
    return "█" * max(1, round(count / total * width)) if count else ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Summary of the pipeline log")
    parser.add_argument("log", nargs="?", default="logs/trades.jsonl")
    parser.add_argument("--since", help="YYYY-MM-DD, only records from this date")
    parser.add_argument("--rotated", action="store_true",
                        help="include rotated copies (.1, .2, ...)")
    args = parser.parse_args()

    since = parse_since(args.since)
    source = TradeLog(args.log).read_all() if args.rotated else read_log(args.log)
    records = [r for r in source if r.get("ts", 0) >= since]
    if not records:
        print(f"No records in {args.log}" + (f" since {args.since}" if args.since else ""))
        return 1

    buys = [r for r in records if r.get("type") == "buy"]
    skips = [r for r in records if r.get("type") == "skip"]
    closes = [r for r in records if r.get("type") == "close"]

    span_start = min(r.get("ts", 0) for r in records)
    span_end = max(r.get("ts", 0) for r in records)

    print()
    print("=" * 62)
    print(f"  REPLAY  {args.log}")
    print(f"  period: {fmt_ts(span_start)} — {fmt_ts(span_end)}")
    modes = Counter(r.get("mode", "?") for r in records)
    print(f"  mode:   {', '.join(f'{m} ({n})' for m, n in modes.most_common())}")
    print("=" * 62)

    seen = len(buys) + len(skips)
    print(f"\nTokens reviewed: {seen}")
    print(f"  bought:    {len(buys)}")
    print(f"  skipped:   {len(skips)}")
    if seen:
        print(f"  conversion: {len(buys) / seen * 100:.2f}%")

    # -- why they were filtered --------------------------------------------
    if skips:
        print("\nSkip reasons")
        by_stage: dict[str, Counter] = defaultdict(Counter)
        for record in skips:
            by_stage[record.get("stage", "?")][record.get("reason", "?")] += 1
        for stage in sorted(by_stage, key=lambda s: -sum(by_stage[s].values())):
            stage_total = sum(by_stage[stage].values())
            print(f"  [{stage}]  {stage_total}")
            for reason, count in by_stage[stage].most_common():
                print(f"      {reason[:28]:<28} {count:>5}  {bar(count, len(skips))}")

    # -- funnel ------------------------------------------------------------
    if skips or buys:
        print("\nFunnel")
        by_stage_count = Counter(r.get("stage", "?") for r in skips)
        remaining = seen
        rows = [("reviewed", remaining, "", "")]
        for stage, label in STAGES:
            dropped = by_stage_count.get(stage, 0)
            if not dropped:
                continue                       # stage filtered nobody
            remaining -= dropped
            share = f"{remaining / seen * 100:5.1f}%" if seen else ""
            rows.append((f"after «{label}»", remaining, share, f"−{dropped}"))
        rows.append(("bought", len(buys),
                     f"{len(buys) / seen * 100:5.1f}%" if seen else "", ""))
        width = max(len(name) for name, _, _, _ in rows)
        for name, count, share, dropped in rows:
            print(f"  {name:<{width}}  {count:>6}  {share:>7}  {dropped:>7}")
        unknown = set(by_stage_count) - {stage for stage, _ in STAGES}
        if unknown:
            print(f"  (stages outside the order: {', '.join(sorted(unknown))})")

    # -- scoring -----------------------------------------------------------
    scored = [r for r in records if (r.get("scores") or {}).get("total") is not None]
    if scored:
        print("\nDistribution of the final score")
        hist = Counter(bucket_of(r["scores"]["total"]) for r in scored)
        for low, high in BUCKETS:
            label = f"{low:.1f}-{min(high, 1.0):.1f}"
            count = hist.get(label, 0)
            print(f"  {label:<10} {count:>5}  {bar(count, len(scored))}")
        components = ("audit", "narrative", "timing", "metrics")
        print("\n  component averages:")
        for name in components:
            values = [r["scores"].get(name, 0.0) for r in scored]
            print(f"    {name:<10} {sum(values) / len(values):.3f}")

    # -- money -------------------------------------------------------------
    if closes:
        pnl = sum(r.get("pnl_sol", 0.0) for r in closes)
        wins = [r for r in closes if r.get("pnl_sol", 0.0) > 0]
        losses = [r for r in closes if r.get("pnl_sol", 0.0) <= 0]
        holds = [r.get("hold_seconds", 0.0) for r in closes]
        print("\nClosed positions")
        print(f"  closed:       {len(closes)}")
        print(f"  winners:      {len(wins)}  ({len(wins) / len(closes) * 100:.1f}%)")
        print(f"  losers:       {len(losses)}")
        print(f"  total PnL:    {pnl:+.4f} SOL")
        if wins:
            print(f"  best:         {max(r['pnl_sol'] for r in wins):+.4f} SOL")
        if losses:
            print(f"  worst:        {min(r['pnl_sol'] for r in losses):+.4f} SOL")
        print(f"  average hold: {sum(holds) / len(holds) / 60:.1f} min")

        print("\n  How positions end")
        print(f"    {'rule':<16} {'trades':>6} {'PnL':>10} {'avg %':>10} {'held':>9}")
        by_reason: dict[str, list[dict]] = defaultdict(list)
        for record in closes:
            by_reason[record.get("reason", "?")].append(record)
        for reason in sorted(by_reason, key=lambda r: -len(by_reason[r])):
            group = by_reason[reason]
            pnl_sum = sum(r.get("pnl_sol", 0.0) for r in group)
            pct = sum(r.get("pnl_pct", 0.0) for r in group) / len(group)
            hold = sum(r.get("hold_seconds", 0.0) for r in group) / len(group) / 60
            print(f"    {reason:<16} {len(group):>6} {pnl_sum:>+10.4f} "
                  f"{pct:>+10.1f} {hold:>7.0f}m")

        # -- creators -----------------------------------------------------
        by_creator: dict[str, list[dict]] = defaultdict(list)
        for record in closes:
            creator = record.get("creator")
            if creator:
                by_creator[creator].append(record)
        repeats = {c: rows for c, rows in by_creator.items() if len(rows) > 1}
        if repeats:
            print("\n  Creators whose tokens were taken more than once")
            for creator, rows in sorted(repeats.items(), key=lambda kv: -len(kv[1]))[:5]:
                pnl_sum = sum(r.get("pnl_sol", 0.0) for r in rows)
                worst = min(r.get("pnl_pct", 0.0) for r in rows)
                print(f"    {creator[:12]:<14} trades {len(rows):>2}  "
                      f"PnL {pnl_sum:>+8.4f}  worst {worst:>+7.1f}%")
    elif buys:
        print("\nNo closed positions — all buys are still in the market.")

    open_mints = {r["mint"] for r in buys} - {r["mint"] for r in closes}
    if open_mints:
        print(f"\nOpen now: {len(open_mints)} — {', '.join(sorted(open_mints)[:5])}")
    print()
    return 0


def fmt_ts(ts: float) -> str:
    if not ts:
        return "?"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


if __name__ == "__main__":
    raise SystemExit(main())

"""Single entry point: `grokbot <command>`.

Before this module, start, check, replay, dashboard, and weight tuning
lived in different places and were invoked differently. One command with
subcommands is not decoration: the runbook and the unit file should
hold one thing a person will remember in a month.

    grokbot run                # trading loop (dry-run by default)
    grokbot check              # check the config and exit
    grokbot doctor             # pre-flight environment check
    grokbot replay [log]       # log summary
    grokbot dashboard [log]    # live state
    grokbot tune [log]         # tune weights and threshold
    grokbot curve              # curve numbers: fee, impact, ceiling

`python -m src.pipeline` still works: old unit files and cron must not
break because the command moved.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import runpy
import sys
from pathlib import Path

from .curve import sanity_check
from .doctor import run_checks, summary
from .models import Config, ConfigError
from .pipeline import amain as run_pipeline

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="grokbot",
        description="pump.fun memecoin trading pipeline with Grok agents",
    )
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="trading loop")
    run.add_argument("--config", default="config.yaml")
    run.add_argument("--i-understand-the-risk", action="store_true",
                     help="required to start in live mode")

    check = sub.add_parser("check", help="check the config and exit")
    check.add_argument("--config", default="config.yaml")

    doctor = sub.add_parser("doctor", help="pre-flight environment check")
    doctor.add_argument("--config", default="config.yaml")
    doctor.add_argument("--offline", action="store_true", help="skip network checks")
    doctor.add_argument("--json", action="store_true", help="machine-readable output")

    for name, help_text in (("replay", "log summary"),
                            ("dashboard", "live state"),
                            ("tune", "tune weights and threshold")):
        script = sub.add_parser(name, help=help_text)
        script.add_argument("args", nargs=argparse.REMAINDER)

    sub.add_parser("curve", help="curve numbers: fee, impact, order ceiling")
    return parser


def load(config_path: str) -> Config:
    path = Path(config_path)
    if not path.exists():
        raise SystemExit(f"No config at {path}. Copy config.example.yaml to config.yaml.")
    try:
        return Config.load(path)
    except Exception as exc:
        raise SystemExit(f"Config {path} is unreadable: {exc}") from exc


def cmd_doctor(args: argparse.Namespace) -> int:
    config = load(args.config)
    report = asyncio.run(run_checks(config, skip_network=args.offline))

    if args.json:
        print(json.dumps({
            "summary": summary(report),
            "checks": [{"name": c.name, "status": c.status, "detail": c.detail}
                       for c in report.checks],
        }, ensure_ascii=False, indent=2))
    else:
        print()
        print("  PRE-FLIGHT CHECK")
        print("  " + "─" * 58)
        print(report.render())
        print()
    return 1 if report.failed else 0


def cmd_check(args: argparse.Namespace) -> int:
    config = load(args.config)
    try:
        warnings = config.check_ready()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    for warning in warnings:
        print(f"WARNING: {warning}", file=sys.stderr)
    print(json.dumps(config.redacted(), ensure_ascii=False, indent=2))
    print("\nConfig is ready to start.", file=sys.stderr)
    return 0


def cmd_curve() -> int:
    numbers = sanity_check()
    print("\n  pump.fun curve: what a trade on a fresh token costs\n")
    print(f"    price at curve start             {numbers['spot_price']:.12f} SOL")
    print(f"    0.5 SOL order moves the price    {numbers['impact_0.5_sol']:.2f} %")
    print(f"    0.5 SOL round trip costs         {numbers['round_trip_0.5_sol']:.2f} %")
    print(f"    order ceiling at 3%              {numbers['max_sol_for_3pct']:.3f} SOL")
    print(f"    tokens for 1 SOL                 {numbers['tokens_for_1_sol']:,.0f}")
    print("\n  Constants come from the pump.fun program and may go stale:")
    print("  before enabling live, check them against the on-chain state.\n")
    return 0


def run_script(name: str, args: list[str]) -> int:
    """Run a script from scripts/ as if it was invoked directly."""
    script = SCRIPTS / f"{name}.py"
    if not script.exists():
        raise SystemExit(f"Script {script} not found")
    sys.argv = [str(script), *args]
    try:
        runpy.run_path(str(script), run_name="__main__")
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    command = args.command or "run"

    if command == "run":
        run_args = ["--config", getattr(args, "config", "config.yaml")]
        if getattr(args, "i_understand_the_risk", False):
            run_args.append("--i-understand-the-risk")
        return asyncio.run(run_pipeline(run_args))
    if command == "check":
        return cmd_check(args)
    if command == "doctor":
        return cmd_doctor(args)
    if command == "curve":
        return cmd_curve()
    if command in ("replay", "dashboard", "tune"):
        return run_script(command, [a for a in args.args if a != "--"])

    build_parser().print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Pre-flight check: will the bot work if started right now.

Half of failed starts are not about logic, they are about the
environment: a key expired, the data provider answers 403, the socket
does not open from this network, the state directory is not writable.
All of that is discovered after an hour of silent idle work, and could
have been known in ten seconds before start.

Each check returns one of three: ok — it works; warn — it will work,
but worse than it could; fail — it will not work. None of them trade
or spend model tokens: reachability is checked, not answers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import websockets

from .curve import sanity_check
from .models import Config, mask
from .state import InstanceLock

log = logging.getLogger(__name__)

OK, WARN, FAIL = "ok", "warn", "fail"
MARKS = {OK: "✓", WARN: "!", FAIL: "✗"}

# Less free space than this — the log and state will soon hit the disk.
MIN_FREE_MB = 200


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    hint: str = ""

    def line(self) -> str:
        text = f"  {MARKS[self.status]} {self.name}"
        if self.detail:
            text += f": {self.detail}"
        if self.hint and self.status != OK:
            text += f"\n      → {self.hint}"
        return text


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, *checks: Check) -> None:
        self.checks.extend(checks)

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.status == FAIL]

    @property
    def warned(self) -> list[Check]:
        return [c for c in self.checks if c.status == WARN]

    def render(self) -> str:
        lines = [check.line() for check in self.checks]
        lines.append("")
        if self.failed:
            lines.append(f"  Will not start: failed checks — {len(self.failed)}.")
        elif self.warned:
            lines.append(f"  Will start, but there are notes ({len(self.warned)}).")
        else:
            lines.append("  Everything in place, ready to start.")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Individual checks
# --------------------------------------------------------------------------


def check_config(config: Config) -> list[Check]:
    errors, warnings = config.problems()
    checks = [
        Check("config", FAIL if errors else OK,
              "; ".join(errors) if errors else f"mode {config.mode}",
              "fix the listed items and run the check again")
    ]
    checks += [Check("setting", WARN, warning) for warning in warnings]
    return checks


def check_paths(config: Config) -> list[Check]:
    checks: list[Check] = []
    for name, raw in (("log", config.logging.path), ("state", config.ops.state_path),
                      ("reputation book", config.ops.reputation_path)):
        path = Path(raw)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            probe = path.parent / f".doctor{os.getpid()}"
            probe.write_text("x")
            probe.unlink()
            checks.append(Check(f"{name} is writable", OK, str(path.parent)))
        except OSError as exc:
            checks.append(Check(f"{name} is writable", FAIL, str(exc),
                                f"give the process write access to {path.parent}"))

    lock = InstanceLock(config.ops.state_path)
    holder = lock._holder()
    if holder and lock._alive(holder) and holder != os.getpid():
        checks.append(Check("state is free", FAIL, f"held by process {holder}",
                            "another bot is already running on this state"))
    else:
        checks.append(Check("state is free", OK))

    free_mb = shutil.disk_usage(Path(config.logging.path).parent.resolve()).free / 1e6
    checks.append(Check(
        "disk space",
        OK if free_mb >= MIN_FREE_MB else WARN,
        f"{free_mb:.0f} MB free",
        "the log grows fast; enable rotation or free space",
    ))
    return checks


async def check_grok(config: Config, client: httpx.AsyncClient | None = None) -> Check:
    """Key and API reachability. The model is not called — no model tokens spent."""
    url = config.grok.base_url.replace("/chat/completions", "/models")
    owns = client is None
    client = client or httpx.AsyncClient(timeout=10.0)
    try:
        response = await client.get(
            url, headers={"Authorization": f"Bearer {config.grok.key}"}
        )
        if response.status_code in (401, 403):
            return Check("Grok API", FAIL, f"key rejected (HTTP {response.status_code})",
                         f"check GROKBOT_GROK_API_KEY, currently {mask(config.grok.key)}")
        if response.status_code >= 500:
            return Check("Grok API", WARN, f"service responds {response.status_code}",
                         "looks like an xAI-side outage, try again later")
        if response.status_code >= 400:
            return Check("Grok API", WARN, f"unexpected response {response.status_code}")
        models = _model_names(response)
        missing = [m for m in (config.grok.fast_model, config.grok.checker_model)
                   if models and m not in models]
        if missing:
            return Check("Grok API", WARN, f"key accepted, but models not in the list: {missing}",
                         "check grok.fast_model and grok.checker_model against available ones")
        return Check("Grok API", OK, f"key accepted, {mask(config.grok.key)}")
    except Exception as exc:
        return Check("Grok API", FAIL, f"unreachable: {exc}",
                     "check the network and proxy")
    finally:
        if owns:
            await client.aclose()


def _model_names(response: httpx.Response) -> list[str]:
    try:
        body = response.json()
    except (json.JSONDecodeError, ValueError):
        return []
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list):
        return []
    return [item.get("id", "") for item in data if isinstance(item, dict)]


async def check_data_api(config: Config, client: httpx.AsyncClient | None = None) -> Check:
    owns = client is None
    client = client or httpx.AsyncClient(timeout=10.0)
    try:
        response = await client.get(config.data.rest_url)
        if response.status_code >= 500:
            return Check("data provider", WARN,
                         f"responds {response.status_code}",
                         "without it metrics are not computed: check data.rest_url")
        return Check("data provider", OK,
                     f"{config.data.rest_url} responds {response.status_code}")
    except Exception as exc:
        return Check("data provider", FAIL, f"unreachable: {exc}",
                     "check data.rest_url and the network")
    finally:
        if owns:
            await client.aclose()


async def check_socket(config: Config, wait_seconds: float = 15.0) -> Check:
    """Whether the launch stream opens and events come through."""
    try:
        async with websockets.connect(config.data.ws_url, open_timeout=wait_seconds) as ws:
            await ws.send(json.dumps({"method": "subscribeNewToken"}))
            started = time.monotonic()
            while True:
                left = wait_seconds - (time.monotonic() - started)
                if left <= 0:
                    break
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=left)
                except TimeoutError:
                    break
                payload = json.loads(raw) if raw else {}
                if isinstance(payload, dict) and payload.get("mint"):
                    return Check("launch stream", OK, "events are flowing")
            # Connected, but silence — not a broken socket, a quiet night.
            return Check("launch stream", WARN, "connected, but no events arrived",
                         "at night the stream can be sparse; retry the check in daytime")
    except Exception as exc:
        return Check("launch stream", FAIL, f"did not open: {exc}",
                     "check data.ws_url and that the network allows websocket")


async def check_rpc(config: Config, client: httpx.AsyncClient | None = None) -> Check:
    """RPC is only needed in live, but better to check it early."""
    owns = client is None
    client = client or httpx.AsyncClient(timeout=10.0)
    try:
        response = await client.post(
            config.solana.rpc_url,
            json={"jsonrpc": "2.0", "id": 1, "method": "getHealth"},
        )
        body = response.json() if response.status_code < 400 else {}
        healthy = body.get("result") == "ok"
        status = OK if healthy else (WARN if config.is_live else OK)
        detail = "healthy" if healthy else f"response {response.status_code}: {body or 'empty'}"
        return Check("Solana RPC", status, detail,
                     "critical in live: use a paid RPC")
    except Exception as exc:
        return Check("Solana RPC", FAIL if config.is_live else WARN,
                     f"unreachable: {exc}",
                     "not needed in dry-run, required in live")
    finally:
        if owns:
            await client.aclose()


def check_live_readiness(config: Config) -> list[Check]:
    if not config.is_live:
        return [Check("mode", OK, "dry-run: transactions are not sent")]
    from .executor import LiveExecutor

    checks = [Check("mode", WARN, "live: transactions will be sent for real")]
    stub = "intentionally unimplemented" in (LiveExecutor.buy.__doc__ or "")
    try:
        source = LiveExecutor.buy.__code__.co_consts
        stub = stub or any("intentionally unimplemented" in c for c in source if isinstance(c, str))
    except AttributeError:      # pragma: no cover
        pass
    if stub:
        checks.append(Check(
            "execution", FAIL, "LiveExecutor is still a stub",
            "write the transaction send or set mode: dry-run",
        ))
    return checks


def check_curve_constants() -> Check:
    """Curve numbers must stay plausible: the program gets updated."""
    numbers = sanity_check()
    cap = numbers["max_sol_for_3pct"]
    round_trip = numbers["round_trip_0.5_sol"]
    if not (0.1 < cap < 10) or not (0 < round_trip < 10):
        return Check("curve constants", WARN,
                     f"suspicious numbers: {numbers}",
                     "check pump.fun program parameters against the on-chain state")
    return Check("curve constants", OK,
                 f"0.5 SOL round trip ≈ {round_trip:.2f}%, "
                 f"order ceiling at 3% ≈ {cap:.2f} SOL")


# --------------------------------------------------------------------------
# All together
# --------------------------------------------------------------------------


async def run_checks(config: Config, skip_network: bool = False) -> Report:
    report = Report()
    report.add(*check_config(config))
    report.add(*check_paths(config))
    report.add(check_curve_constants())
    report.add(*check_live_readiness(config))

    if skip_network:
        report.add(Check("network", WARN, "network checks skipped (--offline)"))
        return report

    grok, data, rpc = await asyncio.gather(
        check_grok(config), check_data_api(config), check_rpc(config)
    )
    report.add(grok, data, rpc)
    report.add(await check_socket(config))
    return report


def summary(report: Report) -> dict[str, Any]:
    return {
        "ok": len([c for c in report.checks if c.status == OK]),
        "warn": len(report.warned),
        "fail": len(report.failed),
    }

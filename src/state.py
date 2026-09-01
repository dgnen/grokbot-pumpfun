"""State that must survive a restart.

A process that trades for days will be restarted: a deploy, an OOM, a
host reboot. If after restart it forgets open positions and the day's
counters, it will buy the same thing a second time and exceed the daily
loss limit — both limits start from zero.

The file is written atomically: first to a temp, then os.replace. So
the disk never holds a half-written JSON, even if the process was
killed mid-save.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .models import Position

log = logging.getLogger(__name__)

STATE_VERSION = 1


class PipelineState(BaseModel):
    """Snapshot of everything that must not be lost."""

    version: int = STATE_VERSION
    day: str = ""                                   # UTC day the counters belong to
    trades_today: int = 0
    realized_pnl_sol: float = 0.0
    grok_calls_today: int = 0
    losing_streak: int = 0
    cooldown_until: float = 0.0
    positions: dict[str, Position] = Field(default_factory=dict)
    updated_at: float = 0.0

    @property
    def is_empty(self) -> bool:
        return not self.positions and not self.trades_today and not self.realized_pnl_sol


class StateStore:
    """Atomic read and write of state to one JSON file."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> PipelineState | None:
        """Read state. None if the file is missing or corrupt."""
        if not self.path.exists():
            return None
        try:
            raw: Any = json.loads(self.path.read_text(encoding="utf-8"))
            state = PipelineState.model_validate(raw)
        except Exception as exc:
            backup = self.path.with_suffix(self.path.suffix + ".corrupt")
            log.error(
                "state %s unreadable (%s) — set aside as %s, starting clean",
                self.path, exc, backup,
            )
            with contextlib.suppress(OSError):
                os.replace(self.path, backup)
            return None

        if state.version != STATE_VERSION:
            log.warning("state version %d, expected %d — daily counters reset",
                        state.version, STATE_VERSION)
        return state

    def save(self, state: PipelineState) -> None:
        """Write state atomically. A write failure does not take down trading."""
        state.updated_at = time.time()
        tmp = self.path.with_suffix(self.path.suffix + f".tmp{os.getpid()}")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(state.model_dump(mode="json"), fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except OSError as exc:
            log.error("failed to save state to %s: %s", self.path, exc)
            tmp.unlink(missing_ok=True)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


class InstanceLock:
    """Lock of «one bot per one state».

    Two processes on one state file are two bots on one wallet:
    they will overwrite each other's positions, spend the daily limit
    twice, and buy the same token. The lock costs nothing and saves
    a scenario that is otherwise discovered by money.

    A lock from a dead process is taken over: a crash must not leave
    the system unable to start.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(str(path) + ".lock")
        self.acquired = False

    def _holder(self) -> int | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return int(data.get("pid") or 0) or None
        except (OSError, ValueError, TypeError):
            return None

    @staticmethod
    def _alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:      # the process exists, but is not ours
            return True
        return True

    def acquire(self) -> bool:
        """Take the lock. False if a live process holds it."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        pid = self._holder()
        if pid and pid != os.getpid() and self._alive(pid):
            log.error("state %s is already held by process %d — a second bot on "
                      "the same wallet will not start", self.path.stem, pid)
            return False
        if pid and not self._alive(pid):
            log.warning("lock left by dead process %d, taking it over", pid)
        try:
            self.path.write_text(
                json.dumps({"pid": os.getpid(), "started": time.time()}),
                encoding="utf-8",
            )
        except OSError as exc:
            log.error("failed to take lock %s: %s", self.path, exc)
            return False
        self.acquired = True
        return True

    def release(self) -> None:
        if not self.acquired:
            return
        if self._holder() == os.getpid():
            with contextlib.suppress(OSError):
                self.path.unlink()
        self.acquired = False

    def __enter__(self) -> InstanceLock:
        if not self.acquire():
            raise RuntimeError(f"state is held by another process: {self.path}")
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()


def describe(state: PipelineState) -> str:
    """Line for the startup log."""
    age = max(0.0, time.time() - state.updated_at) / 60 if state.updated_at else 0.0
    return (
        f"day {state.day or '?'}, trades {state.trades_today}, "
        f"PnL {state.realized_pnl_sol:+.4f} SOL, "
        f"open positions {len(state.positions)}, "
        f"Grok calls {state.grok_calls_today}, "
        f"written {age:.0f} min ago"
    )

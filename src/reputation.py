"""Memory of token creators.

The pipeline reviews every launch from a clean slate, so the same
deployer can rug us three times in a row — and each time they will be
"new". The auditor will not recognize them either: it sees one token,
not the address history.

The reputation book closes this cheaply and without an LLM: an address
whose token already closed deep in the red is cut at the door, before
a single Grok call. The decision is made only from our own closed
trades — not an internet blacklist and not a heuristic, a fact from
our own log.
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

log = logging.getLogger(__name__)

# How many addresses we remember. After that the oldest are evicted.
MAX_CREATORS = 50_000


class CreatorRecord(BaseModel):
    """What we know about an address from our own trades."""

    creator: str
    tokens_seen: int = 0
    tokens_bought: int = 0
    closed: int = 0
    rugs: int = 0
    realized_pnl_sol: float = 0.0
    worst_pnl_pct: float = 0.0
    last_seen: float = 0.0

    @property
    def is_known_bad(self) -> bool:
        return self.rugs > 0


class ReputationBook(BaseModel):
    """File of address history. Read on start, written after closes."""

    version: int = 1
    creators: dict[str, CreatorRecord] = Field(default_factory=dict)
    updated_at: float = 0.0

    # -- disk --------------------------------------------------------------

    @classmethod
    def load(cls, path: str | Path) -> ReputationBook:
        file = Path(path)
        if not file.exists():
            return cls()
        try:
            raw: Any = json.loads(file.read_text(encoding="utf-8"))
            return cls.model_validate(raw)
        except Exception as exc:
            log.error("reputation book %s is unreadable (%s) — starting empty", file, exc)
            return cls()

    def save(self, path: str | Path) -> None:
        """Atomically, like state: half a file is worse than none."""
        file = Path(path)
        self.updated_at = time.time()
        tmp = file.with_suffix(file.suffix + f".tmp{os.getpid()}")
        try:
            file.parent.mkdir(parents=True, exist_ok=True)
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(self.model_dump(mode="json"), fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, file)
        except OSError as exc:
            log.error("failed to save reputation book: %s", exc)
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)

    # -- bookkeeping -------------------------------------------------------

    def _record(self, creator: str) -> CreatorRecord:
        record = self.creators.get(creator)
        if record is None:
            record = CreatorRecord(creator=creator)
            self.creators[creator] = record
            self._evict_if_crowded()
        record.last_seen = time.time()
        return record

    def observe(self, creator: str | None) -> None:
        if creator:
            self._record(creator).tokens_seen += 1

    def record_open(self, creator: str | None) -> None:
        if creator:
            self._record(creator).tokens_bought += 1

    def record_close(
        self,
        creator: str | None,
        *,
        pnl_sol: float,
        pnl_pct: float,
        rug_loss_pct: float,
    ) -> CreatorRecord | None:
        """Close a position. A deep loss is counted against the address as a rug."""
        if not creator:
            return None
        record = self._record(creator)
        record.closed += 1
        record.realized_pnl_sol += pnl_sol
        record.worst_pnl_pct = min(record.worst_pnl_pct, pnl_pct)
        if rug_loss_pct and -pnl_pct >= rug_loss_pct:
            record.rugs += 1
            log.warning("creator %s: rug %d, worst result %.1f%% — "
                        "their next tokens are cut at the door",
                        creator[:8], record.rugs, record.worst_pnl_pct)
        return record

    # -- decision ----------------------------------------------------------

    def verdict(self, creator: str | None, block_after_rugs: int) -> str | None:
        """Reason not to deal with this address, or None."""
        if not creator or block_after_rugs <= 0:
            return None
        record = self.creators.get(creator)
        if record is None:
            return None
        if record.rugs >= block_after_rugs:
            return (f"creator already rugged {record.rugs} times "
                    f"(worst {record.worst_pnl_pct:.0f}%)")
        return None

    # -- maintenance -------------------------------------------------------

    def forget_older_than(self, days: float, now: float | None = None) -> int:
        """Forget addresses we have not heard from in a while. Rugs we keep."""
        if days <= 0:
            return 0
        cutoff = (now or time.time()) - days * 86_400
        stale = [
            key for key, record in self.creators.items()
            if record.last_seen < cutoff and not record.is_known_bad
        ]
        for key in stale:
            del self.creators[key]
        return len(stale)

    def _evict_if_crowded(self) -> None:
        """Evict clean addresses first: rugs are the value of this book."""
        if len(self.creators) <= MAX_CREATORS:
            return
        order = sorted(
            self.creators.items(),
            key=lambda item: (item[1].is_known_bad, item[1].last_seen),
        )
        for key, _ in order:
            if len(self.creators) <= MAX_CREATORS:
                break
            del self.creators[key]

    def summary(self) -> str:
        bad = sum(1 for r in self.creators.values() if r.is_known_bad)
        return f"addresses {len(self.creators)}, of them with rugs {bad}"

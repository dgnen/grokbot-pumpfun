"""WebSocket monitor of new pump.fun launches.

First stage of the pipeline and the coarsest: filters in code, no LLM,
and cuts about 94% of the stream. Anything that did not get through
here goes no further and spends no Grok tokens.

A freshly created token cannot pass the age filter, so launches go into
the `pending` buffer, accumulate trades from the same socket, and are
checked again once they reach `min_age_seconds`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from typing import Any

import websockets

from .curve import CURVE_COMPLETION_SOL, progress_from_sol
from .models import Config, FilterConfig, Token

log = logging.getLogger(__name__)

__all__ = ["CURVE_COMPLETION_SOL", "LaunchMonitor", "parse_create_event", "passes_filter"]

# How long to keep a launch in the buffer if it never gathered buyers.
PENDING_TTL_SECONDS = 900.0

# Memory ceilings. The process lives for days, and pump.fun has
# thousands of launches an hour: without a cap both the buffer and the
# already-seen list grow without end.
MAX_PENDING = 2_000
MAX_REMEMBERED = 20_000


class SeenSet:
    """Set of the last N keys. Old ones are evicted, memory does not leak."""

    def __init__(self, maxlen: int = MAX_REMEMBERED) -> None:
        self.maxlen = maxlen
        self._items: OrderedDict[str, None] = OrderedDict()

    def add(self, key: str) -> None:
        self._items[key] = None
        self._items.move_to_end(key)
        while len(self._items) > self.maxlen:
            self._items.popitem(last=False)

    def __contains__(self, key: object) -> bool:
        return key in self._items

    def __len__(self) -> int:
        return len(self._items)


def parse_create_event(payload: dict[str, Any]) -> Token | None:
    """Token-create event -> Token. None if the event is not a create."""
    if payload.get("txType") not in ("create", "created"):
        return None
    mint = payload.get("mint") or payload.get("mintAddress")
    if not mint:
        return None

    sol_in_curve = float(payload.get("vSolInBondingCurve") or 0.0)
    created = payload.get("timestamp") or payload.get("createdTimestamp")
    created_ts = (
        float(created) / 1000.0
        if created and float(created) > 1e11
        else float(created or time.time())
    )

    return Token(
        mint=mint,
        name=payload.get("name"),
        symbol=payload.get("symbol"),
        description=payload.get("description"),
        image_uri=payload.get("image") or payload.get("image_uri"),
        metadata_uri=payload.get("uri") or payload.get("metadata_uri"),
        twitter=payload.get("twitter"),
        telegram=payload.get("telegram"),
        website=payload.get("website"),
        creator=payload.get("traderPublicKey") or payload.get("creator"),
        created_timestamp=created_ts,
        sol_in_curve=sol_in_curve,
        market_cap_sol=float(payload.get("marketCapSol") or 0.0),
        # The reserve the socket reports includes 30 virtual SOL: they
        # sit in the curve from birth and are not progress.
        curve_progress=progress_from_sol(sol_in_curve),
    )


def passes_filter(token: Token, cfg: FilterConfig) -> tuple[bool, str]:
    """Base filter. Returns (passed, reject reason or "ok").

    A reason is always returned — it goes into the log as `skip.reason`,
    otherwise later you cannot tell what the stream died on.
    """
    # Final verdicts first (metadata, a full curve), then temporary
    # ones — the token may still ripen in the monitor buffer.
    if cfg.require_metadata and not token.has_metadata:
        return False, "no_metadata"
    if token.curve_progress >= cfg.max_curve_progress:
        return False, "curve_too_full"
    if token.age_seconds < cfg.min_age_seconds:
        return False, "too_young"
    if token.unique_buyers < cfg.min_unique_buyers:
        return False, "few_buyers"
    return True, "ok"


class LaunchMonitor:
    """Subscribe to new tokens and their trades, filtering on the fly."""

    def __init__(
        self,
        config: Config,
        on_skip: Callable[[Token, str], None] | None = None,
    ) -> None:
        self.config = config
        self.filter = config.filter
        self.on_skip = on_skip
        self.pending: dict[str, Token] = {}
        self._buyers: dict[str, set[str]] = {}
        self._emitted = SeenSet()

    # -- event handling ----------------------------------------------------

    def handle_event(self, payload: dict[str, Any]) -> Token | None:
        """One message from the socket. Returns the token if it is ready to go on."""
        tx_type = payload.get("txType")

        if tx_type in ("create", "created"):
            token = parse_create_event(payload)
            if token and token.mint not in self._emitted:
                self._evict_if_crowded()
                self.pending[token.mint] = token
                # Do not count the creator as a buyer: we need a count
                # of outside wallets, not everyone in a row.
                self._buyers[token.mint] = set()
            return None

        mint = payload.get("mint")
        if not mint or mint not in self.pending:
            return None

        token = self.pending[mint]
        wallet = payload.get("traderPublicKey") or payload.get("wallet")
        if tx_type == "buy" and wallet:
            self._buyers[mint].add(wallet)
        token.unique_buyers = len(self._buyers[mint])

        sol_in_curve = payload.get("vSolInBondingCurve")
        if sol_in_curve is not None:
            token.sol_in_curve = float(sol_in_curve)
            token.curve_progress = progress_from_sol(token.sol_in_curve)
        if payload.get("marketCapSol") is not None:
            token.market_cap_sol = float(payload["marketCapSol"])

        return self._promote(token)

    def _promote(self, token: Token) -> Token | None:
        """Check a ripe token and pull it from the buffer if a decision was made."""
        ok, reason = passes_filter(token, self.filter)
        if ok:
            self._forget(token.mint)
            self._emitted.add(token.mint)
            return token
        # too_young / few_buyers — may still ripen, the rest is final
        if reason in ("too_young", "few_buyers"):
            return None
        self._forget(token.mint)
        self._emitted.add(token.mint)
        if self.on_skip:
            self.on_skip(token, reason)
        return None

    def sweep(self, now: float | None = None) -> list[Token]:
        """Walk the buffer: ripe ones out, stale ones gone."""
        now = now or time.time()
        ready: list[Token] = []
        for mint in list(self.pending):
            token = self.pending[mint]
            promoted = self._promote(token)
            if promoted is not None:
                ready.append(promoted)
            elif now - token.created_timestamp > PENDING_TTL_SECONDS:
                self._forget(mint)
                self._emitted.add(mint)
                if self.on_skip:
                    self.on_skip(token, "stale_no_traction")
        return ready

    def _forget(self, mint: str) -> None:
        self.pending.pop(mint, None)
        self._buyers.pop(mint, None)

    def _evict_if_crowded(self) -> None:
        """Buffer is full — drop the oldest unripe launches."""
        while len(self.pending) >= MAX_PENDING:
            oldest = min(self.pending, key=lambda mint: self.pending[mint].created_timestamp)
            token = self.pending[oldest]
            self._forget(oldest)
            self._emitted.add(oldest)
            if self.on_skip:
                self.on_skip(token, "buffer_overflow")

    # -- socket ------------------------------------------------------------

    async def stream(self) -> AsyncIterator[Token]:
        """Infinite stream of filtered tokens. Reconnects on its own."""
        sweeper_delay = 10.0
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(self.config.data.ws_url) as ws:
                    await ws.send(json.dumps({"method": "subscribeNewToken"}))
                    log.info("monitor connected to %s", self.config.data.ws_url)
                    backoff = 1.0
                    last_sweep = time.time()
                    while True:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=sweeper_delay)
                        except TimeoutError:
                            raw = None
                        if raw:
                            try:
                                payload = json.loads(raw)
                            except json.JSONDecodeError:
                                continue
                            if isinstance(payload, dict):
                                token = self.handle_event(payload)
                                if token is not None:
                                    await self._subscribe_trades(ws, token.mint, off=True)
                                    yield token
                                elif payload.get("txType") in ("create", "created"):
                                    mint = payload.get("mint")
                                    if mint:
                                        await self._subscribe_trades(ws, mint)
                        if time.time() - last_sweep >= sweeper_delay:
                            last_sweep = time.time()
                            for token in self.sweep():
                                yield token
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # socket drop — wait and reconnect
                log.warning("monitor dropped (%s), reconnecting in %.0fs", exc, backoff)
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    @staticmethod
    async def _subscribe_trades(ws: Any, mint: str, off: bool = False) -> None:
        method = "unsubscribeTokenTrade" if off else "subscribeTokenTrade"
        with contextlib.suppress(Exception):
            await ws.send(json.dumps({"method": method, "keys": [mint]}))

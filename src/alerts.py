"""Notifications to an external webhook.

The pipeline runs unsupervised, and the only way to learn that the
circuit opened or the daily limit was spent is to look in the log.
Sending events outward closes that gap.

Three rules from which everything else follows:
  * off by default — an empty `webhook_url` sends nothing;
  * never interferes with trading — send is a background task, any
    network error stays in the log and does not surface;
  * does not turn into spam — the event stream is rate-limited, extras
    are counted and dropped, not queued.

Message format is intentionally simple: `text` is understood by Slack,
`content` by Discord, the other fields bother neither. Telegram needs
an intermediate relay: it has a different protocol.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from typing import Any

import httpx

from .models import AlertsConfig

log = logging.getLogger(__name__)

# Events the pipeline knows how to send. The set in config is a subset.
KNOWN_EVENTS = (
    "started", "stopped", "buy", "close", "rug",
    "breaker", "halted", "stalled", "blind", "cooldown",
)


class Notifier:
    """Send events to a webhook. Works while disabled — then it stays silent."""

    def __init__(self, config: AlertsConfig, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self._client = client
        self._owns_client = client is None
        self._tasks: set[asyncio.Task] = set()
        self._recent: deque[float] = deque()
        self.sent = 0
        self.dropped = 0
        self.failed = 0

    @property
    def enabled(self) -> bool:
        return bool(self.config.webhook_url.get_secret_value().strip())

    def wants(self, event: str) -> bool:
        return self.enabled and event in self.config.events

    # -- send --------------------------------------------------------------

    def notify(self, event: str, text: str, **fields: Any) -> asyncio.Task | None:
        """Queue an event for send. Returns a task or None.

        Synchronous call: trading logic must not wait on the network
        for a notification.
        """
        if not self.wants(event):
            return None
        if not self._allow_now():
            self.dropped += 1
            return None
        task = asyncio.create_task(self._send(event, text, fields), name=f"alert-{event}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _allow_now(self) -> bool:
        """Sliding one-minute window. A launch spike must not become a mail spike."""
        now = time.monotonic()
        while self._recent and now - self._recent[0] > 60.0:
            self._recent.popleft()
        if len(self._recent) >= self.config.max_per_minute:
            if self.dropped == 0:
                log.warning("notifications held back: more than %d per minute",
                            self.config.max_per_minute)
            return False
        self._recent.append(now)
        return True

    async def _send(self, event: str, text: str, fields: dict[str, Any]) -> None:
        payload = {
            "event": event,
            "text": f"[grokbot] {text}",
            "content": f"[grokbot] {text}",   # Discord reads this field
            "fields": fields,
        }
        try:
            client = self._ensure_client()
            response = await client.post(
                self.config.webhook_url.get_secret_value(),
                json=payload,
                timeout=self.config.timeout_seconds,
            )
            if response.status_code >= 400:
                self.failed += 1
                # Do not print the URL: it contains a token
                log.warning("webhook replied %d to event %s", response.status_code, event)
                return
            self.sent += 1
        except Exception as exc:
            self.failed += 1
            log.warning("notification %s did not go out: %s", event, exc)

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.config.timeout_seconds)
        return self._client

    # -- shutdown ----------------------------------------------------------

    async def aclose(self, grace: float = 5.0) -> None:
        """Let the last notifications go out and close the connection."""
        if self._tasks:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    asyncio.gather(*self._tasks, return_exceptions=True), timeout=grace
                )
        for task in list(self._tasks):
            task.cancel()
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def snapshot(self) -> dict[str, Any]:
        return {"sent": self.sent, "dropped": self.dropped, "failed": self.failed}

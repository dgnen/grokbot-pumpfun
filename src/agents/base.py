"""Base Grok API agent class.

All shared call mechanics live here: request assembly, temperature=0,
strict JSON parsing, retries with exponential backoff, timeout.

Core rule: on any error — timeout, HTTP, malformed JSON, invalid
schema — the agent returns the MOST PESSIMISTIC result, not empty
and not "neutral". A broken check equals a refusal. A silent skip
in this market costs money.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import Any, ClassVar, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from ..models import Config
from ..ops import GrokOps

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class GrokAgentError(RuntimeError):
    """Grok call failed after all retries."""


class GrokAgent:
    """One agent = one prompt + one pydantic response schema."""

    name: ClassVar[str] = "agent"
    # Prompt version. Changes with the prompt text and is written to the log:
    # without it you cannot compare stats before and after a prompt edit —
    # those are different bots that look like one.
    version: ClassVar[str] = "0"
    prompt: ClassVar[str] = ""            # system prompt, constant of the agent module
    result_model: ClassVar[type[BaseModel]] = BaseModel
    use_checker_model: ClassVar[bool] = False

    def __init__(
        self,
        config: Config,
        client: httpx.AsyncClient | None = None,
        ops: GrokOps | None = None,
    ) -> None:
        self.config = config
        self.grok = config.grok
        self.ops = ops              # process-wide limiters; None — without them
        self._client = client
        self._owns_client = client is None

    # -- lifecycle ---------------------------------------------------------

    @property
    def model(self) -> str:
        return self.grok.checker_model if self.use_checker_model else self.grok.fast_model

    async def __aenter__(self) -> GrokAgent:
        self._ensure_client()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.grok.timeout_seconds)
        return self._client

    # -- overridden in subclasses ------------------------------------------

    def build_user_message(self, *args: Any, **kwargs: Any) -> str:
        raise NotImplementedError

    def fallback(self, reason: str) -> Any:
        """Pessimistic result. The subclass must return the worst case."""
        raise NotImplementedError

    # -- main entry --------------------------------------------------------

    async def run(self, *args: Any, **kwargs: Any) -> Any:
        """Assemble the request, call Grok, parse the response.

        Never leaks exceptions: any failure becomes a pessimistic result.
        """
        try:
            message = self.build_user_message(*args, **kwargs)
        except Exception as exc:
            log.warning("[%s] failed to assemble prompt: %s", self.name, exc)
            return self.fallback(f"prompt_error: {exc}")

        try:
            raw = await self._call(message)
        except GrokAgentError as exc:
            log.warning("[%s] call failed: %s", self.name, exc)
            return self.fallback(str(exc))

        try:
            data = extract_json(raw)
        except ValueError as exc:
            log.warning("[%s] response did not parse as JSON: %s", self.name, exc)
            return self.fallback(f"parse_error: {exc}")

        try:
            return self.result_model.model_validate(data)
        except ValidationError as exc:
            log.warning("[%s] response did not match the schema: %s", self.name, exc)
            return self.fallback(f"schema_error: {exc.error_count()} fields")

    # -- transport ---------------------------------------------------------

    def _payload(self, message: str) -> dict[str, Any]:
        return {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": self.prompt},
                {"role": "user", "content": message},
            ],
        }

    async def _call(self, message: str) -> str:
        """POST to Grok with retries. Returns the model's response text."""
        client = self._ensure_client()
        headers = {
            "Authorization": f"Bearer {self.grok.key}",
            "Content-Type": "application/json",
        }
        last_error: Exception | None = None

        for attempt in range(self.grok.max_retries):
            # Ask the limiters on every attempt: the circuit may have
            # opened, and the budget may have run out while we were retrying.
            if self.ops is not None:
                blocked = self.ops.precheck(self.name)
                if blocked:
                    raise GrokAgentError(blocked)

            if attempt:
                await asyncio.sleep(self._backoff(attempt))
            try:
                async with self._slot():
                    resp = await client.post(
                        self.grok.base_url,
                        json=self._payload(message),
                        headers=headers,
                        timeout=self.grok.timeout_seconds,
                    )
                if resp.status_code >= 500 or resp.status_code == 429:
                    last_error = GrokAgentError(f"HTTP {resp.status_code}")
                    self._failed()
                    continue
                resp.raise_for_status()
                body = resp.json()
                content = body["choices"][0]["message"]["content"]
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                self._failed()
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                last_error = GrokAgentError(f"unexpected response shape: {exc}")
                self._failed()
            except httpx.HTTPStatusError as exc:
                # Retrying 4xx other than 429 is pointless: the key or the request is wrong
                self._failed()
                raise GrokAgentError(f"HTTP {exc.response.status_code}") from exc
            else:
                if self.ops is not None:
                    self.ops.record_success(self.name, body.get("usage"))
                return content

        raise GrokAgentError(f"after {self.grok.max_retries} attempts: {last_error}")

    def _backoff(self, attempt: int) -> float:
        """Exponential backoff with jitter.

        Jitter matters: without it a batch of agents that started together
        retries together too and finishes off an already struggling API.
        """
        base = self.grok.retry_base_delay * (2 ** (attempt - 1))
        delay = base * (0.5 + random.random())
        log.debug("[%s] retry %d in %.2fs", self.name, attempt, delay)
        return delay

    def _slot(self) -> Any:
        if self.ops is None:
            return _NullSlot()
        return self.ops.slot(self.name)

    def _failed(self) -> None:
        if self.ops is not None:
            self.ops.record_failure(self.name)


class _NullSlot:
    """Queue stub for an agent without limiters (tests, single call)."""

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: Any) -> None:
        return None


# --------------------------------------------------------------------------
# Response parsing
# --------------------------------------------------------------------------

JSON_ONLY = (
    "Reply with ONLY valid JSON of the specified shape. "
    "No explanations, no text before or after, no markdown wrapper and no ```."
)


def extract_json(text: str) -> dict[str, Any]:
    """Extract a JSON object from the model response.

    The prompt requires bare JSON, but models still sometimes wrap it
    in ```json. Strip the fence and cut out the first balanced object.
    """
    if not text or not text.strip():
        raise ValueError("empty response")

    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1] if "```" in cleaned[3:] else cleaned[3:]
        if cleaned.lstrip().lower().startswith("json"):
            cleaned = cleaned.lstrip()[4:]
        cleaned = cleaned.strip()

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        data = json.loads(_first_object(cleaned))

    if not isinstance(data, dict):
        raise ValueError(f"expected an object, got {type(data).__name__}")
    return data


def _first_object(text: str) -> str:
    """First balanced {...} in the string."""
    start = text.find("{")
    if start == -1:
        raise ValueError("no JSON object in the response")
    depth = 0
    in_string = False
    escaped = False
    for i, ch in enumerate(text[start:], start):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise ValueError("unclosed JSON object")

"""Provider-neutral reasoning interface.

Alan depends on this protocol, never on a vendor SDK.  Every provider adapter
translates the same three concepts — system prompt, conversation, JSON mode —
so swapping models cannot change what Alan is able to do.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence, runtime_checkable

import httpx

from ..errors import LLMError, LLMTimeout


@dataclass(slots=True)
class Message:
    role: str  # "user" | "assistant"
    content: str


@dataclass(slots=True)
class LLMResponse:
    text: str
    model: str
    provider: str
    usage: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class LLMClient(Protocol):
    provider: str
    model: str

    async def complete(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        max_tokens: int | None = None,
        temperature: float | None = None,
        json_mode: bool = False,
    ) -> LLMResponse: ...

    async def aclose(self) -> None: ...


class BaseHTTPClient:
    """Shared HTTP plumbing: timeouts, retries, and honest error messages."""

    provider = "generic"

    def __init__(self, *, timeout: float = 90.0, max_retries: int = 2) -> None:
        self._timeout = timeout
        self._max_retries = max(0, max_retries)
        self._client: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(self._timeout))
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    async def _post(self, url: str, *, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._http().post(url, headers=headers, json=payload)
            except httpx.TimeoutException as exc:
                last_error = exc
                if attempt >= self._max_retries:
                    raise LLMTimeout(f"{self.provider} timed out after {self._timeout:.0f}s") from exc
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt >= self._max_retries:
                    raise LLMError(f"{self.provider} transport error: {exc}") from exc
            else:
                if response.status_code < 400:
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise LLMError(f"{self.provider} returned non-JSON response") from exc
                if response.status_code in {408, 409, 429, 500, 502, 503, 504} and attempt < self._max_retries:
                    last_error = LLMError(f"{self.provider} HTTP {response.status_code}")
                else:
                    body = response.text[:600]
                    raise LLMError(f"{self.provider} HTTP {response.status_code}: {body}")
            await asyncio.sleep(1.5 * (2**attempt))

        raise LLMError(f"{self.provider} failed: {last_error}")

"""Anthropic Messages API adapter."""

from __future__ import annotations

from typing import Sequence

from ..errors import LLMError
from .base import BaseHTTPClient, LLMResponse, Message

DEFAULT_BASE = "https://api.anthropic.com"
API_VERSION = "2023-06-01"


class AnthropicClient(BaseHTTPClient):
    provider = "anthropic"

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str = "",
        timeout: float = 90.0,
        max_retries: int = 2,
    ) -> None:
        super().__init__(timeout=timeout, max_retries=max_retries)
        self.model = model
        self._api_key = api_key
        self._base = (base_url or DEFAULT_BASE).rstrip("/")

    async def complete(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        max_tokens: int | None = None,
        temperature: float | None = None,
        json_mode: bool = False,
    ) -> LLMResponse:
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        }
        system_text = system
        if json_mode:
            system_text += "\n\nReturn only a single JSON object. No prose, no code fences."

        payload = {
            "model": self.model,
            "max_tokens": max_tokens or 2048,
            "temperature": temperature if temperature is not None else 0.2,
            "system": system_text,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
        }
        data = await self._post(f"{self._base}/v1/messages", headers=headers, payload=payload)

        blocks = data.get("content") or []
        text = "".join(
            block.get("text", "") for block in blocks if isinstance(block, dict) and block.get("type") == "text"
        )
        if not text:
            raise LLMError(f"anthropic returned no text: {data.get('stop_reason')}")
        return LLMResponse(
            text=text,
            model=data.get("model") or self.model,
            provider=self.provider,
            usage=data.get("usage") or {},
            raw=data,
        )

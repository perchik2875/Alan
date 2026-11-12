"""OpenAI-compatible chat completions.

Works with OpenAI, OpenRouter, DeepSeek, Groq, Together, vLLM, LM Studio, and
anything else that implements ``POST /chat/completions``.
"""

from __future__ import annotations

from typing import Any, Sequence

from ..errors import LLMError
from .base import BaseHTTPClient, LLMResponse, Message

DEFAULT_BASE = "https://api.openai.com/v1"


class OpenAICompatibleClient(BaseHTTPClient):
    provider = "openai"

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
        if self._base.endswith("/chat/completions"):
            self._base = self._base[: -len("/chat/completions")]

    def _payload(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        max_tokens: int | None,
        temperature: float | None,
        json_mode: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}]
            + [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature if temperature is not None else 0.2,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        return payload

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
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self._base}/chat/completions"
        payload = self._payload(
            system=system,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            json_mode=json_mode,
        )
        try:
            data = await self._post(url, headers=headers, payload=payload)
        except LLMError as exc:
            # Some compatible servers reject response_format.  JSON is also
            # enforced by the prompt, so degrade rather than fail.
            if json_mode and "response_format" in str(exc):
                payload.pop("response_format", None)
                data = await self._post(url, headers=headers, payload=payload)
            else:
                raise

        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"openai-compatible response had no choices: {data}")
        message = choices[0].get("message") or {}
        text = message.get("content") or ""
        if isinstance(text, list):  # some servers return content parts
            text = "".join(part.get("text", "") for part in text if isinstance(part, dict))
        return LLMResponse(
            text=text,
            model=data.get("model") or self.model,
            provider=self.provider,
            usage=data.get("usage") or {},
            raw=data,
        )

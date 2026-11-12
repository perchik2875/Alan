"""Google Gemini (Generative Language API) adapter."""

from __future__ import annotations

from typing import Sequence

from ..errors import LLMError
from .base import BaseHTTPClient, LLMResponse, Message

DEFAULT_BASE = "https://generativelanguage.googleapis.com/v1beta"


class GeminiClient(BaseHTTPClient):
    provider = "gemini"

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
        headers = {"x-goog-api-key": self._api_key, "Content-Type": "application/json"}
        generation: dict[str, object] = {
            "temperature": temperature if temperature is not None else 0.2,
        }
        if max_tokens:
            generation["maxOutputTokens"] = max_tokens
        if json_mode:
            generation["responseMimeType"] = "application/json"

        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [
                {
                    "role": "model" if m.role == "assistant" else "user",
                    "parts": [{"text": m.content}],
                }
                for m in messages
            ],
            "generationConfig": generation,
        }
        url = f"{self._base}/models/{self.model}:generateContent"
        data = await self._post(url, headers=headers, payload=payload)

        candidates = data.get("candidates") or []
        if not candidates:
            raise LLMError(f"gemini returned no candidates: {data.get('promptFeedback')}")
        parts = ((candidates[0].get("content") or {}).get("parts")) or []
        text = "".join(part.get("text", "") for part in parts if isinstance(part, dict))
        if not text:
            raise LLMError(f"gemini returned no text: {candidates[0].get('finishReason')}")
        return LLMResponse(
            text=text,
            model=self.model,
            provider=self.provider,
            usage=data.get("usageMetadata") or {},
            raw=data,
        )

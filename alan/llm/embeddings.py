"""Embedding providers for meaning-based retrieval.

Embeddings are optional.  Without them Alan still answers from exact-term
search and from notes the model selects by title; with them, paraphrase and
cross-language recall improve substantially.
"""

from __future__ import annotations

from typing import Protocol, Sequence, runtime_checkable

from ..config import EmbeddingConfig
from ..errors import EmbeddingError
from .base import BaseHTTPClient

OPENAI_DEFAULT = "https://api.openai.com/v1"
GEMINI_DEFAULT = "https://generativelanguage.googleapis.com/v1beta"


@runtime_checkable
class Embedder(Protocol):
    model: str
    enabled: bool

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...

    async def aclose(self) -> None: ...


class NullEmbedder:
    model = ""
    enabled = False

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return []

    async def aclose(self) -> None:
        return None


class OpenAIEmbedder(BaseHTTPClient):
    provider = "openai"

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str = "",
        timeout: float = 60.0,
        dimensions: int = 0,
    ) -> None:
        super().__init__(timeout=timeout, max_retries=2)
        self.model = model
        self.enabled = True
        self._api_key = api_key
        self._base = (base_url or OPENAI_DEFAULT).rstrip("/")
        self._dimensions = dimensions

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        payload: dict[str, object] = {"model": self.model, "input": list(texts)}
        if self._dimensions:
            payload["dimensions"] = self._dimensions
        data = await self._post(f"{self._base}/embeddings", headers=headers, payload=payload)
        rows = data.get("data") or []
        if len(rows) != len(texts):
            raise EmbeddingError(f"embedding count mismatch: {len(rows)} vs {len(texts)}")
        ordered = sorted(rows, key=lambda item: item.get("index", 0))
        return [list(map(float, row["embedding"])) for row in ordered]


class GeminiEmbedder(BaseHTTPClient):
    provider = "gemini"

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str = "",
        timeout: float = 60.0,
        dimensions: int = 0,
    ) -> None:
        super().__init__(timeout=timeout, max_retries=2)
        self.model = model
        self.enabled = True
        self._api_key = api_key
        self._base = (base_url or GEMINI_DEFAULT).rstrip("/")
        self._dimensions = dimensions

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        headers = {"x-goog-api-key": self._api_key, "Content-Type": "application/json"}
        requests = []
        for text in texts:
            item: dict[str, object] = {
                "model": f"models/{self.model}",
                "content": {"parts": [{"text": text[:8000]}]},
            }
            if self._dimensions:
                item["outputDimensionality"] = self._dimensions
            requests.append(item)
        payload = {"requests": requests}
        data = await self._post(
            f"{self._base}/models/{self.model}:batchEmbedContents",
            headers=headers,
            payload=payload,
        )
        embeddings = data.get("embeddings") or []
        if len(embeddings) != len(texts):
            raise EmbeddingError(f"embedding count mismatch: {len(embeddings)} vs {len(texts)}")
        return [list(map(float, row.get("values") or [])) for row in embeddings]


def build_embedder(cfg: EmbeddingConfig) -> Embedder:
    if cfg.provider in {"", "none", "off", "disabled"}:
        return NullEmbedder()
    if cfg.provider in {"openai", "openai-compatible", "compatible"}:
        return OpenAIEmbedder(
            model=cfg.model,
            api_key=cfg.api_key,
            base_url=cfg.base_url,
            timeout=cfg.timeout,
            dimensions=cfg.dimensions,
        )
    if cfg.provider == "gemini":
        return GeminiEmbedder(
            model=cfg.model,
            api_key=cfg.api_key,
            base_url=cfg.base_url,
            timeout=cfg.timeout,
            dimensions=cfg.dimensions,
        )
    raise EmbeddingError(f"unknown embedding provider: {cfg.provider}")

"""Construct the reasoning client from configuration."""

from __future__ import annotations

from ..config import LLMConfig
from ..errors import ConfigError
from .anthropic import AnthropicClient
from .base import LLMClient
from .gemini import GeminiClient
from .openai_compat import OpenAICompatibleClient


def build_llm(cfg: LLMConfig) -> LLMClient:
    common = {
        "model": cfg.model,
        "api_key": cfg.api_key,
        "base_url": cfg.base_url,
        "timeout": cfg.timeout,
        "max_retries": cfg.max_retries,
    }
    if cfg.provider in {"openai", "openai-compatible", "compatible"}:
        return OpenAICompatibleClient(**common)
    if cfg.provider == "anthropic":
        return AnthropicClient(**common)
    if cfg.provider == "gemini":
        return GeminiClient(**common)
    raise ConfigError(f"unknown reasoning provider: {cfg.provider}")

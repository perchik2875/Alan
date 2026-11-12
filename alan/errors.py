"""Typed error hierarchy.

Errors are deliberately small and explicit so that the bot can translate
failures into short, honest messages instead of crashing the update loop.
"""

from __future__ import annotations


class AlanError(Exception):
    """Base class for all Alan errors."""


class ConfigError(AlanError):
    """Configuration is missing or invalid."""


class LLMError(AlanError):
    """The reasoning provider failed or returned unusable output."""


class LLMTimeout(LLMError):
    """The reasoning provider did not answer in time."""


class LLMOutputInvalid(LLMError):
    """The model produced output that could not be validated."""

    def __init__(self, message: str, *, raw: str = "") -> None:
        super().__init__(message)
        self.raw = raw


class EmbeddingError(AlanError):
    """The embedding provider failed."""


class NotFound(AlanError):
    """A requested record does not exist."""


class ImmutableRecord(AlanError):
    """An attempt was made to mutate a raw capture."""


class PermissionDenied(AlanError):
    """The acting user is not allowed to perform this action."""


class ImportFailed(AlanError):
    """An import could not be completed."""

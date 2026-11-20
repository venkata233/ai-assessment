"""Providers. `create_provider` is the only place that maps a name to a concrete class."""

import os

from copilot.providers.base import CompletionRequest, CompletionResponse, Provider, ProviderError
from copilot.providers.mock import MockProvider
from copilot.providers.openai_compat import OpenAICompatibleProvider

__all__ = [
    "CompletionRequest",
    "CompletionResponse",
    "MockProvider",
    "OpenAICompatibleProvider",
    "Provider",
    "ProviderError",
    "create_provider",
]


def create_provider(name: str) -> Provider:
    """`mock` (default, offline) or `openai` (needs COPILOT_BASE_URL, COPILOT_MODEL; COPILOT_API_KEY optional for local servers)."""
    if name == "mock":
        return MockProvider()
    if name == "openai":
        base_url, model = os.environ.get("COPILOT_BASE_URL"), os.environ.get("COPILOT_MODEL")
        if not base_url or not model:
            raise ProviderError(
                "set COPILOT_BASE_URL and COPILOT_MODEL (and COPILOT_API_KEY for hosted services)"
            )
        return OpenAICompatibleProvider(base_url, model, os.environ.get("COPILOT_API_KEY"))
    raise ProviderError(f"unknown provider {name!r}; choose mock or openai")

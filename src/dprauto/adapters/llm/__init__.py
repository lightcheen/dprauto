"""LLM adapters for API transport and structured repair planning."""

from dprauto.adapters.llm.api import (
    APIModelSettings,
    FailoverLLMClient,
    OpenAICompatibleLLMClient,
)
from dprauto.adapters.llm.repair import LLMRepairPlanner

__all__ = [
    "APIModelSettings",
    "FailoverLLMClient",
    "LLMRepairPlanner",
    "OpenAICompatibleLLMClient",
]

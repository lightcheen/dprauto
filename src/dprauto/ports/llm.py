"""Provider-neutral LLM port and request/response DTOs."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Protocol, runtime_checkable

from dprauto.errors import ModelValidationError


@dataclass(frozen=True, slots=True)
class LLMMessage:
    role: str
    content: str

    def __post_init__(self) -> None:
        if not self.role.strip() or not self.content.strip():
            raise ModelValidationError("LLM message role and content must not be empty")


@dataclass(frozen=True, slots=True)
class LLMRequest:
    messages: tuple[LLMMessage, ...]
    response_schema: Mapping[str, Any] | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    deadline_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.messages:
            raise ModelValidationError("LLM request.messages must not be empty")


@dataclass(frozen=True, slots=True)
class LLMResponse:
    content: str
    structured: Mapping[str, Any] | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    model: str | None = None

    def __post_init__(self) -> None:
        if not self.content and self.structured is None:
            raise ModelValidationError("LLM response must contain text or structured output")
        for name, value in (
            ("input_tokens", self.input_tokens),
            ("output_tokens", self.output_tokens),
        ):
            if value is not None and value < 0:
                raise ModelValidationError(f"LLM response.{name} must not be negative")


@runtime_checkable
class LLMClient(Protocol):
    def complete(self, request: LLMRequest) -> LLMResponse:
        """Complete a provider-neutral request."""
        ...

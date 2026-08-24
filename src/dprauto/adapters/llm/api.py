"""OpenAI-compatible chat-completions client configured by myapi.json."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urlsplit

from dprauto.errors import ConfigurationError, LLMError, LLMTimeoutError
from dprauto.observability.llm_calls import HourlyLLMCallLogger
from dprauto.ports.llm import LLMRequest, LLMResponse
from dprauto.time_budget import clamped_timeout_seconds, time_budget_exhausted


@dataclass(frozen=True, slots=True)
class APIModelSettings:
    api_key: str
    model: str
    base_url: str
    name: str = ""

    @classmethod
    def from_json(cls, path: Path) -> "APIModelSettings":
        """Load a legacy single-model file or the first model in a model pool."""

        return cls.all_from_json(path)[0]

    @classmethod
    def all_from_json(cls, path: Path) -> tuple["APIModelSettings", ...]:
        try:
            payload = json.loads(path.expanduser().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigurationError(f"failed to load API model config {path}: {exc}") from exc
        if not isinstance(payload, Mapping):
            raise ConfigurationError("API model config must be a JSON object")
        if all(key in payload for key in ("api_key", "model", "base_url")):
            return (cls._from_mapping(payload, name=str(payload.get("model", ""))),)
        models: list[APIModelSettings] = []
        for name, candidate in payload.items():
            if not isinstance(name, str) or not name.strip():
                raise ConfigurationError("API model config names must be non-empty strings")
            if not isinstance(candidate, Mapping):
                raise ConfigurationError(f"API model config.{name} must be an object")
            models.append(cls._from_mapping(candidate, name=name.strip()))
        if not models:
            raise ConfigurationError("API model config must contain at least one model")
        return tuple(models)

    @classmethod
    def _from_mapping(
        cls, payload: Mapping[str, Any], *, name: str
    ) -> "APIModelSettings":
        values = {}
        for key in ("api_key", "model", "base_url"):
            value = payload.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ConfigurationError(f"API model config.{key} must be a non-empty string")
            values[key] = value.strip()
        parsed = urlsplit(values["base_url"])
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ConfigurationError("API model config.base_url must be an HTTP(S) URL")
        return cls(**values, name=name)


class OpenAICompatibleLLMClient:
    """Provider-neutral LLMClient using the chat/completions HTTP shape."""

    def __init__(
        self,
        settings: APIModelSettings,
        logger: HourlyLLMCallLogger,
        *,
        temperature: float = 0.0,
        timeout_seconds: int = 120,
        max_output_tokens: int = 4_096,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self.settings = settings
        self.logger = logger
        self.temperature = temperature
        self.timeout_seconds = timeout_seconds
        if max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        self.max_output_tokens = max_output_tokens
        self._opener = opener or urllib.request.urlopen

    def complete(self, request: LLMRequest) -> LLMResponse:
        timeout_seconds = clamped_timeout_seconds(
            self.timeout_seconds,
            request.deadline_at,
        )
        if timeout_seconds <= 0:
            raise LLMTimeoutError(
                "LLM request skipped because workflow time budget is exhausted",
                details={"model": self.settings.model},
            )
        payload = {
            "model": self.settings.model,
            "messages": [
                {"role": message.role, "content": message.content}
                for message in request.messages
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_output_tokens,
            "stream": False,
        }
        started_wall = self.logger.clock()
        started = time.monotonic()
        http_request = urllib.request.Request(
            self.settings.base_url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.settings.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with self._opener(http_request, timeout=timeout_seconds) as response:
                raw = response.read()
            decoded = json.loads(raw.decode("utf-8"))
            result = self._response(decoded, request)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[-4_000:]
            message = f"LLM API returned HTTP {exc.code}: {body}"
            self._log(payload, None, request, started, started_wall, message)
            raise LLMError(message) from exc
        except (OSError, ValueError, KeyError, TypeError) as exc:
            message = f"LLM API request failed: {exc}"
            self._log(payload, None, request, started, started_wall, message)
            if self._is_timeout(exc):
                raise LLMTimeoutError(
                    message,
                    details={"model": self.settings.model, "timeout_seconds": timeout_seconds},
                ) from exc
            raise LLMError(message) from exc
        response_log = {
            "content": result.content,
            "structured": result.structured,
            "model": result.model,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
        }
        self._log(payload, response_log, request, started, started_wall, "")
        return result

    @staticmethod
    def _response(payload: Mapping[str, Any], request: LLMRequest) -> LLMResponse:
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ValueError("response.choices is empty")
        message = choices[0].get("message", {})
        content = message.get("content", "")
        if not isinstance(content, str) or not content:
            raise ValueError("response message contains no text")
        structured = None
        if request.response_schema is not None:
            try:
                candidate = json.loads(content)
                if isinstance(candidate, Mapping):
                    structured = candidate
            except json.JSONDecodeError:
                pass
        usage = payload.get("usage", {})
        return LLMResponse(
            content=content,
            structured=structured,
            input_tokens=OpenAICompatibleLLMClient._optional_int(usage.get("prompt_tokens")),
            output_tokens=OpenAICompatibleLLMClient._optional_int(
                usage.get("completion_tokens")
            ),
            model=str(payload.get("model", "")) or None,
        )

    def _log(self, payload, response, request, started, started_wall, error) -> None:
        self.logger.record(
            request=payload,
            response=response,
            metadata=request.metadata,
            duration_seconds=time.monotonic() - started,
            error=error,
            timestamp=started_wall,
        )

    @staticmethod
    def _optional_int(value: Any) -> int | None:
        return value if isinstance(value, int) and value >= 0 else None

    @staticmethod
    def _is_timeout(exc: BaseException) -> bool:
        current: BaseException | object | None = exc
        visited: set[int] = set()
        while isinstance(current, BaseException) and id(current) not in visited:
            visited.add(id(current))
            if isinstance(current, TimeoutError):
                return True
            reason = getattr(current, "reason", None)
            if isinstance(reason, BaseException):
                current = reason
                continue
            break
        message = str(exc).casefold()
        return "timed out" in message or "timeout" in message


class FailoverLLMClient:
    """Retry one timed-out model, then fail over through the configured model pool."""

    def __init__(
        self,
        clients: Iterable[OpenAICompatibleLLMClient],
        *,
        attempts_per_model: int = 2,
        operation_attempt_limits: Mapping[str, int] | None = None,
    ) -> None:
        self.clients = tuple(clients)
        if not self.clients:
            raise ValueError("failover LLM client requires at least one model")
        if attempts_per_model <= 0:
            raise ValueError("attempts_per_model must be positive")
        self.attempts_per_model = attempts_per_model
        self.operation_attempt_limits = dict(operation_attempt_limits or {"plan_fix": 2})
        for operation, limit in self.operation_attempt_limits.items():
            if not operation.strip():
                raise ValueError("operation attempt limit names must be non-empty")
            if limit <= 0:
                raise ValueError("operation attempt limits must be positive")

    def complete(self, request: LLMRequest) -> LLMResponse:
        timeouts: list[str] = []
        total_attempt = 0
        operation = str(request.metadata.get("operation", "")).strip()
        max_attempts = self.operation_attempt_limits.get(operation)
        for model_index, client in enumerate(self.clients):
            for model_attempt in range(1, self.attempts_per_model + 1):
                if max_attempts is not None and total_attempt >= max_attempts:
                    raise LLMTimeoutError(
                        f"LLM failover stopped after {max_attempts} "
                        f"{operation} timeout attempt(s)",
                        details={
                            "attempts": total_attempt,
                            "operation": operation,
                            "timeouts": tuple(timeouts),
                        },
                    )
                if time_budget_exhausted(request.deadline_at):
                    raise LLMTimeoutError(
                        "LLM failover stopped because workflow time budget is exhausted",
                        details={"attempts": total_attempt, "timeouts": tuple(timeouts)},
                    )
                total_attempt += 1
                metadata = dict(request.metadata)
                metadata.update(
                    {
                        "transport_attempt": total_attempt,
                        "model_attempt": model_attempt,
                        "model_index": model_index,
                        "configured_model": client.settings.model,
                        "configured_model_name": client.settings.name or client.settings.model,
                    }
                )
                attempted = LLMRequest(
                    messages=request.messages,
                    response_schema=request.response_schema,
                    metadata=metadata,
                    deadline_at=request.deadline_at,
                )
                try:
                    return client.complete(attempted)
                except LLMTimeoutError as exc:
                    timeouts.append(
                        f"{client.settings.name or client.settings.model} attempt "
                        f"{model_attempt}: {exc.message}"
                    )
        raise LLMTimeoutError(
            "all configured LLM models timed out after same-model retries",
            details={"attempts": total_attempt, "timeouts": tuple(timeouts)},
        )

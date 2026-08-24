"""Central error codes and exception hierarchy."""

from __future__ import annotations

from enum import Enum
from typing import Any, Mapping


class ErrorCode(str, Enum):
    CONFIGURATION = "configuration_error"
    MODEL_VALIDATION = "model_validation_error"
    PROJECT_PARSING = "project_parsing_error"
    BUILD_PLANNING = "build_planning_error"
    BUILD_EXECUTION = "build_execution_error"
    COMMAND_EXECUTION = "command_execution_error"
    FAILURE_CLASSIFICATION = "failure_classification_error"
    VERIFICATION = "verification_error"
    LLM = "llm_error"
    AGENT_WORKFLOW = "agent_workflow_error"
    TOOL_EXECUTION = "tool_execution_error"
    STORAGE = "storage_error"
    POLICY_VIOLATION = "policy_violation"
    ADAPTER = "adapter_error"


class DPRAutoError(Exception):
    """Base exception carrying a stable machine-readable error code."""

    default_code = ErrorCode.ADAPTER

    def __init__(
        self,
        message: str,
        *,
        code: ErrorCode | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code or self.default_code
        self.details = dict(details or {})

    def __str__(self) -> str:
        return f"[{self.code.value}] {self.message}"


class ConfigurationError(DPRAutoError):
    default_code = ErrorCode.CONFIGURATION


class ModelValidationError(DPRAutoError):
    default_code = ErrorCode.MODEL_VALIDATION


class ProjectParsingError(DPRAutoError):
    default_code = ErrorCode.PROJECT_PARSING


class BuildPlanningError(DPRAutoError):
    default_code = ErrorCode.BUILD_PLANNING


class BuildExecutionError(DPRAutoError):
    default_code = ErrorCode.BUILD_EXECUTION


class CommandExecutionError(DPRAutoError):
    default_code = ErrorCode.COMMAND_EXECUTION


class FailureClassificationError(DPRAutoError):
    default_code = ErrorCode.FAILURE_CLASSIFICATION


class VerificationError(DPRAutoError):
    default_code = ErrorCode.VERIFICATION


class LLMError(DPRAutoError):
    default_code = ErrorCode.LLM


class LLMTimeoutError(LLMError):
    """Retryable LLM transport timeout, kept distinct from semantic/API errors."""


class AgentWorkflowError(DPRAutoError):
    default_code = ErrorCode.AGENT_WORKFLOW


class ToolExecutionError(DPRAutoError):
    default_code = ErrorCode.TOOL_EXECUTION


class StorageError(DPRAutoError):
    default_code = ErrorCode.STORAGE


class PolicyViolationError(DPRAutoError):
    default_code = ErrorCode.POLICY_VIOLATION


class AdapterError(DPRAutoError):
    default_code = ErrorCode.ADAPTER

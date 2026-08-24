"""Strict structured-output adapter from LLMClient to RepairPlanner."""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

from dprauto.agent.models import FixPlan, InvestigationDecision, ToolCall, ToolResult
from dprauto.agent.state import AgentState
from dprauto.agent.context import AgentContextManager
from dprauto.agent.tools.schema import validate_tool_arguments
from dprauto.domain.enums import BuildStage
from dprauto.errors import LLMError, ToolExecutionError
from dprauto.ports.llm import LLMClient, LLMMessage, LLMRequest, LLMResponse
from dprauto.serialization import to_json_bytes


_ANALYSIS_SCHEMA = {
    "type": "object",
    "required": ["diagnosis"],
    "properties": {"diagnosis": {"type": "string"}},
    "additionalProperties": False,
}

_PLAN_SCHEMA = {
    "type": "object",
    "required": ["hypothesis", "actions"],
    "properties": {
        "hypothesis": {"type": "string"},
        "summary": {"type": "string"},
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["tool", "arguments"],
                "properties": {
                    "tool": {"type": "string"},
                    "arguments": {"type": "object"},
                    "rationale": {"type": "string"},
                },
                "additionalProperties": False,
            },
        },
    },
    "additionalProperties": False,
}

_INVESTIGATION_SCHEMA = {
    "type": "object",
    "required": ["complete", "actions", "rationale"],
    "properties": {
        "complete": {"type": "boolean"},
        "rationale": {"type": "string"},
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["tool", "arguments"],
                "properties": {
                    "tool": {"type": "string"},
                    "arguments": {"type": "object"},
                    "rationale": {"type": "string"},
                },
                "additionalProperties": False,
            },
        },
    },
    "additionalProperties": False,
}


class LLMRepairPlanner:
    """Keep provider details behind LLMClient and accept only validated JSON decisions."""

    def __init__(
        self,
        client: LLMClient,
        context_manager: AgentContextManager | None = None,
        *,
        max_actions: int = 6,
        max_investigation_actions_per_round: int = 2,
    ) -> None:
        if max_actions <= 0:
            raise ValueError("max_actions must be positive")
        if max_investigation_actions_per_round <= 0:
            raise ValueError("max_investigation_actions_per_round must be positive")
        self.client = client
        self.context_manager = context_manager or AgentContextManager()
        self.max_actions = max_actions
        self.max_investigation_actions_per_round = max_investigation_actions_per_round

    def plan_investigation(
        self,
        state: AgentState,
        observations: tuple[ToolResult, ...],
        available_tools: Mapping[str, Any],
    ) -> InvestigationDecision:
        """Ask for the minimum additional read-only evidence needed for diagnosis."""

        if not available_tools:
            return InvestigationDecision(complete=True, rationale="no read-only tools available")
        context = self.context_manager.build_llm_context(state, observations)
        payload = {
            "context": context,
            "available_tools": available_tools,
            "policy": {
                "read_only": True,
                "maximum_actions_this_round": self.max_investigation_actions_per_round,
                "prefer": [
                    "files named in the failure",
                    "dependency and build manifests",
                    "CI commands",
                    "README or INSTALL instructions",
                ],
                "forbidden": [
                    "mutating tools",
                    "command execution",
                    "secret or credential files",
                    "repeating an identical observation",
                ],
            },
        }
        encoded = to_json_bytes(payload).decode()
        feedback = ""
        for contract_attempt in range(1, 3):
            user_content = encoded
            if feedback:
                user_content += (
                    "\n\nThe previous investigation decision violated its contract. "
                    f"Correct it and return the full decision again:\n{feedback}"
                )
            response = self.client.complete(
                LLMRequest(
                    messages=(
                        LLMMessage(
                            "system",
                            "Decide whether the supplied evidence is sufficient to diagnose one "
                            "build-environment failure. Use only listed read-only tools. Request "
                            "the minimum additional evidence and never request source mutation, "
                            "command execution, builds, secrets, .env files, keys, or credentials. "
                            "A read_file result with truncated=true is incomplete; use its "
                            "next_start_line and a bounded end_line to request the needed page. "
                            "Return exactly one JSON object shaped as "
                            '{"complete":false,"rationale":"...","actions":['
                            '{"tool":"read_file","arguments":{"path":"pyproject.toml"},'
                            '"rationale":"..."}]}. When evidence is sufficient, return '
                            '{"complete":true,"rationale":"...","actions":[]}.',
                        ),
                        LLMMessage("user", user_content),
                    ),
                    response_schema=_INVESTIGATION_SCHEMA,
                    metadata={
                        "operation": "investigate_failure",
                        "run_id": state["run_id"],
                        "context_characters": len(user_content),
                        "contract_attempt": contract_attempt,
                    },
                    deadline_at=state.get("deadline_at"),
                )
            )
            try:
                return self._investigation_decision(response, available_tools)
            except (LLMError, ToolExecutionError) as exc:
                if contract_attempt == 2:
                    raise LLMError(
                        "LLM investigation violated tool contracts after one correction: "
                        f"{exc}"
                    ) from exc
                feedback = str(exc)
        raise AssertionError("unreachable investigation contract loop")

    def analyze_failure(
        self,
        state: AgentState,
        observations: tuple[ToolResult, ...],
    ) -> str:
        failure = state.get("failure")
        if failure is None:
            raise LLMError("cannot analyze an agent state without FailureInfo")
        context = self.context_manager.build_llm_context(state, observations)
        payload = {
            "context": context,
            "attempt_number": state.get("attempt_number", 0),
        }
        encoded = to_json_bytes(payload).decode()
        response = self.client.complete(
            LLMRequest(
                messages=(
                    LLMMessage(
                        "system",
                        "Analyze one deterministic build failure. Use only the supplied bounded "
                        "evidence. Do not propose source-code changes. Return exactly one JSON "
                        "object with a non-empty string field named diagnosis: "
                        '{"diagnosis":"concise root-cause analysis"}. Do not rename or nest '
                        "the diagnosis field.",
                    ),
                    LLMMessage("user", encoded),
                ),
                response_schema=_ANALYSIS_SCHEMA,
                metadata={
                    "operation": "analyze_failure",
                    "run_id": state["run_id"],
                    "context_characters": len(encoded),
                },
                deadline_at=state.get("deadline_at"),
            )
        )
        parsed = self._object(response)
        diagnosis = self._diagnosis(parsed)
        if not diagnosis:
            raise LLMError("LLM failure analysis did not contain diagnosis")
        return diagnosis

    def plan_fix(
        self,
        state: AgentState,
        diagnosis: str,
        available_tools: Mapping[str, Any],
    ) -> FixPlan:
        context = self.context_manager.build_llm_context(
            state,
            state.get("tool_results", ()),
        )
        payload = {
            "diagnosis": diagnosis,
            "context": context,
            "available_tools": available_tools,
            "policy": self._repair_policy(state),
        }
        encoded = to_json_bytes(payload).decode()
        feedback = ""
        for contract_attempt in range(1, 3):
            user_content = encoded
            if feedback:
                user_content += (
                    "\n\nYour previous plan was rejected before execution. Correct every "
                    f"contract violation and return the full plan again:\n{feedback}"
                )
            response = self.client.complete(
                LLMRequest(
                    messages=(
                        LLMMessage(
                            "system",
                            "Create the smallest build-environment repair plan. Select only listed "
                            "tools, obey each tool's argument_schema exactly (including required "
                            "fields and additionalProperties), prefer Dockerfile/setup.sh, never "
                            "edit business source, and return exactly one JSON object with this "
                            "shape: "
                            '{"hypothesis":"...","summary":"...","actions":['
                            '{"tool":"one listed tool","arguments":{},"rationale":"..."}]}. '
                            "Do not rename or nest hypothesis or actions. Prefer the structured "
                            "patch_system_packages, patch_python_dependencies, "
                            "patch_verification_dependencies, or patch_base_image "
                            "tool whenever one expresses the repair. Change only one high-risk "
                            "environment dimension per plan. Use modify_build_script only when no "
                            "structured patch can express the fix; its arguments.content must be "
                            "the complete non-empty replacement file, never a diff. A repair "
                            "must change the build "
                            "environment before rebuilding. Observation evidence has already been "
                            "provided in the context; do not plan read, search, log, command-run, "
                            "or build tools.",
                        ),
                        LLMMessage("user", user_content),
                    ),
                    response_schema=_PLAN_SCHEMA,
                    metadata={
                        "operation": "plan_fix",
                        "run_id": state["run_id"],
                        "context_characters": len(user_content),
                        "contract_attempt": contract_attempt,
                    },
                    deadline_at=state.get("deadline_at"),
                )
            )
            try:
                return self._fix_plan(response, diagnosis, available_tools)
            except (LLMError, ToolExecutionError) as exc:
                if contract_attempt == 2:
                    raise LLMError(
                        f"LLM fix plan violated tool contracts after one correction: {exc}"
                    ) from exc
                feedback = str(exc)
        raise AssertionError("unreachable plan contract loop")

    def _fix_plan(
        self,
        response: LLMResponse,
        diagnosis: str,
        available_tools: Mapping[str, Any],
    ) -> FixPlan:
        parsed = self._object(response)
        raw_actions = parsed.get("actions")
        if not isinstance(raw_actions, list) or not raw_actions:
            raise LLMError("LLM fix plan must contain a non-empty actions list")
        if len(raw_actions) > self.max_actions:
            raise LLMError(f"LLM fix plan exceeds maximum of {self.max_actions} actions")

        actions: list[ToolCall] = []
        for raw in raw_actions:
            if not isinstance(raw, Mapping):
                raise LLMError("LLM fix plan action must be an object")
            tool = str(raw.get("tool", "")).strip()
            if tool not in available_tools:
                raise LLMError(f"LLM selected unavailable tool: {tool or '<empty>'}")
            arguments = raw.get("arguments", {})
            if not isinstance(arguments, Mapping):
                raise LLMError(f"tool arguments for {tool} must be an object")
            specification = available_tools[tool]
            if isinstance(specification, Mapping):
                schema = specification.get("argument_schema")
                if isinstance(schema, Mapping):
                    validate_tool_arguments(tool, arguments, schema)
            actions.append(
                ToolCall(
                    tool=tool,
                    arguments=dict(arguments),
                    rationale=str(raw.get("rationale", "")).strip(),
                )
            )
        hypothesis = str(parsed.get("hypothesis", diagnosis)).strip()
        return FixPlan(
            hypothesis=hypothesis,
            actions=tuple(actions),
            summary=str(parsed.get("summary", "")).strip(),
        )

    def _investigation_decision(
        self,
        response: LLMResponse,
        available_tools: Mapping[str, Any],
    ) -> InvestigationDecision:
        parsed = self._object(response)
        complete = parsed.get("complete")
        if not isinstance(complete, bool):
            raise LLMError("LLM investigation.complete must be a boolean")
        raw_actions = parsed.get("actions")
        if not isinstance(raw_actions, list):
            raise LLMError("LLM investigation.actions must be a list")
        if complete and raw_actions:
            raise LLMError("completed investigation must not request tool actions")
        if not complete and not raw_actions:
            raise LLMError("incomplete investigation must request at least one tool action")
        if len(raw_actions) > self.max_investigation_actions_per_round:
            raise LLMError(
                "LLM investigation exceeds maximum of "
                f"{self.max_investigation_actions_per_round} actions per round"
            )
        actions: list[ToolCall] = []
        for raw in raw_actions:
            if not isinstance(raw, Mapping):
                raise LLMError("LLM investigation action must be an object")
            tool = str(raw.get("tool", "")).strip()
            if tool not in available_tools:
                raise LLMError(
                    f"LLM selected unavailable investigation tool: {tool or '<empty>'}"
                )
            arguments = raw.get("arguments", {})
            if not isinstance(arguments, Mapping):
                raise LLMError(f"investigation arguments for {tool} must be an object")
            specification = available_tools[tool]
            if isinstance(specification, Mapping):
                schema = specification.get("argument_schema")
                if isinstance(schema, Mapping):
                    validate_tool_arguments(tool, arguments, schema)
            actions.append(
                ToolCall(
                    tool,
                    dict(arguments),
                    str(raw.get("rationale", "")).strip(),
                )
            )
        return InvestigationDecision(
            actions=tuple(actions),
            complete=complete,
            rationale=str(parsed.get("rationale", "")).strip(),
        )

    @staticmethod
    def _object(response: LLMResponse) -> Mapping[str, Any]:
        if response.structured is not None:
            return response.structured
        content = response.content.strip()
        fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", content, re.DOTALL | re.IGNORECASE)
        if fenced:
            content = fenced.group(1)
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMError(f"LLM returned invalid JSON: {exc.msg}") from exc
        if not isinstance(parsed, Mapping):
            raise LLMError("LLM response must be a JSON object")
        return parsed

    @staticmethod
    def _diagnosis(parsed: Mapping[str, Any]) -> str:
        """Normalize common provider-safe analysis shapes without accepting actions."""

        direct = parsed.get("diagnosis")
        if isinstance(direct, str) and direct.strip():
            return direct.strip()
        candidates: list[Any] = [
            parsed.get("root_cause"),
            parsed.get("analysis"),
            parsed.get("failure_analysis"),
            parsed.get("possible_cause"),
            parsed.get("explanation"),
        ]
        for candidate in candidates:
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
            if isinstance(candidate, Mapping):
                values = [
                    candidate.get(name)
                    for name in (
                        "diagnosis",
                        "root_cause",
                        "cause",
                        "summary",
                        "analysis",
                        "details",
                        "explanation",
                    )
                ]
                selected = [
                    value.strip()
                    for value in values
                    if isinstance(value, str) and value.strip()
                ]
                if selected:
                    return "\n".join(dict.fromkeys(selected))
        return ""

    def _repair_policy(self, state: AgentState) -> Mapping[str, Any]:
        policy: dict[str, Any] = {
            "source_changes_allowed": False,
            "preferred_files": ["Dockerfile", "setup.sh"],
            "maximum_actions": self.max_actions,
            "must_change_build_environment": True,
            "structured_patch_preference": {
                "system_packages": "patch_system_packages",
                "python_dependencies": "patch_python_dependencies",
                "base_image": "patch_base_image",
                "whole_file_fallback": "modify_build_script",
                "maximum_high_risk_dimensions_per_round": 1,
            },
        }
        failure = state.get("failure")
        if failure is not None and failure.failure_stage is BuildStage.TEST:
            policy["preferred_files"] = [
                ".dprauto/requirements-verification.txt"
            ]
            policy["verification_repair"] = {
                "dependency_tool": "patch_verification_dependencies",
                "scope": "temporary Testability container only",
                "runtime_image_must_remain_unchanged": True,
                "allowed_only_for": [
                    "missing test-only Python module",
                    "missing test plugin",
                    "test-runner dependency conflict with direct causal evidence",
                ],
            }
            policy["structured_patch_preference"] = {
                **policy["structured_patch_preference"],
                "test_python_dependencies": "patch_verification_dependencies",
            }
        if (
            failure is not None
            and "timed out" in failure.message.casefold()
            and failure.failure_stage in {BuildStage.BUILD, BuildStage.DEPENDENCY_INSTALLATION}
        ):
            policy["timeout_repair"] = {
                "allowed_direction": "reduce build/install cost or narrow execution scope only",
                "forbidden_without_causal_evidence": [
                    "adding Python dependencies",
                    "adding system packages",
                    "changing dependency versions",
                    "changing Python version",
                    "changing base image",
                    "running full tox/nox/docs/lint matrices",
                ],
                "preferred_actions": [
                    "remove optional docs/dev/lint/test extras not needed by the "
                    "selected verification command",
                    "use the existing runtime/base dependencies already declared by the project",
                    "preserve base image and Python version unless the log explicitly "
                    "proves incompatibility",
                    "avoid repeating a method with the same timeout profile",
                ],
            }
        return policy

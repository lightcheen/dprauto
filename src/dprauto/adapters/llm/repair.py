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
    "required": ["diagnosis", "claims"],
    "properties": {
        "diagnosis": {"type": "string", "minLength": 1},
        "claims": {
            "type": "array",
            "minItems": 1,
            "maxItems": 8,
            "items": {
                "type": "object",
                "required": ["claim", "evidence_refs", "counterevidence_refs"],
                "properties": {
                    "claim": {"type": "string", "minLength": 1},
                    "evidence_refs": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string", "minLength": 1},
                    },
                    "counterevidence_refs": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                    },
                },
                "additionalProperties": False,
            },
        },
    },
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
            "evidence_reference_catalog": self._evidence_reference_catalog(context),
        }
        encoded = to_json_bytes(payload).decode()
        allowed_refs = {
            str(item["ref"])
            for item in payload["evidence_reference_catalog"]
        }
        feedback = ""
        for contract_attempt in range(1, 3):
            user_content = encoded
            if feedback:
                user_content += (
                    "\n\nThe previous diagnosis violated its evidence contract. "
                    "Correct it and return the full diagnosis again:\n"
                    + feedback
                )
            response = self.client.complete(
                LLMRequest(
                    messages=(
                        LLMMessage(
                            "system",
                            "Analyze one deterministic build failure using only supplied bounded "
                            "evidence. Do not propose source-code changes. Return exactly one JSON "
                            "object shaped as "
                            '{"diagnosis":"concise root cause","claims":['
                            '{"claim":"one factual claim","evidence_refs":['
                            '"failure:key_log"],"counterevidence_refs":[]}]}. '
                            "Every claim must cite one or more exact refs from "
                            "evidence_reference_catalog. List relevant contrary evidence in "
                            "counterevidence_refs; use an empty list only when none is present. "
                            "Do not invent paths, observations, log facts, dependencies, or refs.",
                        ),
                        LLMMessage("user", user_content),
                    ),
                    response_schema=_ANALYSIS_SCHEMA,
                    metadata={
                        "operation": "analyze_failure",
                        "run_id": state["run_id"],
                        "context_characters": len(user_content),
                        "contract_attempt": contract_attempt,
                    },
                    deadline_at=state.get("deadline_at"),
                )
            )
            try:
                return self._evidence_backed_diagnosis(response, allowed_refs)
            except LLMError as exc:
                if contract_attempt == 2:
                    raise LLMError(
                        "LLM failure analysis violated its evidence contract after one "
                        f"correction: {exc}"
                    ) from exc
                feedback = str(exc)
        raise AssertionError("unreachable analysis contract loop")

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
            "repair_search_space": state.get("repair_search_space", {}),
            "plan_feedback": state.get("plan_feedback", ()),
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
                            "or build tools. Treat plan_feedback as binding rejection evidence: "
                            "do not repeat the rejected tool, arguments, or repair method.",
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

    def _evidence_backed_diagnosis(
        self,
        response: LLMResponse,
        allowed_refs: set[str],
    ) -> str:
        parsed = self._object(response)
        diagnosis = parsed.get("diagnosis")
        if not isinstance(diagnosis, str) or not diagnosis.strip():
            raise LLMError("LLM failure analysis must contain a non-empty diagnosis")
        raw_claims = parsed.get("claims")
        if not isinstance(raw_claims, list) or not 1 <= len(raw_claims) <= 8:
            raise LLMError("LLM failure analysis must contain between 1 and 8 claims")
        rendered: list[str] = [diagnosis.strip(), "Evidence-backed claims:"]
        for index, raw_claim in enumerate(raw_claims, start=1):
            if not isinstance(raw_claim, Mapping):
                raise LLMError(f"analysis claim {index} must be an object")
            claim = raw_claim.get("claim")
            if not isinstance(claim, str) or not claim.strip():
                raise LLMError(f"analysis claim {index} must be non-empty")
            evidence_refs = raw_claim.get("evidence_refs")
            counter_refs = raw_claim.get("counterevidence_refs")
            if not isinstance(evidence_refs, list) or not evidence_refs or any(
                not isinstance(item, str) or not item.strip() for item in evidence_refs
            ):
                raise LLMError(
                    f"analysis claim {index} must cite at least one evidence ref"
                )
            if not isinstance(counter_refs, list) or any(
                not isinstance(item, str) or not item.strip() for item in counter_refs
            ):
                raise LLMError(
                    f"analysis claim {index} counterevidence_refs must be a list"
                )
            cited = tuple(dict.fromkeys((*evidence_refs, *counter_refs)))
            unknown = tuple(ref for ref in cited if ref not in allowed_refs)
            if unknown:
                raise LLMError(
                    f"analysis claim {index} cited unknown evidence ref(s): "
                    + ", ".join(unknown)
                )
            support = ", ".join(dict.fromkeys(evidence_refs))
            contrary = ", ".join(dict.fromkeys(counter_refs)) or "none"
            rendered.append(
                f"{index}. {claim.strip()} [evidence: {support}; contrary: {contrary}]"
            )
        return "\n".join(rendered)

    @staticmethod
    def _evidence_reference_catalog(
        context: Mapping[str, Any],
    ) -> tuple[dict[str, str], ...]:
        catalog: list[dict[str, str]] = [
            {"ref": "failure:summary", "description": "normalized failure fields"}
        ]
        failure = context.get("current_failure")
        if isinstance(failure, Mapping):
            if failure.get("key_log"):
                catalog.append(
                    {"ref": "failure:key_log", "description": "bounded failure log"}
                )
            evidence = failure.get("evidence")
            if isinstance(evidence, (list, tuple)):
                catalog.extend(
                    {
                        "ref": f"failure:evidence:{index}",
                        "description": str(value)[:200],
                    }
                    for index, value in enumerate(evidence)
                    if isinstance(value, str) and value
                )
        for record in context.get("evidence", ()):
            if not isinstance(record, Mapping):
                continue
            ref = record.get("ref")
            if not isinstance(ref, str) or not ref:
                continue
            data = record.get("data")
            path = data.get("path") if isinstance(data, Mapping) else ""
            description = f"{record.get('tool', 'observation')}: {path or record.get('summary', '')}"
            catalog.append({"ref": ref, "description": description[:200]})
            if isinstance(path, str) and path:
                catalog.append(
                    {"ref": f"path:{path}", "description": f"observed file {path}"}
                )
        for script in context.get("current_build_scripts", ()):
            if not isinstance(script, Mapping):
                continue
            path = script.get("path")
            if isinstance(path, str) and path:
                catalog.append(
                    {"ref": f"path:{path}", "description": f"build script {path}"}
                )
        profile = context.get("project_profile")
        if profile is not None:
            catalog.extend(
                (
                    {
                        "ref": "profile:dependency_names",
                        "description": "deterministically parsed project dependencies",
                    },
                    {
                        "ref": "profile:commands",
                        "description": "deterministically extracted project commands",
                    },
                    {
                        "ref": "profile:build_files",
                        "description": "deterministically identified build files",
                    },
                )
            )
        deduplicated = {item["ref"]: item for item in catalog}
        return tuple(deduplicated.values())

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

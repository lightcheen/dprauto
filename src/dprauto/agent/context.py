"""Build bounded LLM context and compact completed repair rounds."""

from __future__ import annotations

import hashlib
from dataclasses import asdict
from pathlib import PurePosixPath
from typing import Any

from dprauto.agent.models import (
    AttemptedMethod,
    ContextSummary,
    EvidencePack,
    EvidenceRecord,
    FixPlan,
    ToolResult,
)
from dprauto.agent.state import AgentState
from dprauto.config import AgentConfig
from dprauto.domain.models import EnvironmentDiff, FailureInfo, ProjectProfile
from dprauto.errors import AgentWorkflowError
from dprauto.serialization import to_json_bytes


def method_fingerprint(plan: FixPlan) -> str:
    """Stable identity used to prevent re-executing an already failed method."""

    digest = hashlib.sha256(to_json_bytes(plan.actions)).hexdigest()[:20]
    return f"fix:{digest}"


class AgentContextManager:
    """Separate active prompt memory from complete external repair history."""

    def __init__(self, config: AgentConfig | None = None) -> None:
        self.config = config or AgentConfig()

    def build_evidence_pack(
        self,
        observations: tuple[ToolResult, ...],
        *,
        rounds: int,
        action_count: int,
        completed: bool,
        stop_reason: str,
    ) -> EvidencePack:
        records = tuple(
            EvidenceRecord(
                tool=str(record["tool"]),
                summary=str(record["summary"]),
                data=dict(record["data"]),
            )
            for record in self._evidence(
                observations,
                self.config.max_evidence_characters,
            )
        )
        return EvidencePack(
            records=records,
            rounds=rounds,
            action_count=action_count,
            completed=completed,
            stop_reason=stop_reason,
        )

    def build_llm_context(
        self,
        state: AgentState,
        observations: tuple[ToolResult, ...],
    ) -> dict[str, Any]:
        summary = state.get("context_summary", ContextSummary())
        payload: dict[str, Any] = {
            "project_profile": state.get("project_profile"),
            "current_build_scripts": self._build_scripts(state, observations, 8_000),
            "evidence": self._evidence(observations, self.config.max_evidence_characters),
            "resolved_issues": summary.resolved_issues,
            "current_failure": self._failure(state.get("failure"), key_log_limit=3_000),
            "recent_modifications": summary.recent_modifications,
            "failed_methods": summary.failed_methods,
            "context_summary": summary.narrative[-4_000:],
        }
        if self._size(payload) <= self.config.max_context_characters:
            return payload

        profile = state.get("project_profile")
        payload["project_profile"] = self._compact_profile(profile) if profile else None
        payload["current_build_scripts"] = self._build_scripts(state, observations, 2_000)
        payload["evidence"] = self._evidence(observations, 3_000)
        payload["current_failure"] = self._failure(
            state.get("failure"), key_log_limit=1_000
        )
        payload["context_summary"] = summary.narrative[-1_500:]
        if self._size(payload) <= self.config.max_context_characters:
            return payload

        payload["current_build_scripts"] = self._build_scripts(state, observations, 500)
        payload["evidence"] = self._evidence(observations, 800)
        payload["current_failure"] = self._failure(
            state.get("failure"), key_log_limit=300
        )
        payload["resolved_issues"] = summary.resolved_issues[-2:]
        payload["recent_modifications"] = summary.recent_modifications[-2:]
        payload["failed_methods"] = summary.failed_methods[-2:]
        payload["context_summary"] = summary.narrative[-500:]
        if self._size(payload) > self.config.max_context_characters:
            raise AgentWorkflowError(
                "minimal LLM context exceeds configured max_context_characters"
            )
        return payload

    def after_attempt(
        self,
        previous: ContextSummary,
        *,
        previous_failure: FailureInfo | None,
        current_failure: FailureInfo | None,
        plan: FixPlan,
        fingerprint: str,
        outcome: str,
        environment_diff: EnvironmentDiff | None,
    ) -> ContextSummary:
        resolved = list(previous.resolved_issues)
        if previous_failure and (
            current_failure is None
            or previous_failure.fingerprint != current_failure.fingerprint
        ):
            resolved.append(
                f"{previous_failure.fingerprint}: {previous_failure.message}"
            )
        modifications = list(previous.recent_modifications)
        if environment_diff and environment_diff.summary:
            modifications.append(environment_diff.summary)
        failed = list(previous.failed_methods)
        if outcome != "succeeded":
            failed.append(
                AttemptedMethod(
                    fingerprint=fingerprint,
                    hypothesis=plan.hypothesis,
                    outcome=outcome,
                    failure_fingerprint=(
                        current_failure.fingerprint if current_failure else ""
                    ),
                )
            )
        resolved = resolved[-self.config.max_resolved_issues :]
        modifications = modifications[-self.config.max_recent_modifications :]
        failed = failed[-self.config.max_failed_methods :]
        narrative = self._narrative(resolved, modifications, failed, current_failure)
        return ContextSummary(
            resolved_issues=tuple(resolved),
            recent_modifications=tuple(modifications),
            failed_methods=tuple(failed),
            narrative=narrative,
        )

    def merge_persisted_methods(
        self,
        summary: ContextSummary,
        methods: tuple[AttemptedMethod, ...],
    ) -> ContextSummary:
        indexed = {item.fingerprint: item for item in (*summary.failed_methods, *methods)}
        failed = tuple(indexed.values())[-self.config.max_failed_methods :]
        return ContextSummary(
            resolved_issues=summary.resolved_issues[-self.config.max_resolved_issues :],
            recent_modifications=summary.recent_modifications[
                -self.config.max_recent_modifications :
            ],
            failed_methods=failed,
            narrative=summary.narrative,
        )

    @staticmethod
    def _evidence(
        observations: tuple[ToolResult, ...],
        character_limit: int,
    ) -> tuple[dict[str, Any], ...]:
        """Retain bounded read/search evidence instead of only generated scripts."""

        records: list[dict[str, Any]] = []
        used = 0
        for observation in observations:
            remaining = character_limit - used
            if remaining <= 100:
                break
            data: dict[str, Any] = {}
            for key in (
                "path",
                "query",
                "content",
                "matches",
                "files",
                "truncated",
                "skipped_files",
            ):
                if key not in observation.data:
                    continue
                value = observation.data[key]
                if isinstance(value, str):
                    data[key] = value[: min(len(value), max(100, remaining // 2))]
                elif isinstance(value, (list, tuple)):
                    data[key] = tuple(value[:100])
                elif isinstance(value, (bool, int, float)) or value is None:
                    data[key] = value
            record = {
                "tool": observation.tool,
                "succeeded": observation.succeeded,
                "summary": observation.summary,
                "data": data,
            }
            encoded = to_json_bytes(record).decode()
            if len(encoded) > remaining:
                record["data"] = {"excerpt": encoded[: max(0, remaining - 200)]}
                encoded = to_json_bytes(record).decode()
            if len(encoded) > remaining:
                break
            records.append(record)
            used += len(encoded)
        return tuple(records)

    @staticmethod
    def _build_scripts(
        state: AgentState,
        observations: tuple[ToolResult, ...],
        character_limit: int,
    ) -> tuple[dict[str, str], ...]:
        scripts: dict[str, str] = {}
        for observation in observations:
            path = observation.data.get("path")
            content = observation.data.get("content")
            if isinstance(path, str) and isinstance(content, str):
                scripts[path] = content[-character_limit:]
        plan = state.get("build_plan")
        if plan:
            for generated in plan.generated_files:
                name = PurePosixPath(generated.path).name.lower()
                if name == "setup.sh" or name.startswith("dockerfile"):
                    scripts.setdefault(generated.path, generated.content[-character_limit:])
        return tuple(
            {"path": path, "content": content}
            for path, content in sorted(scripts.items())
        )

    @staticmethod
    def _failure(
        failure: FailureInfo | None,
        *,
        key_log_limit: int,
    ) -> dict[str, Any] | None:
        if failure is None:
            return None
        return {
            "category": failure.category.value,
            "failure_stage": failure.failure_stage.value,
            "message": failure.message,
            "fingerprint": failure.fingerprint,
            "failed_command": failure.failed_command.display if failure.failed_command else "",
            "key_log": failure.key_log[-key_log_limit:],
            "environment": dict(failure.environment),
            "possible_cause": failure.possible_cause,
            "evidence": failure.evidence,
            "suggestions": failure.suggestions,
        }

    @staticmethod
    def _compact_profile(profile: ProjectProfile) -> dict[str, Any]:
        return {
            "project_id": profile.project_id,
            "source": asdict(profile.source),
            "languages": profile.languages,
            "project_type": profile.project_type.value,
            "runtime_constraints": dict(profile.runtime_constraints),
            "package_managers": profile.package_managers,
            "dependency_files": profile.dependency_files,
            "build_files": profile.build_files,
            "dockerfiles": profile.dockerfiles,
            "commands": tuple(
                {
                    "name": item.name,
                    "command": item.command.display,
                    "source": item.source,
                }
                for item in profile.commands[:20]
            ),
        }

    @staticmethod
    def _narrative(resolved, modifications, failed, current_failure) -> str:
        parts = []
        if resolved:
            parts.append("Resolved: " + " | ".join(resolved[-3:]))
        if modifications:
            parts.append("Recent modifications: " + " | ".join(modifications[-3:]))
        if failed:
            parts.append(
                "Failed methods: "
                + " | ".join(f"{item.fingerprint} {item.hypothesis}" for item in failed[-3:])
            )
        if current_failure:
            parts.append(
                f"Current failure: {current_failure.fingerprint} {current_failure.message}"
            )
        return "\n".join(parts)[-4_000:] or "No repair attempts have completed yet."

    @staticmethod
    def _size(payload: dict[str, Any]) -> int:
        return len(to_json_bytes(payload).decode("utf-8"))

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

        selected = tuple(enumerate(observations))[-12:]
        per_record_limit = max(400, character_limit // max(1, min(len(selected), 8)))
        records: list[dict[str, Any]] = []
        used = 0
        for observation_index, observation in reversed(selected):
            remaining = character_limit - used
            if remaining <= 100:
                break
            record_limit = min(remaining, per_record_limit)
            data: dict[str, Any] = {}
            for key in (
                "path",
                "query",
                "graph_id",
                "session_id",
                "turn",
                "content",
                "context",
                "matches",
                "files",
                "truncated",
                "skipped_files",
                "start_line",
                "end_line",
                "total_lines",
                "next_start_line",
                "new_context_characters",
                "seen_node_count",
                "graph_truncated",
                "scan_truncated",
            ):
                if key not in observation.data:
                    continue
                value = observation.data[key]
                if isinstance(value, str):
                    data[key] = AgentContextManager._bounded_text(
                        value,
                        max(100, record_limit - 300),
                    )
                elif isinstance(value, (list, tuple)):
                    data[key] = tuple(value[:100])
                elif isinstance(value, (bool, int, float)) or value is None:
                    data[key] = value
            record = {
                "ref": f"observation:{observation_index}",
                "tool": observation.tool,
                "succeeded": observation.succeeded,
                "summary": observation.summary,
                "data": data,
            }
            record = AgentContextManager._fit_evidence_record(record, record_limit)
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
        profile = state.get("project_profile")
        allowed_paths = {".dprauto/requirements-verification.txt"}
        if profile:
            allowed_paths.update(profile.build_files)
            allowed_paths.update(profile.dockerfiles)
        plan = state.get("build_plan")
        if plan:
            allowed_paths.update(generated.path for generated in plan.generated_files)
        for observation in observations:
            path = observation.data.get("path")
            content = observation.data.get("content")
            if (
                isinstance(path, str)
                and path in allowed_paths
                and isinstance(content, str)
            ):
                scripts[path] = AgentContextManager._bounded_text(
                    content,
                    character_limit,
                )
        if plan:
            for generated in plan.generated_files:
                name = PurePosixPath(generated.path).name.lower()
                if name == "setup.sh" or name.startswith("dockerfile"):
                    scripts.setdefault(
                        generated.path,
                        AgentContextManager._bounded_text(
                            generated.content,
                            character_limit,
                        ),
                    )
        return tuple(
            {"path": path, "content": content}
            for path, content in sorted(scripts.items())
        )

    @staticmethod
    def _bounded_text(value: str, character_limit: int) -> str:
        if len(value) <= character_limit:
            return value
        if character_limit <= 0:
            return ""
        marker = f"\n... <{len(value) - character_limit} characters omitted> ...\n"
        if len(marker) >= character_limit:
            return value[:character_limit]
        available = max(0, character_limit - len(marker))
        head = (available * 2) // 3
        tail = available - head
        return value[:head] + marker + (value[-tail:] if tail else "")

    @staticmethod
    def _fit_evidence_record(
        record: dict[str, Any],
        character_limit: int,
    ) -> dict[str, Any]:
        if len(to_json_bytes(record).decode()) <= character_limit:
            return record
        data = dict(record["data"])
        text_key = next(
            (
                key
                for key in ("content", "context")
                if isinstance(data.get(key), str)
            ),
            None,
        )
        content = data.get(text_key) if text_key is not None else None
        if isinstance(content, str) and text_key is not None:
            lower = 0
            upper = len(content)
            best = ""
            while lower <= upper:
                middle = (lower + upper) // 2
                candidate_data = dict(data)
                candidate_data[text_key] = AgentContextManager._bounded_text(
                    content,
                    middle,
                )
                candidate = {**record, "data": candidate_data}
                if len(to_json_bytes(candidate).decode()) <= character_limit:
                    best = candidate_data[text_key]
                    lower = middle + 1
                else:
                    upper = middle - 1
            data[text_key] = best
            fitted = {**record, "data": data}
            if len(to_json_bytes(fitted).decode()) <= character_limit:
                return fitted
        scalar_data = {
            key: value
            for key, value in data.items()
            if key not in {"content", "context"}
            and isinstance(value, (str, bool, int, float, type(None)))
        }
        return {
            **record,
            "summary": AgentContextManager._bounded_text(
                str(record["summary"]),
                200,
            ),
            "data": scalar_data,
        }

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

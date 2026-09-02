"""Application service for deterministic build planning, execution and classification."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from dprauto.adapters.execution import SubprocessCommandExecutor
from dprauto.adapters.failure import FailureClassifierChain, RuleBasedBuildFailureClassifier
from dprauto.config import BuildConfig
from dprauto.domain.enums import BuildFailureKind, BuildStatus, FailureCategory
from dprauto.domain.models import (
    ArtifactRef,
    BuildPlan,
    BuildResult,
    FailureInfo,
    ProjectProfile,
)
from dprauto.ports.build import BuildStrategy
from dprauto.ports.failure import FailureClassifier
from dprauto.ports.storage import Storage
from dprauto.serialization import to_json_bytes
from dprauto.strategies import (
    CNBStrategy,
    DockerStrategy,
    JVMTemplateStrategy,
    NativeTemplateStrategy,
    RecordedBuildRunner,
    StrategyRegistry,
    TemplateStrategy,
)
from dprauto.time_budget import time_budget_exhausted


@dataclass(frozen=True, slots=True)
class BuildStrategyAttempt:
    """One fully classified deterministic strategy attempt."""

    plan: BuildPlan
    result: BuildResult
    failure: FailureInfo | None


@dataclass(frozen=True, slots=True)
class BuildExecution:
    plan: BuildPlan
    result: BuildResult
    failure: FailureInfo | None
    attempts: tuple[BuildStrategyAttempt, ...] = ()
    selection_reason: str = ""
    artifacts: tuple[ArtifactRef, ...] = ()


class DeterministicBuildService:
    def __init__(
        self,
        registry: StrategyRegistry,
        failure_classifier: FailureClassifier,
        *,
        config: BuildConfig | None = None,
        storage: Storage | None = None,
    ) -> None:
        self.registry = registry
        self.failure_classifier = failure_classifier
        self.config = config or BuildConfig()
        self.storage = storage

    def plan(self, profile: ProjectProfile) -> tuple[BuildPlan, ...]:
        """Create the bounded production strategy portfolio without executing it.

        Callers that need build plans must use this application entry point instead
        of selecting language strategies themselves. This keeps registry priority,
        optional-tool availability, portfolio policy, and attempt bounds identical
        to an actual build.
        """

        return tuple(
            strategy.create_plan(profile)
            for strategy in self._candidate_strategies(profile)
        )

    def _candidate_strategies(
        self, profile: ProjectProfile
    ) -> tuple[BuildStrategy, ...]:
        strategies = (
            self.registry.candidates(profile)
            if self.config.strategy_portfolio_enabled
            else (self.registry.select(profile),)
        )
        return strategies[: self.config.max_strategy_attempts]

    def build(
        self,
        profile: ProjectProfile,
        workspace: Path,
        *,
        deadline_at: datetime | None = None,
    ) -> BuildExecution:
        strategies = self._candidate_strategies(profile)
        attempts: list[BuildStrategyAttempt] = []
        selection_reason = "all compatible strategies failed"
        terminal_attempt: BuildStrategyAttempt | None = None

        for index, strategy in enumerate(strategies):
            if index and time_budget_exhausted(deadline_at):
                selection_reason = "shared build deadline exhausted before fallback"
                break
            plan = strategy.create_plan(profile)
            result = strategy.build(plan, workspace, deadline_at=deadline_at)
            failure = self.failure_classifier.classify(profile, plan, result)
            attempts.append(BuildStrategyAttempt(plan, result, failure))

            if result.status is BuildStatus.SUCCEEDED and failure is None:
                selection_reason = f"strategy {plan.strategy} succeeded"
                terminal_attempt = attempts[-1]
                break
            if result.status is BuildStatus.TIMED_OUT:
                selection_reason = f"strategy {plan.strategy} exhausted its build budget"
                terminal_attempt = attempts[-1]
                break
            if result.status is BuildStatus.CANCELLED:
                selection_reason = f"strategy {plan.strategy} was cancelled"
                terminal_attempt = attempts[-1]
                break
            if failure is None:
                selection_reason = (
                    f"strategy {plan.strategy} failed without classified fallback evidence"
                )
                terminal_attempt = attempts[-1]
                break
            if failure.infrastructure_related or failure.kind in {
                BuildFailureKind.NETWORK,
                BuildFailureKind.DOCKER_INFRASTRUCTURE,
            }:
                selection_reason = (
                    f"strategy {plan.strategy} hit infrastructure failure; fallback stopped"
                )
                terminal_attempt = attempts[-1]
                break
        else:
            if len(strategies) >= self.config.max_strategy_attempts:
                selection_reason = (
                    f"maximum strategy attempts reached: {self.config.max_strategy_attempts}"
                )

        selected = terminal_attempt or self._best_repair_baseline(attempts)
        if terminal_attempt is None and selected is not attempts[-1]:
            selection_reason = (
                f"{selection_reason}; selected {selected.plan.strategy} as repair baseline"
            )
        artifacts = self._record_selection(profile, attempts, selected, selection_reason)
        return BuildExecution(
            selected.plan,
            selected.result,
            selected.failure,
            tuple(attempts),
            selection_reason,
            artifacts,
        )

    @staticmethod
    def _best_repair_baseline(
        attempts: list[BuildStrategyAttempt],
    ) -> BuildStrategyAttempt:
        """Prefer a specific failure attached to files the Agent can repair."""

        def score(item: BuildStrategyAttempt) -> tuple[int, int, float, int]:
            failure = item.failure
            repair_surface = bool(
                item.plan.generated_files
                or item.plan.source_files
                or item.plan.metadata.get("dockerfile")
                or item.plan.metadata.get("setup_script")
            )
            known = bool(failure and failure.category is not FailureCategory.UNKNOWN)
            evidence = bool(failure and (failure.key_log or failure.evidence))
            confidence = failure.confidence if failure is not None else 0.0
            # Earlier strategies win exact ties because registry order expresses
            # deployment preference and normally favors project-owned recipes.
            priority = -attempts.index(item)
            return int(repair_surface), int(known or evidence), confidence, priority

        return max(attempts, key=score)

    def _record_selection(
        self,
        profile: ProjectProfile,
        attempts: list[BuildStrategyAttempt],
        selected: BuildStrategyAttempt,
        reason: str,
    ) -> tuple[ArtifactRef, ...]:
        if self.storage is None:
            return ()
        portfolio_result = replace(
            selected.result,
            started_at=attempts[0].result.started_at,
            finished_at=attempts[-1].result.finished_at,
            summary=f"deterministic portfolio: {reason}",
            metadata={
                **selected.result.metadata,
                "portfolio_attempt_ids": tuple(
                    item.result.attempt_id for item in attempts
                ),
                "portfolio_strategy_order": tuple(
                    item.plan.strategy for item in attempts
                ),
                "portfolio_selected_strategy": selected.plan.strategy,
                "portfolio_selection_reason": reason,
            },
        )
        payload: dict[str, Any] = {
            "project_id": profile.project_id,
            "strategy_order": [item.plan.strategy for item in attempts],
            "attempts": attempts,
            "selected_attempt_id": selected.result.attempt_id,
            "selected_strategy": selected.plan.strategy,
            "selection_reason": reason,
            "result": selected.result,
            "portfolio_result": portfolio_result,
            "failure": selected.failure,
        }
        artifact = self.storage.save(
            f"build-portfolios/{attempts[0].result.attempt_id}/selection.json",
            to_json_bytes(payload),
            media_type="application/json",
        )
        return (artifact,)


def create_deterministic_builder(
    storage: Storage,
    config: BuildConfig | None = None,
) -> DeterministicBuildService:
    """Wire project-owned Docker, language templates, then optional CNB."""

    build_config = config or BuildConfig()
    runner = RecordedBuildRunner(SubprocessCommandExecutor(storage), storage)
    registry = StrategyRegistry(
        (
            DockerStrategy(runner, build_config),
            JVMTemplateStrategy(runner, build_config),
            NativeTemplateStrategy(runner, build_config),
            TemplateStrategy(runner, build_config),
            CNBStrategy(runner, build_config),
        )
    )
    return DeterministicBuildService(
        registry,
        FailureClassifierChain(RuleBasedBuildFailureClassifier(storage)),
        config=build_config,
        storage=storage,
    )

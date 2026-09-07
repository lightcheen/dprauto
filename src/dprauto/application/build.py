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
    attempt_number: int = 1
    strategy_attempt_number: int = 1
    retry_of_attempt_id: str = ""
    retry_reason: str = ""


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

    def build(
        self,
        profile: ProjectProfile,
        workspace: Path,
        *,
        deadline_at: datetime | None = None,
    ) -> BuildExecution:
        strategies = (
            self.registry.candidates(profile)
            if self.config.strategy_portfolio_enabled
            else (self.registry.select(profile),)
        )
        strategies = strategies[: self.config.max_strategy_attempts]
        attempts: list[BuildStrategyAttempt] = []
        selection_reason = "all compatible strategies failed"
        terminal_attempt: BuildStrategyAttempt | None = None

        for index, strategy in enumerate(strategies):
            if index and time_budget_exhausted(deadline_at):
                selection_reason = "shared build deadline exhausted before fallback"
                break
            plan = strategy.create_plan(profile)
            strategy_attempt_number = 0
            retry_of_attempt_id = ""
            retry_reason = ""
            retried_fingerprints: set[str] = set()
            while True:
                strategy_attempt_number += 1
                result = strategy.build(plan, workspace, deadline_at=deadline_at)
                failure = self.failure_classifier.classify(profile, plan, result)
                attempts.append(
                    BuildStrategyAttempt(
                        plan,
                        result,
                        failure,
                        attempt_number=len(attempts) + 1,
                        strategy_attempt_number=strategy_attempt_number,
                        retry_of_attempt_id=retry_of_attempt_id,
                        retry_reason=retry_reason,
                    )
                )

                if result.status is BuildStatus.SUCCEEDED and failure is None:
                    suffix = (
                        f" after {strategy_attempt_number - 1} transient retry"
                        if strategy_attempt_number > 1
                        else ""
                    )
                    selection_reason = f"strategy {plan.strategy} succeeded{suffix}"
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
                if self._should_retry_transient_network(
                    failure,
                    result,
                    strategy_attempt_number=strategy_attempt_number,
                    retried_fingerprints=retried_fingerprints,
                    deadline_at=deadline_at,
                ):
                    retried_fingerprints.add(failure.fingerprint)
                    retry_of_attempt_id = result.attempt_id
                    retry_reason = (
                        f"{failure.category.value}:{failure.fingerprint}"
                    )
                    continue
                if failure.infrastructure_related or failure.kind in {
                    BuildFailureKind.NETWORK,
                    BuildFailureKind.DOCKER_INFRASTRUCTURE,
                }:
                    if strategy_attempt_number > 1:
                        selection_reason = (
                            f"strategy {plan.strategy} transient retry exhausted; "
                            "fallback stopped"
                        )
                    else:
                        selection_reason = (
                            f"strategy {plan.strategy} hit infrastructure failure; "
                            "fallback stopped"
                        )
                    terminal_attempt = attempts[-1]
                    break
                # A classified project failure may use the next compatible
                # deterministic strategy, but it is never retried unchanged.
                break
            if terminal_attempt is not None:
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

    def _should_retry_transient_network(
        self,
        failure: FailureInfo,
        result: BuildResult,
        *,
        strategy_attempt_number: int,
        retried_fingerprints: set[str],
        deadline_at: datetime | None,
    ) -> bool:
        retries_used = strategy_attempt_number - 1
        return (
            self.config.allow_network
            and result.status is BuildStatus.FAILED
            and failure.kind is BuildFailureKind.NETWORK
            and failure.category is FailureCategory.NETWORK
            and failure.retryable
            and failure.infrastructure_related
            and retries_used < self.config.max_transient_retries
            and failure.fingerprint not in retried_fingerprints
            and not time_budget_exhausted(deadline_at)
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
                "portfolio_transient_retry_count": sum(
                    bool(item.retry_of_attempt_id) for item in attempts
                ),
            },
        )
        payload: dict[str, Any] = {
            "project_id": profile.project_id,
            "strategy_order": [item.plan.strategy for item in attempts],
            "attempt_sequence": [
                {
                    "attempt_id": item.result.attempt_id,
                    "attempt_number": item.attempt_number,
                    "strategy": item.plan.strategy,
                    "strategy_attempt_number": item.strategy_attempt_number,
                    "retry_of_attempt_id": item.retry_of_attempt_id,
                    "retry_reason": item.retry_reason,
                    "status": item.result.status.value,
                    "failure_fingerprint": (
                        item.failure.fingerprint if item.failure is not None else ""
                    ),
                }
                for item in attempts
            ],
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

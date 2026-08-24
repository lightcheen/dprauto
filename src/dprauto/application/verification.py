"""Application service that runs and persists the three verification layers."""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path

from dprauto.adapters.verification import DockerContainerRuntime
from dprauto.config import BuildConfig, VerificationConfig
from dprauto.domain.enums import VerificationLevel, VerificationStatus
from dprauto.domain.models import (
    BuildPlan,
    BuildResult,
    ProjectProfile,
    VerificationCheck,
    VerificationReport,
    VerificationResult,
)
from dprauto.errors import DPRAutoError
from dprauto.ports.storage import Storage
from dprauto.ports.verification import VerificationContext, Verifier
from dprauto.serialization import to_json_bytes
from dprauto.time_budget import time_budget_exhausted
from dprauto.verification import (
    InstallabilityVerifier,
    RunnabilityVerifier,
    TestabilityVerifier,
)
from dprauto.verification.common import verification_id


_PYRAMID = (
    VerificationLevel.INSTALLABILITY,
    VerificationLevel.TESTABILITY,
    VerificationLevel.RUNNABILITY,
)


class LayeredVerificationService:
    def __init__(self, verifiers: tuple[Verifier, ...], storage: Storage) -> None:
        self.verifiers = verifiers
        self.storage = storage

    def verify(
        self,
        profile: ProjectProfile,
        build_result: BuildResult,
        workspace: Path,
        *,
        build_plan: BuildPlan | None = None,
        deadline_at: datetime | None = None,
    ) -> VerificationReport:
        workspace = workspace.expanduser().resolve()
        results: list[VerificationResult] = []
        artifacts = []
        for level in _PYRAMID:
            if time_budget_exhausted(deadline_at):
                results.append(
                    self._error(level, "workflow time budget exceeded before verification")
                )
                break
            verifier = next(
                (item for item in self.verifiers if item.supports(profile, level)),
                None,
            )
            context = VerificationContext(
                profile,
                build_result,
                workspace,
                build_plan,
                tuple(results),
                deadline_at,
            )
            if verifier is None:
                result = self._skipped(level, "no verifier supports this project and level")
            else:
                try:
                    result = verifier.verify(context)
                except (DPRAutoError, OSError, ValueError) as exc:
                    result = self._error(level, f"verifier raised {type(exc).__name__}: {exc}")
            results.append(result)
            artifacts.append(
                self.storage.save(
                    f"attempts/{build_result.attempt_id}/verifications/"
                    f"{result.verification_id}.json",
                    to_json_bytes(result),
                    media_type="application/json",
                )
            )

        report_id = uuid.uuid4().hex
        report = VerificationReport(report_id, build_result.attempt_id, tuple(results), tuple(artifacts))
        report_artifact = self.storage.save(
            f"attempts/{build_result.attempt_id}/verifications/report-{report_id}.json",
            to_json_bytes(report),
            media_type="application/json",
        )
        return VerificationReport(
            report.report_id,
            report.attempt_id,
            report.results,
            (*report.artifacts, report_artifact),
        )

    @staticmethod
    def _skipped(level: VerificationLevel, summary: str) -> VerificationResult:
        check = VerificationCheck(level.value, VerificationStatus.SKIPPED, summary)
        return VerificationResult(
            verification_id(level), level, check.status, summary=summary, checks=(check,)
        )

    @staticmethod
    def _error(level: VerificationLevel, summary: str) -> VerificationResult:
        check = VerificationCheck(level.value, VerificationStatus.ERROR, summary)
        return VerificationResult(
            verification_id(level), level, check.status, summary=summary, checks=(check,)
        )


def create_layered_verifier(
    storage: Storage,
    build_config: BuildConfig | None = None,
    verification_config: VerificationConfig | None = None,
) -> LayeredVerificationService:
    verification_config = verification_config or VerificationConfig()
    runtime = DockerContainerRuntime(storage, build_config, verification_config)
    return LayeredVerificationService(
        (
            InstallabilityVerifier(runtime),
            TestabilityVerifier(runtime, config=verification_config),
            RunnabilityVerifier(runtime, verification_config),
        ),
        storage,
    )

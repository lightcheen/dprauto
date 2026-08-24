"""Environment verification ports and request context."""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from dprauto.domain.enums import VerificationLevel
from dprauto.domain.models import (
    BuildPlan,
    BuildResult,
    ProjectProfile,
    VerificationReport,
    VerificationResult,
)


@dataclass(frozen=True, slots=True)
class VerificationContext:
    profile: ProjectProfile
    build_result: BuildResult
    workspace: Path
    build_plan: BuildPlan | None = None
    prior_results: tuple[VerificationResult, ...] = ()
    deadline_at: datetime | None = None


@runtime_checkable
class Verifier(Protocol):
    def supports(self, profile: ProjectProfile, level: VerificationLevel) -> bool:
        """Return whether the verifier can evaluate this profile and level."""
        ...

    def verify(
        self,
        context: VerificationContext,
    ) -> VerificationResult:
        """Run one verification level and return independently reportable evidence."""
        ...


@runtime_checkable
class VerificationRunner(Protocol):
    def verify(
        self,
        profile: ProjectProfile,
        build_result: BuildResult,
        workspace: Path,
        *,
        build_plan: BuildPlan | None = None,
        deadline_at: datetime | None = None,
    ) -> VerificationReport:
        """Run the complete layered verification use case."""
        ...

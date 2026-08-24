"""Failure classification port."""

from typing import Protocol, runtime_checkable

from dprauto.domain.models import BuildPlan, BuildResult, FailureInfo, ProjectProfile


@runtime_checkable
class FailureClassifier(Protocol):
    def classify(
        self,
        profile: ProjectProfile,
        plan: BuildPlan,
        result: BuildResult,
    ) -> FailureInfo | None:
        """Return normalized failure information, or None for a successful result."""
        ...


@runtime_checkable
class FailureFallbackClassifier(Protocol):
    def refine(
        self,
        profile: ProjectProfile,
        plan: BuildPlan,
        preliminary: FailureInfo,
    ) -> FailureInfo | None:
        """Refine an Unknown result using only its bounded structured evidence."""
        ...

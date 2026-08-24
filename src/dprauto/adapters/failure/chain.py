"""Optional fallback chain; intended for a future LLM-backed classifier."""

from __future__ import annotations

from dprauto.domain.enums import FailureCategory
from dprauto.domain.models import BuildPlan, BuildResult, FailureInfo, ProjectProfile
from dprauto.ports.failure import FailureClassifier, FailureFallbackClassifier


class FailureClassifierChain:
    def __init__(
        self,
        rules: FailureClassifier,
        fallback: FailureFallbackClassifier | None = None,
    ) -> None:
        self.rules = rules
        self.fallback = fallback

    def classify(
        self,
        profile: ProjectProfile,
        plan: BuildPlan,
        result: BuildResult,
    ) -> FailureInfo | None:
        preliminary = self.rules.classify(profile, plan, result)
        if (
            preliminary is None
            or preliminary.category is not FailureCategory.UNKNOWN
            or self.fallback is None
        ):
            return preliminary
        return self.fallback.refine(profile, plan, preliminary) or preliminary

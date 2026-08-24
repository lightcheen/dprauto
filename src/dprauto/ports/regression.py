"""Regression comparison port."""

from typing import Protocol, runtime_checkable

from dprauto.domain.models import (
    ProjectProfile,
    RegressionBaseline,
    RegressionResult,
    VerificationReport,
)


@runtime_checkable
class RegressionChecker(Protocol):
    def create_baseline(
        self,
        profile: ProjectProfile,
        report: VerificationReport,
    ) -> RegressionBaseline:
        ...

    def check(
        self,
        baseline: RegressionBaseline,
        current: VerificationReport,
    ) -> RegressionResult:
        ...

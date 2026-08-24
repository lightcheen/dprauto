"""Create immutable pass baselines and compare post-repair verification reports."""

from __future__ import annotations

import re
import uuid

from dprauto.domain.enums import RegressionStatus, VerificationStatus
from dprauto.domain.models import (
    ProjectProfile,
    RegressionBaseline,
    RegressionExpectation,
    RegressionFinding,
    RegressionResult,
    VerificationCheck,
    VerificationReport,
)
from dprauto.ports.storage import Storage
from dprauto.serialization import to_json_bytes


class PersistedRegressionChecker:
    """Only checks that previously passed become mandatory regression checks."""

    def __init__(self, storage: Storage) -> None:
        self.storage = storage

    def create_baseline(
        self,
        profile: ProjectProfile,
        report: VerificationReport,
    ) -> RegressionBaseline:
        expectations: list[RegressionExpectation] = []
        for result in report.results:
            if result.status is VerificationStatus.PASSED and not result.checks:
                expectations.append(
                    RegressionExpectation(
                        "__layer__",
                        result.level,
                        result.level.value,
                        result.verification_id,
                        result.summary,
                    )
                )
            for check in result.checks:
                if check.status is VerificationStatus.PASSED:
                    expectations.append(
                        RegressionExpectation(
                            check.identity,
                            result.level,
                            check.name,
                            result.verification_id,
                            check.summary,
                        )
                    )
        baseline_id = uuid.uuid4().hex
        baseline = RegressionBaseline(
            baseline_id,
            profile.project_id,
            report.report_id,
            report.attempt_id,
            tuple(expectations),
        )
        artifact = self.storage.save(
            f"regressions/{self._slug(profile.project_id)}/baselines/{baseline_id}.json",
            to_json_bytes(baseline),
            media_type="application/json",
        )
        return RegressionBaseline(
            baseline.baseline_id,
            baseline.project_id,
            baseline.source_report_id,
            baseline.source_attempt_id,
            baseline.required_checks,
            (artifact,),
        )

    def check(
        self,
        baseline: RegressionBaseline,
        current: VerificationReport,
    ) -> RegressionResult:
        current_checks: dict[str, tuple[VerificationCheck, str]] = {}
        for result in current.results:
            current_checks[f"{result.level.value}:__layer__"] = (
                VerificationCheck(result.level.value, result.status, check_id="__layer__"),
                result.verification_id,
            )
            for check in result.checks:
                identity = f"{result.level.value}:{check.identity}"
                current_checks[identity] = (check, result.verification_id)

        findings: list[RegressionFinding] = []
        for expected in baseline.required_checks:
            matched = current_checks.get(expected.identity)
            if matched is None:
                findings.append(
                    RegressionFinding(
                        expected,
                        None,
                        summary="previously passing check was not rerun",
                    )
                )
                continue
            check, verification_id = matched
            findings.append(
                RegressionFinding(
                    expected,
                    check.status,
                    verification_id,
                    (
                        "previously passing check still passes"
                        if check.status is VerificationStatus.PASSED
                        else "previously passing check failed after repair"
                        if check.status
                        in {VerificationStatus.FAILED, VerificationStatus.ERROR}
                        else "previously passing check was skipped after repair"
                    ),
                )
            )

        if any(item.regressed for item in findings):
            status = RegressionStatus.REGRESSION
            summary = "one or more previously passing checks regressed"
        elif any(not item.rerun for item in findings):
            status = RegressionStatus.INCOMPLETE
            summary = "one or more previously passing checks were not rerun"
        else:
            status = RegressionStatus.PASSED
            summary = "all previously passing checks were rerun and still pass"

        regression_id = uuid.uuid4().hex
        result = RegressionResult(
            regression_id,
            baseline.baseline_id,
            current.report_id,
            status,
            tuple(findings),
            summary,
        )
        artifact = self.storage.save(
            f"regressions/{self._slug(baseline.project_id)}/checks/{regression_id}.json",
            to_json_bytes(result),
            media_type="application/json",
        )
        return RegressionResult(
            result.regression_id,
            result.baseline_id,
            result.current_report_id,
            result.status,
            result.findings,
            result.summary,
            (artifact,),
        )

    @staticmethod
    def _slug(value: str) -> str:
        return re.sub(r"[^a-z0-9._-]+", "-", value.lower()).strip("-._")[:100] or "project"

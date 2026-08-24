"""Generic immutable data models used by the entire system."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PurePosixPath
import shlex
from typing import Any, Mapping

from dprauto.domain.enums import (
    BuildFailureKind,
    BuildStage,
    BuildStatus,
    ChangeKind,
    CommandPurpose,
    FailureCategory,
    EnvironmentBuildStatus,
    ProjectType,
    RegressionStatus,
    RiskLevel,
    VerificationLevel,
    VerificationStatus,
)
from dprauto.errors import ModelValidationError


def _require_text(value: str, field_name: str) -> None:
    if not value or not value.strip():
        raise ModelValidationError(f"{field_name} must not be empty")


def _validate_relative_path(value: str, field_name: str) -> None:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise ModelValidationError(f"{field_name} must be a safe relative path")


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    key: str
    digest: str | None = None
    media_type: str = "application/octet-stream"
    size_bytes: int | None = None

    def __post_init__(self) -> None:
        _require_text(self.key, "artifact.key")
        if self.size_bytes is not None and self.size_bytes < 0:
            raise ModelValidationError("artifact.size_bytes must not be negative")


@dataclass(frozen=True, slots=True)
class SourceReference:
    locator: str
    revision: str | None = None
    subdirectory: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.locator, "source.locator")
        if self.subdirectory:
            _validate_relative_path(self.subdirectory, "source.subdirectory")


@dataclass(frozen=True, slots=True)
class CommandSpec:
    argv: tuple[str, ...]
    purpose: CommandPurpose = CommandPurpose.OTHER
    cwd: str | None = None
    environment: Mapping[str, str] = field(default_factory=dict)
    timeout_seconds: int = 300
    shell: bool = False

    def __post_init__(self) -> None:
        if not self.argv or any(not item for item in self.argv):
            raise ModelValidationError("command.argv must contain non-empty arguments")
        if self.cwd:
            _validate_relative_path(self.cwd, "command.cwd")
        if self.timeout_seconds <= 0:
            raise ModelValidationError("command.timeout_seconds must be positive")

    @property
    def display(self) -> str:
        """Return a stable human-readable representation of this command."""

        if self.shell and len(self.argv) == 1:
            return self.argv[0]
        return shlex.join(self.argv)


@dataclass(frozen=True, slots=True)
class CommandResult:
    command: CommandSpec
    exit_code: int | None
    stdout: ArtifactRef | None = None
    stderr: ArtifactRef | None = None
    timed_out: bool = False
    duration_seconds: float = 0.0

    def __post_init__(self) -> None:
        if self.duration_seconds < 0:
            raise ModelValidationError("command result duration must not be negative")

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


@dataclass(frozen=True, slots=True)
class RepairPreflightCheck:
    """One non-building validation of a proposed environment change."""

    check_id: str
    status: VerificationStatus
    summary: str
    command_result: CommandResult | None = None

    def __post_init__(self) -> None:
        _require_text(self.check_id, "repair preflight check.check_id")
        _require_text(self.summary, "repair preflight check.summary")


@dataclass(frozen=True, slots=True)
class RepairPreflightResult:
    """Bounded evidence used to decide whether a full rebuild is justified."""

    status: VerificationStatus
    checks: tuple[RepairPreflightCheck, ...]
    summary: str
    artifacts: tuple[ArtifactRef, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.summary, "repair preflight result.summary")

    @property
    def accepted(self) -> bool:
        return self.status in {VerificationStatus.PASSED, VerificationStatus.SKIPPED}


@dataclass(frozen=True, slots=True)
class ProjectCommand:
    name: str
    command: CommandSpec
    source: str
    confidence: float = 1.0

    def __post_init__(self) -> None:
        _require_text(self.name, "project command.name")
        _require_text(self.source, "project command.source")
        if not 0.0 <= self.confidence <= 1.0:
            raise ModelValidationError("project command.confidence must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class ProjectProfile:
    project_id: str
    source: SourceReference
    languages: tuple[str, ...] = ()
    project_type: ProjectType = ProjectType.UNKNOWN
    runtime_constraints: Mapping[str, str] = field(default_factory=dict)
    package_managers: tuple[str, ...] = ()
    dependency_files: tuple[str, ...] = ()
    build_files: tuple[str, ...] = ()
    dockerfiles: tuple[str, ...] = ()
    readme_files: tuple[str, ...] = ()
    ci_files: tuple[str, ...] = ()
    commands: tuple[ProjectCommand, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text(self.project_id, "project.project_id")
        for field_name, values in (
            ("dependency_files", self.dependency_files),
            ("build_files", self.build_files),
            ("dockerfiles", self.dockerfiles),
            ("readme_files", self.readme_files),
            ("ci_files", self.ci_files),
        ):
            for value in values:
                _validate_relative_path(value, f"project.{field_name}")


@dataclass(frozen=True, slots=True)
class BuildStep:
    name: str
    stage: BuildStage
    command: CommandSpec
    required: bool = True

    def __post_init__(self) -> None:
        _require_text(self.name, "build step.name")


@dataclass(frozen=True, slots=True)
class GeneratedFile:
    path: str
    content: str
    executable: bool = False
    media_type: str = "text/plain"

    def __post_init__(self) -> None:
        _validate_relative_path(self.path, "generated file.path")
        _require_text(self.path, "generated file.path")
        _require_text(self.media_type, "generated file.media_type")


@dataclass(frozen=True, slots=True)
class BuildPlan:
    plan_id: str
    project_id: str
    strategy: str
    steps: tuple[BuildStep, ...]
    platform: str | None = None
    environment: Mapping[str, str] = field(default_factory=dict)
    network_allowed: bool = True
    cache_enabled: bool = True
    metadata: Mapping[str, Any] = field(default_factory=dict)
    generated_files: tuple[GeneratedFile, ...] = ()
    source_files: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.plan_id, "build plan.plan_id")
        _require_text(self.project_id, "build plan.project_id")
        _require_text(self.strategy, "build plan.strategy")
        if not self.steps:
            raise ModelValidationError("build plan.steps must not be empty")
        for value in self.source_files:
            _validate_relative_path(value, "build plan.source_files")


@dataclass(frozen=True, slots=True)
class BuildResult:
    attempt_id: str
    plan_id: str
    status: BuildStatus
    started_at: datetime
    finished_at: datetime
    exit_code: int | None = None
    failed_stage: BuildStage | None = None
    image_reference: str | None = None
    logs: tuple[ArtifactRef, ...] = ()
    outputs: tuple[ArtifactRef, ...] = ()
    command_results: tuple[CommandResult, ...] = ()
    summary: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text(self.attempt_id, "build result.attempt_id")
        _require_text(self.plan_id, "build result.plan_id")
        if self.finished_at < self.started_at:
            raise ModelValidationError("build result.finished_at must not precede started_at")
        if self.status is BuildStatus.SUCCEEDED and self.exit_code not in (None, 0):
            raise ModelValidationError("a successful build cannot have a non-zero exit code")

    @property
    def duration_seconds(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()


@dataclass(frozen=True, slots=True)
class FailureInfo:
    category: FailureCategory
    failure_stage: BuildStage
    message: str
    fingerprint: str
    kind: BuildFailureKind = BuildFailureKind.PROJECT_BUILD
    failed_command: CommandSpec | None = None
    key_log: str = ""
    environment: Mapping[str, str] = field(default_factory=dict)
    possible_cause: str = ""
    evidence: tuple[str, ...] = ()
    retryable: bool = False
    infrastructure_related: bool = False
    confidence: float = 1.0
    suggestions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.message, "failure.message")
        _require_text(self.fingerprint, "failure.fingerprint")
        if not 0.0 <= self.confidence <= 1.0:
            raise ModelValidationError("failure.confidence must be between 0 and 1")

    @property
    def stage(self) -> BuildStage:
        """Backward-compatible alias for the normalized failure stage."""

        return self.failure_stage


@dataclass(frozen=True, slots=True)
class VerificationResult:
    verification_id: str
    level: VerificationLevel
    status: VerificationStatus
    command_result: CommandResult | None = None
    evidence: tuple[ArtifactRef, ...] = ()
    summary: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    checks: tuple["VerificationCheck", ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.verification_id, "verification.verification_id")
        identities = tuple(check.identity for check in self.checks)
        if len(identities) != len(set(identities)):
            raise ModelValidationError("verification check identities must be unique within a layer")

    @property
    def passed(self) -> bool:
        return self.status is VerificationStatus.PASSED

    @property
    def command_results(self) -> tuple[CommandResult, ...]:
        """Return all commands executed for this layer, without duplicates."""

        values: list[CommandResult] = []
        if self.command_result is not None:
            values.append(self.command_result)
        for check in self.checks:
            if check.command_result is not None and check.command_result not in values:
                values.append(check.command_result)
        return tuple(values)


@dataclass(frozen=True, slots=True)
class VerificationCheck:
    """One independently reportable assertion within a verification layer."""

    name: str
    status: VerificationStatus
    summary: str = ""
    command_result: CommandResult | None = None
    evidence: tuple[ArtifactRef, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    check_id: str = ""

    def __post_init__(self) -> None:
        _require_text(self.name, "verification check.name")
        if self.check_id:
            _require_text(self.check_id, "verification check.check_id")

    @property
    def passed(self) -> bool:
        return self.status is VerificationStatus.PASSED

    @property
    def identity(self) -> str:
        return self.check_id or self.name


@dataclass(frozen=True, slots=True)
class VerificationReport:
    """Persisted aggregate that keeps every pyramid layer separate."""

    report_id: str
    attempt_id: str
    results: tuple[VerificationResult, ...]
    artifacts: tuple[ArtifactRef, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.report_id, "verification report.report_id")
        _require_text(self.attempt_id, "verification report.attempt_id")
        levels = tuple(result.level for result in self.results)
        if len(levels) != len(set(levels)):
            raise ModelValidationError("verification report levels must be unique")

    def result_for(self, level: VerificationLevel) -> VerificationResult | None:
        return next((item for item in self.results if item.level is level), None)

    @property
    def succeeded(self) -> bool:
        """Require installability and runnability; skipped tests are neutral."""

        installability = self.result_for(VerificationLevel.INSTALLABILITY)
        runnability = self.result_for(VerificationLevel.RUNNABILITY)
        testability = self.result_for(VerificationLevel.TESTABILITY)
        return bool(
            installability
            and installability.passed
            and runnability
            and runnability.passed
            and testability
            and testability.status
            not in {VerificationStatus.FAILED, VerificationStatus.ERROR}
        )


@dataclass(frozen=True, slots=True)
class RegressionExpectation:
    """A previously passing check that must be rerun after every repair."""

    check_id: str
    level: VerificationLevel
    name: str
    source_verification_id: str
    summary: str = ""

    def __post_init__(self) -> None:
        _require_text(self.check_id, "regression expectation.check_id")
        _require_text(self.name, "regression expectation.name")
        _require_text(
            self.source_verification_id,
            "regression expectation.source_verification_id",
        )

    @property
    def identity(self) -> str:
        return f"{self.level.value}:{self.check_id}"


@dataclass(frozen=True, slots=True)
class RegressionBaseline:
    baseline_id: str
    project_id: str
    source_report_id: str
    source_attempt_id: str
    required_checks: tuple[RegressionExpectation, ...]
    artifacts: tuple[ArtifactRef, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.baseline_id, "regression baseline.baseline_id")
        _require_text(self.project_id, "regression baseline.project_id")
        _require_text(self.source_report_id, "regression baseline.source_report_id")
        identities = tuple(item.identity for item in self.required_checks)
        if len(identities) != len(set(identities)):
            raise ModelValidationError("regression baseline check identities must be unique")


@dataclass(frozen=True, slots=True)
class RegressionFinding:
    expectation: RegressionExpectation
    current_status: VerificationStatus | None
    current_verification_id: str | None = None
    summary: str = ""

    @property
    def regressed(self) -> bool:
        return self.current_status in {
            VerificationStatus.FAILED,
            VerificationStatus.ERROR,
        }

    @property
    def rerun(self) -> bool:
        return self.current_status is not None and self.current_status is not VerificationStatus.SKIPPED


@dataclass(frozen=True, slots=True)
class RegressionResult:
    regression_id: str
    baseline_id: str
    current_report_id: str
    status: RegressionStatus
    findings: tuple[RegressionFinding, ...]
    summary: str = ""
    artifacts: tuple[ArtifactRef, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.regression_id, "regression result.regression_id")
        _require_text(self.baseline_id, "regression result.baseline_id")
        _require_text(self.current_report_id, "regression result.current_report_id")

    @property
    def accepted(self) -> bool:
        return self.status is RegressionStatus.PASSED

    @property
    def regressions(self) -> tuple[RegressionFinding, ...]:
        return tuple(item for item in self.findings if item.regressed)


@dataclass(frozen=True, slots=True)
class FileChange:
    path: str
    kind: ChangeKind
    before_digest: str | None = None
    after_digest: str | None = None

    def __post_init__(self) -> None:
        _validate_relative_path(self.path, "file change.path")


@dataclass(frozen=True, slots=True)
class DependencyChange:
    ecosystem: str
    name: str
    kind: ChangeKind
    before: str | None = None
    after: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.ecosystem, "dependency change.ecosystem")
        _require_text(self.name, "dependency change.name")


@dataclass(frozen=True, slots=True)
class ValueChange:
    name: str
    before: str | None
    after: str | None

    def __post_init__(self) -> None:
        _require_text(self.name, "value change.name")


@dataclass(frozen=True, slots=True)
class EnvironmentSnapshot:
    """Normalized, secret-conscious description of one workspace environment."""

    build_scripts: Mapping[str, str] = field(default_factory=dict)
    business_source: Mapping[str, str] = field(default_factory=dict)
    base_image: str | None = None
    python_version: str | None = None
    system_packages: Mapping[str, str | None] = field(default_factory=dict)
    python_dependencies: Mapping[str, str | None] = field(default_factory=dict)
    environment_variables: Mapping[str, str] = field(default_factory=dict)
    startup_arguments: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EnvironmentDiff:
    files: tuple[FileChange, ...] = ()
    dependencies: tuple[DependencyChange, ...] = ()
    environment_variables: tuple[ValueChange, ...] = ()
    base_image: ValueChange | None = None
    python_version: ValueChange | None = None
    system_packages: tuple[DependencyChange, ...] = ()
    python_dependencies: tuple[DependencyChange, ...] = ()
    startup_arguments: tuple[ValueChange, ...] = ()
    build_scripts: tuple[FileChange, ...] = ()
    business_source: tuple[FileChange, ...] = ()
    source_changed: bool = False
    risk_level: RiskLevel = RiskLevel.NONE
    requires_manual_review: bool = False
    policy_violations: tuple[str, ...] = ()
    summary: str = ""

    @property
    def is_empty(self) -> bool:
        return not (
            self.files
            or self.dependencies
            or self.environment_variables
            or self.base_image
            or self.python_version
            or self.system_packages
            or self.python_dependencies
            or self.startup_arguments
            or self.build_scripts
            or self.business_source
            or self.source_changed
        )


@dataclass(frozen=True, slots=True)
class BuildScriptSnapshot:
    """Final bounded content of a build script relevant to the result."""

    path: str
    content: str
    digest: str

    def __post_init__(self) -> None:
        _validate_relative_path(self.path, "build script snapshot.path")
        _require_text(self.path, "build script snapshot.path")
        _require_text(self.digest, "build script snapshot.digest")


@dataclass(frozen=True, slots=True)
class EnvironmentBuildResult:
    """Complete, externally serializable result of the orchestration graph."""

    run_id: str
    final_status: EnvironmentBuildStatus
    project_profile: ProjectProfile | None
    build_strategy: str | None
    build_plan: BuildPlan | None
    build_result: BuildResult | None
    agent_participated: bool
    repair_attempts: int
    final_build_scripts: tuple[BuildScriptSnapshot, ...]
    installability: VerificationResult | None
    testability: VerificationResult | None
    runnability: VerificationResult | None
    verification_report: VerificationReport | None
    regression_result: RegressionResult | None
    environment_diff: EnvironmentDiff | None
    environment_diffs: tuple[EnvironmentDiff, ...]
    failure: FailureInfo | None
    total_duration_seconds: float
    llm_call_count: int
    stop_reason: str
    artifacts: tuple[ArtifactRef, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.run_id, "environment build result.run_id")
        if self.repair_attempts < 0:
            raise ModelValidationError(
                "environment build result.repair_attempts must not be negative"
            )
        if self.total_duration_seconds < 0:
            raise ModelValidationError(
                "environment build result.total_duration_seconds must not be negative"
            )
        if self.llm_call_count < 0:
            raise ModelValidationError(
                "environment build result.llm_call_count must not be negative"
            )

    @property
    def succeeded(self) -> bool:
        return self.final_status is EnvironmentBuildStatus.SUCCEEDED

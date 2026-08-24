"""Stable enumerations shared across the core workflow."""

from enum import Enum


class BuildStatus(str, Enum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class BuildFailureKind(str, Enum):
    GIT = "git"
    NETWORK = "network"
    DOCKER_INFRASTRUCTURE = "docker_infrastructure"
    PROJECT_BUILD = "project_build"


class BuildStage(str, Enum):
    SOURCE_ACQUISITION = "source_acquisition"
    INSPECTION = "inspection"
    PLANNING = "planning"
    DETECTION = "detection"
    DEPENDENCY_INSTALLATION = "dependency_installation"
    BUILD = "build"
    EXPORT = "export"
    STARTUP = "startup"
    TEST = "test"
    SECURITY = "security"
    UNKNOWN = "unknown"


class FailureCategory(str, Enum):
    INFRASTRUCTURE = "infrastructure"
    DOCKER = "docker"
    SOURCE = "source"
    NETWORK = "network"
    SANDBOX = "sandbox"
    TOOLCHAIN = "toolchain"
    RUNTIME_VERSION = "runtime_version"
    SYSTEM_DEPENDENCY = "system_dependency"
    PACKAGE_DEPENDENCY = "package_dependency"
    PYTHON_DEPENDENCY = "python_dependency"
    DEPENDENCY_CONFLICT = "dependency_conflict"
    BUILD_TOOL = "build_tool"
    COMPILATION = "compilation"
    BUILD_COMMAND = "build_command"
    TEST = "test"
    RUN = "run"
    EXTERNAL_SERVICE = "external_service"
    VERIFICATION = "verification"
    REGRESSION = "regression"
    POLICY = "policy"
    SECURITY = "security"
    UNKNOWN = "unknown"


class CommandPurpose(str, Enum):
    INSTALL = "install"
    BUILD = "build"
    SMOKE = "smoke"
    TEST = "test"
    RUN = "run"
    SECURITY = "security"
    OTHER = "other"


class ProjectType(str, Enum):
    WEB = "web"
    CLI = "cli"
    SCRIPT = "script"
    LIBRARY = "library"
    UNKNOWN = "unknown"


class VerificationLevel(str, Enum):
    INSTALLABILITY = "installability"
    SMOKE = "smoke"
    TESTABILITY = "testability"
    RUNNABILITY = "runnability"
    SECURITY = "security"
    VULNERABILITY_REPRODUCTION = "vulnerability_reproduction"


class VerificationStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ERROR = "error"


class RegressionStatus(str, Enum):
    PASSED = "passed"
    REGRESSION = "regression"
    INCOMPLETE = "incomplete"
    ERROR = "error"


class ChangeKind(str, Enum):
    ADDED = "added"
    REMOVED = "removed"
    MODIFIED = "modified"


class RiskLevel(str, Enum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class AgentPhase(str, Enum):
    CREATED = "created"
    INSPECTING = "inspecting"
    PLANNING = "planning"
    BUILDING = "building"
    CLASSIFYING = "classifying"
    DIAGNOSING = "diagnosing"
    PROPOSING = "proposing"
    POLICY_CHECK = "policy_check"
    APPLYING = "applying"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    STOPPED = "stopped"


class EnvironmentBuildStatus(str, Enum):
    """Terminal outcome of the complete environment-build workflow."""

    SUCCEEDED = "succeeded"
    PROJECT_FAILED = "project_failed"
    INFRASTRUCTURE_FAILED = "infrastructure_failed"
    VERIFICATION_FAILED = "verification_failed"
    REGRESSION = "regression"
    MANUAL_REVIEW = "manual_review"
    MAX_ATTEMPTS = "max_attempts"
    TIME_BUDGET_EXCEEDED = "time_budget_exceeded"
    ERROR = "error"

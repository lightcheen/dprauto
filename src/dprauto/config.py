"""Central, dependency-free application configuration."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from string import Formatter
from typing import Mapping

from dprauto.errors import ConfigurationError

_DOCKER_NETWORK_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_TOOL_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]*$")
_FIXED_PACKAGE_VERSION = re.compile(r"^[0-9][A-Za-z0-9_.+-]*$")


def _validate_docker_network(value: str, name: str) -> None:
    if value and (len(value) > 255 or not _DOCKER_NETWORK_NAME.fullmatch(value)):
        raise ConfigurationError(
            f"{name} must be empty or a valid Docker network name, got {value!r}"
        )


def _validate_poetry_tool_image(value: str) -> None:
    if not value:
        return
    fields = {
        field_name
        for _, field_name, _, _ in Formatter().parse(value)
        if field_name is not None
    }
    if not fields <= {"version", "poetry_version"}:
        raise ConfigurationError(
            "build.poetry_tool_image may only format {version} and {poetry_version}"
        )
    try:
        rendered = value.format(version="3.11", poetry_version="1.8.5")
    except (KeyError, ValueError) as exc:
        raise ConfigurationError(
            "build.poetry_tool_image may only format {version} and {poetry_version}"
        ) from exc
    if not rendered.strip() or any(character.isspace() for character in rendered):
        raise ConfigurationError(
            "build.poetry_tool_image must render to a non-empty Docker image reference"
        )


def _validate_versioned_image(value: str, name: str) -> None:
    if not value.strip():
        raise ConfigurationError(f"build.{name} must not be empty")
    fields = {
        field_name
        for _, field_name, _, _ in Formatter().parse(value)
        if field_name is not None
    }
    if not fields <= {"version"}:
        raise ConfigurationError(f"build.{name} may only format {{version}}")
    try:
        rendered = value.format(version="17")
    except (KeyError, ValueError) as exc:
        raise ConfigurationError(f"build.{name} may only format {{version}}") from exc
    if not rendered.strip() or any(character.isspace() for character in rendered):
        raise ConfigurationError(
            f"build.{name} must render to a non-empty Docker image reference"
        )


def _read_bool(value: str, name: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be a boolean, got {value!r}")


def _read_int(value: str, name: str, *, minimum: int = 1) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer, got {value!r}") from exc
    if parsed < minimum:
        raise ConfigurationError(f"{name} must be >= {minimum}, got {parsed}")
    return parsed


def _read_float(value: str, name: str, *, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number, got {value!r}") from exc
    if not minimum <= parsed <= maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}, got {parsed}")
    return parsed


@dataclass(frozen=True)
class BuildConfig:
    default_strategy: str = "deterministic"
    timeout_seconds: int = 1800
    strategy_portfolio_enabled: bool = True
    max_strategy_attempts: int = 3
    allow_network: bool = True
    forward_proxy_environment: bool = True
    use_cache: bool = True
    docker_binary: str = "docker"
    image_repository: str = "dprauto"
    default_python_version: str = "3.11"
    python_base_image: str = "python:{version}-slim"
    default_java_version: str = "17"
    maven_base_image: str = "maven:3.9.9-eclipse-temurin-{version}"
    gradle_base_image: str = "gradle:8.12.1-jdk{version}"
    native_base_image: str = "debian:bookworm-slim"
    max_build_jobs: int = 2
    pack_binary: str = "pack"
    cnb_builder: str = "paketobuildpacks/builder-jammy-full"
    cnb_lifecycle_image: str = ""
    docker_network: str = ""
    poetry_version: str = "1.8.5"
    poetry_tool_image: str = ""
    poetry_tool_timeout_seconds: int = 900

    def __post_init__(self) -> None:
        if not self.default_strategy.strip():
            raise ConfigurationError("build.default_strategy must not be empty")
        if self.timeout_seconds <= 0:
            raise ConfigurationError("build.timeout_seconds must be positive")
        if self.max_strategy_attempts <= 0:
            raise ConfigurationError("build.max_strategy_attempts must be positive")
        if not 1 <= self.max_build_jobs <= 32:
            raise ConfigurationError("build.max_build_jobs must be between 1 and 32")
        if self.poetry_tool_timeout_seconds <= 0:
            raise ConfigurationError("build.poetry_tool_timeout_seconds must be positive")
        for name, value in (
            ("docker_binary", self.docker_binary),
            ("image_repository", self.image_repository),
            ("default_python_version", self.default_python_version),
            ("python_base_image", self.python_base_image),
            ("default_java_version", self.default_java_version),
            ("native_base_image", self.native_base_image),
            ("pack_binary", self.pack_binary),
            ("cnb_builder", self.cnb_builder),
        ):
            if not value.strip():
                raise ConfigurationError(f"build.{name} must not be empty")
        _validate_docker_network(self.docker_network, "build.docker_network")
        if not re.fullmatch(r"\d{1,2}", self.default_java_version):
            raise ConfigurationError(
                "build.default_java_version must be a Java feature version"
            )
        _validate_versioned_image(self.maven_base_image, "maven_base_image")
        _validate_versioned_image(self.gradle_base_image, "gradle_base_image")
        if not _TOOL_VERSION.fullmatch(self.poetry_version):
            raise ConfigurationError(
                f"build.poetry_version must be a fixed version, got {self.poetry_version!r}"
            )
        _validate_poetry_tool_image(self.poetry_tool_image)


@dataclass(frozen=True)
class AgentConfig:
    max_attempts: int = 5
    max_repeated_failures: int = 2
    max_total_seconds: int = 7200
    max_context_characters: int = 24_000
    max_recent_modifications: int = 4
    max_failed_methods: int = 6
    max_resolved_issues: int = 8
    preflight_timeout_seconds: int = 30
    max_investigation_rounds: int = 3
    max_investigation_actions: int = 6
    max_evidence_characters: int = 10_000

    def __post_init__(self) -> None:
        if self.max_attempts <= 0:
            raise ConfigurationError("agent.max_attempts must be positive")
        if self.max_repeated_failures <= 0:
            raise ConfigurationError("agent.max_repeated_failures must be positive")
        if self.max_total_seconds <= 0:
            raise ConfigurationError("agent.max_total_seconds must be positive")
        for name, value in (
            ("max_recent_modifications", self.max_recent_modifications),
            ("max_failed_methods", self.max_failed_methods),
            ("max_resolved_issues", self.max_resolved_issues),
            ("preflight_timeout_seconds", self.preflight_timeout_seconds),
            ("max_investigation_rounds", self.max_investigation_rounds),
            ("max_investigation_actions", self.max_investigation_actions),
            ("max_evidence_characters", self.max_evidence_characters),
        ):
            if value <= 0:
                raise ConfigurationError(f"agent.{name} must be positive")
        if self.max_context_characters < 2_000:
            raise ConfigurationError("agent.max_context_characters must be >= 2000")


@dataclass(frozen=True)
class VerificationConfig:
    command_timeout_seconds: int = 120
    dependency_command_timeout_seconds: int = 180
    web_startup_timeout_seconds: int = 30
    web_path: str = "/"
    output_excerpt_characters: int = 4_000
    docker_network: str = ""
    pytest_version: str = "8.3.5"
    pytest_xdist_version: str = "3.6.1"
    tox_version: str = "4.23.2"
    nox_version: str = "2024.10.9"
    max_parallel_test_workers: int = 4
    max_test_files_per_slice: int = 8
    service_orchestration_enabled: bool = True
    service_startup_timeout_seconds: int = 30
    max_service_containers: int = 2
    postgres_service_image: str = "postgres:16-alpine"
    redis_service_image: str = "redis:7-alpine"
    minimal_test_dependency_closure_enabled: bool = True
    max_dependency_analysis_files: int = 256

    def __post_init__(self) -> None:
        if self.command_timeout_seconds <= 0:
            raise ConfigurationError("verification.command_timeout_seconds must be positive")
        if self.dependency_command_timeout_seconds <= 0:
            raise ConfigurationError(
                "verification.dependency_command_timeout_seconds must be positive"
            )
        if self.web_startup_timeout_seconds <= 0:
            raise ConfigurationError("verification.web_startup_timeout_seconds must be positive")
        if not self.web_path.startswith("/"):
            raise ConfigurationError("verification.web_path must start with '/'")
        if self.output_excerpt_characters < 200:
            raise ConfigurationError(
                "verification.output_excerpt_characters must be >= 200"
            )
        _validate_docker_network(
            self.docker_network, "verification.docker_network"
        )
        for name, value in (
            ("pytest_version", self.pytest_version),
            ("pytest_xdist_version", self.pytest_xdist_version),
            ("tox_version", self.tox_version),
            ("nox_version", self.nox_version),
        ):
            if not _FIXED_PACKAGE_VERSION.fullmatch(value):
                raise ConfigurationError(
                    f"verification.{name} must be a fixed version, got {value!r}"
                )
        if not 1 <= self.max_parallel_test_workers <= 32:
            raise ConfigurationError(
                "verification.max_parallel_test_workers must be between 1 and 32"
            )
        if not 1 <= self.max_test_files_per_slice <= 32:
            raise ConfigurationError(
                "verification.max_test_files_per_slice must be between 1 and 32"
            )
        if self.service_startup_timeout_seconds <= 0:
            raise ConfigurationError(
                "verification.service_startup_timeout_seconds must be positive"
            )
        if not 1 <= self.max_service_containers <= 4:
            raise ConfigurationError(
                "verification.max_service_containers must be between 1 and 4"
            )
        _validate_versioned_image(
            self.postgres_service_image, "postgres_service_image"
        )
        _validate_versioned_image(self.redis_service_image, "redis_service_image")
        if not 16 <= self.max_dependency_analysis_files <= 2_048:
            raise ConfigurationError(
                "verification.max_dependency_analysis_files must be between 16 and 2048"
            )


@dataclass(frozen=True)
class LLMConfig:
    provider: str = ""
    model: str = ""
    api_key_env: str = ""
    temperature: float = 0.0
    timeout_seconds: int = 120
    timeout_retries_per_model: int = 1
    max_timeout_attempts_per_operation: int = 2
    max_output_tokens: int = 4_096
    api_config_path: Path = Path("myapi.json")
    request_log_root: Path = Path("logs/llm")

    @property
    def enabled(self) -> bool:
        return bool(self.provider and self.model)

    def __post_init__(self) -> None:
        if not 0.0 <= self.temperature <= 2.0:
            raise ConfigurationError("llm.temperature must be between 0 and 2")
        if self.timeout_seconds <= 0:
            raise ConfigurationError("llm.timeout_seconds must be positive")
        if self.timeout_retries_per_model < 0:
            raise ConfigurationError("llm.timeout_retries_per_model must not be negative")
        if not 1 <= self.max_timeout_attempts_per_operation <= 8:
            raise ConfigurationError(
                "llm.max_timeout_attempts_per_operation must be between 1 and 8"
            )
        if self.max_output_tokens <= 0:
            raise ConfigurationError("llm.max_output_tokens must be positive")
        if not str(self.api_config_path).strip():
            raise ConfigurationError("llm.api_config_path must not be empty")
        if not str(self.request_log_root).strip():
            raise ConfigurationError("llm.request_log_root must not be empty")


@dataclass(frozen=True)
class StorageConfig:
    root: Path = Path("runs")

    def __post_init__(self) -> None:
        if not str(self.root).strip():
            raise ConfigurationError("storage.root must not be empty")


@dataclass(frozen=True)
class SecurityConfig:
    allow_source_changes: bool = False
    allow_privileged_execution: bool = False
    allowed_mutation_globs: tuple[str, ...] = (
        "Dockerfile",
        "**/Dockerfile",
        "setup.sh",
        "**/setup.sh",
        ".dprauto/requirements-verification.txt",
    )


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "INFO"

    def __post_init__(self) -> None:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        normalized = self.level.upper()
        if normalized not in allowed:
            raise ConfigurationError(f"logging.level must be one of {sorted(allowed)}")
        object.__setattr__(self, "level", normalized)


@dataclass(frozen=True)
class AppConfig:
    build: BuildConfig = field(default_factory=BuildConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    verification: VerificationConfig = field(default_factory=VerificationConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)


def load_config(
    environ: Mapping[str, str] | None = None,
    *,
    prefix: str = "DPRAUTO_",
) -> AppConfig:
    """Load all application settings from one environment namespace."""

    env = os.environ if environ is None else environ

    def get(name: str, default: str) -> str:
        return env.get(f"{prefix}{name}", default)

    build = BuildConfig(
        default_strategy=get("BUILD_DEFAULT_STRATEGY", "deterministic"),
        timeout_seconds=_read_int(
            get("BUILD_TIMEOUT_SECONDS", "1800"), "BUILD_TIMEOUT_SECONDS"
        ),
        strategy_portfolio_enabled=_read_bool(
            get("BUILD_STRATEGY_PORTFOLIO_ENABLED", "true"),
            "BUILD_STRATEGY_PORTFOLIO_ENABLED",
        ),
        max_strategy_attempts=_read_int(
            get("BUILD_MAX_STRATEGY_ATTEMPTS", "3"),
            "BUILD_MAX_STRATEGY_ATTEMPTS",
        ),
        allow_network=_read_bool(get("BUILD_ALLOW_NETWORK", "true"), "BUILD_ALLOW_NETWORK"),
        forward_proxy_environment=_read_bool(
            get("BUILD_FORWARD_PROXY_ENVIRONMENT", "true"),
            "BUILD_FORWARD_PROXY_ENVIRONMENT",
        ),
        use_cache=_read_bool(get("BUILD_USE_CACHE", "true"), "BUILD_USE_CACHE"),
        docker_binary=get("BUILD_DOCKER_BINARY", "docker"),
        image_repository=get("BUILD_IMAGE_REPOSITORY", "dprauto"),
        default_python_version=get("BUILD_DEFAULT_PYTHON_VERSION", "3.11"),
        python_base_image=get("BUILD_PYTHON_BASE_IMAGE", "python:{version}-slim"),
        default_java_version=get("BUILD_DEFAULT_JAVA_VERSION", "17"),
        maven_base_image=get(
            "BUILD_MAVEN_BASE_IMAGE", "maven:3.9.9-eclipse-temurin-{version}"
        ),
        gradle_base_image=get("BUILD_GRADLE_BASE_IMAGE", "gradle:8.12.1-jdk{version}"),
        native_base_image=get("BUILD_NATIVE_BASE_IMAGE", "debian:bookworm-slim"),
        max_build_jobs=_read_int(
            get("BUILD_MAX_BUILD_JOBS", "2"), "BUILD_MAX_BUILD_JOBS"
        ),
        pack_binary=get("BUILD_PACK_BINARY", "pack"),
        cnb_builder=get("BUILD_CNB_BUILDER", "paketobuildpacks/builder-jammy-full"),
        cnb_lifecycle_image=get("BUILD_CNB_LIFECYCLE_IMAGE", ""),
        docker_network=get("BUILD_DOCKER_NETWORK", ""),
        poetry_version=get("BUILD_POETRY_VERSION", "1.8.5"),
        poetry_tool_image=get("BUILD_POETRY_TOOL_IMAGE", ""),
        poetry_tool_timeout_seconds=_read_int(
            get("BUILD_POETRY_TOOL_TIMEOUT_SECONDS", "900"),
            "BUILD_POETRY_TOOL_TIMEOUT_SECONDS",
        ),
    )
    agent = AgentConfig(
        max_attempts=_read_int(get("AGENT_MAX_ATTEMPTS", "5"), "AGENT_MAX_ATTEMPTS"),
        max_repeated_failures=_read_int(
            get("AGENT_MAX_REPEATED_FAILURES", "2"), "AGENT_MAX_REPEATED_FAILURES"
        ),
        max_total_seconds=_read_int(
            get("AGENT_MAX_TOTAL_SECONDS", "7200"), "AGENT_MAX_TOTAL_SECONDS"
        ),
        max_context_characters=_read_int(
            get("AGENT_MAX_CONTEXT_CHARACTERS", "24000"),
            "AGENT_MAX_CONTEXT_CHARACTERS",
        ),
        max_recent_modifications=_read_int(
            get("AGENT_MAX_RECENT_MODIFICATIONS", "4"),
            "AGENT_MAX_RECENT_MODIFICATIONS",
        ),
        max_failed_methods=_read_int(
            get("AGENT_MAX_FAILED_METHODS", "6"),
            "AGENT_MAX_FAILED_METHODS",
        ),
        max_resolved_issues=_read_int(
            get("AGENT_MAX_RESOLVED_ISSUES", "8"),
            "AGENT_MAX_RESOLVED_ISSUES",
        ),
        preflight_timeout_seconds=_read_int(
            get("AGENT_PREFLIGHT_TIMEOUT_SECONDS", "30"),
            "AGENT_PREFLIGHT_TIMEOUT_SECONDS",
        ),
        max_investigation_rounds=_read_int(
            get("AGENT_MAX_INVESTIGATION_ROUNDS", "3"),
            "AGENT_MAX_INVESTIGATION_ROUNDS",
        ),
        max_investigation_actions=_read_int(
            get("AGENT_MAX_INVESTIGATION_ACTIONS", "6"),
            "AGENT_MAX_INVESTIGATION_ACTIONS",
        ),
        max_evidence_characters=_read_int(
            get("AGENT_MAX_EVIDENCE_CHARACTERS", "10000"),
            "AGENT_MAX_EVIDENCE_CHARACTERS",
        ),
    )
    verification = VerificationConfig(
        command_timeout_seconds=_read_int(
            get("VERIFICATION_COMMAND_TIMEOUT_SECONDS", "120"),
            "VERIFICATION_COMMAND_TIMEOUT_SECONDS",
        ),
        dependency_command_timeout_seconds=_read_int(
            get("VERIFICATION_DEPENDENCY_COMMAND_TIMEOUT_SECONDS", "180"),
            "VERIFICATION_DEPENDENCY_COMMAND_TIMEOUT_SECONDS",
        ),
        web_startup_timeout_seconds=_read_int(
            get("VERIFICATION_WEB_STARTUP_TIMEOUT_SECONDS", "30"),
            "VERIFICATION_WEB_STARTUP_TIMEOUT_SECONDS",
        ),
        web_path=get("VERIFICATION_WEB_PATH", "/"),
        output_excerpt_characters=_read_int(
            get("VERIFICATION_OUTPUT_EXCERPT_CHARACTERS", "4000"),
            "VERIFICATION_OUTPUT_EXCERPT_CHARACTERS",
            minimum=200,
        ),
        docker_network=get("VERIFICATION_DOCKER_NETWORK", ""),
        pytest_version=get("VERIFICATION_PYTEST_VERSION", "8.3.5"),
        pytest_xdist_version=get("VERIFICATION_PYTEST_XDIST_VERSION", "3.6.1"),
        tox_version=get("VERIFICATION_TOX_VERSION", "4.23.2"),
        nox_version=get("VERIFICATION_NOX_VERSION", "2024.10.9"),
        max_parallel_test_workers=_read_int(
            get("VERIFICATION_MAX_PARALLEL_TEST_WORKERS", "4"),
            "VERIFICATION_MAX_PARALLEL_TEST_WORKERS",
            minimum=1,
        ),
        max_test_files_per_slice=_read_int(
            get("VERIFICATION_MAX_TEST_FILES_PER_SLICE", "8"),
            "VERIFICATION_MAX_TEST_FILES_PER_SLICE",
            minimum=1,
        ),
        service_orchestration_enabled=_read_bool(
            get("VERIFICATION_SERVICE_ORCHESTRATION_ENABLED", "true"),
            "VERIFICATION_SERVICE_ORCHESTRATION_ENABLED",
        ),
        service_startup_timeout_seconds=_read_int(
            get("VERIFICATION_SERVICE_STARTUP_TIMEOUT_SECONDS", "30"),
            "VERIFICATION_SERVICE_STARTUP_TIMEOUT_SECONDS",
            minimum=1,
        ),
        max_service_containers=_read_int(
            get("VERIFICATION_MAX_SERVICE_CONTAINERS", "2"),
            "VERIFICATION_MAX_SERVICE_CONTAINERS",
            minimum=1,
        ),
        postgres_service_image=get(
            "VERIFICATION_POSTGRES_SERVICE_IMAGE", "postgres:16-alpine"
        ),
        redis_service_image=get(
            "VERIFICATION_REDIS_SERVICE_IMAGE", "redis:7-alpine"
        ),
        minimal_test_dependency_closure_enabled=_read_bool(
            get("VERIFICATION_MINIMAL_TEST_DEPENDENCY_CLOSURE_ENABLED", "true"),
            "VERIFICATION_MINIMAL_TEST_DEPENDENCY_CLOSURE_ENABLED",
        ),
        max_dependency_analysis_files=_read_int(
            get("VERIFICATION_MAX_DEPENDENCY_ANALYSIS_FILES", "256"),
            "VERIFICATION_MAX_DEPENDENCY_ANALYSIS_FILES",
            minimum=16,
        ),
    )
    llm = LLMConfig(
        provider=get("LLM_PROVIDER", ""),
        model=get("LLM_MODEL", ""),
        api_key_env=get("LLM_API_KEY_ENV", ""),
        temperature=_read_float(
            get("LLM_TEMPERATURE", "0"), "LLM_TEMPERATURE", minimum=0.0, maximum=2.0
        ),
        timeout_seconds=_read_int(get("LLM_TIMEOUT_SECONDS", "120"), "LLM_TIMEOUT_SECONDS"),
        timeout_retries_per_model=_read_int(
            get("LLM_TIMEOUT_RETRIES_PER_MODEL", "1"),
            "LLM_TIMEOUT_RETRIES_PER_MODEL",
            minimum=0,
        ),
        max_timeout_attempts_per_operation=_read_int(
            get("LLM_MAX_TIMEOUT_ATTEMPTS_PER_OPERATION", "2"),
            "LLM_MAX_TIMEOUT_ATTEMPTS_PER_OPERATION",
        ),
        max_output_tokens=_read_int(
            get("LLM_MAX_OUTPUT_TOKENS", "4096"), "LLM_MAX_OUTPUT_TOKENS"
        ),
        api_config_path=Path(get("LLM_API_CONFIG_PATH", "myapi.json")),
        request_log_root=Path(get("LLM_REQUEST_LOG_ROOT", "logs/llm")),
    )
    storage = StorageConfig(root=Path(get("STORAGE_ROOT", "runs")))
    security = SecurityConfig(
        allow_source_changes=_read_bool(
            get("SECURITY_ALLOW_SOURCE_CHANGES", "false"),
            "SECURITY_ALLOW_SOURCE_CHANGES",
        ),
        allow_privileged_execution=_read_bool(
            get("SECURITY_ALLOW_PRIVILEGED_EXECUTION", "false"),
            "SECURITY_ALLOW_PRIVILEGED_EXECUTION",
        ),
    )
    logging = LoggingConfig(level=get("LOG_LEVEL", "INFO"))
    return AppConfig(
        build=build,
        agent=agent,
        verification=verification,
        llm=llm,
        storage=storage,
        security=security,
        logging=logging,
    )

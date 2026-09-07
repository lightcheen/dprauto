"""Rule-first classification that sends only bounded evidence downstream."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace

from dprauto.domain.enums import (
    BuildFailureKind,
    BuildStage,
    BuildStatus,
    CommandPurpose,
    FailureCategory,
)
from dprauto.domain.models import (
    BuildPlan,
    BuildResult,
    CommandSpec,
    FailureInfo,
    ProjectProfile,
)
from dprauto.ports.storage import Storage


@dataclass(frozen=True, slots=True)
class FailureRule:
    category: FailureCategory
    message: str
    possible_cause: str
    patterns: tuple[str, ...]
    kind: BuildFailureKind = BuildFailureKind.PROJECT_BUILD
    retryable: bool = False
    infrastructure_related: bool = False
    confidence: float = 0.96
    suggestions: tuple[str, ...] = ()


DOCKER_RULE = FailureRule(
    category=FailureCategory.DOCKER,
    kind=BuildFailureKind.DOCKER_INFRASTRUCTURE,
    message="Docker infrastructure failed",
    possible_cause=(
        "Docker daemon, socket, network pool, or local container infrastructure is unavailable"
    ),
    retryable=True,
    infrastructure_related=True,
    confidence=0.99,
    patterns=(
        r"cannot connect to the docker daemon",
        r"is the docker daemon running",
        r"permission denied[^\n]*docker\.sock",
        r"error during connect[^\n]*docker",
        r"failed to execute [^\n]*(?:\bdocker\b|\bpack\b)[^\n]*",
        r"failed to create ephemeral bridge network",
        r"all predefined address pools have been fully subnetted",
        r"error response from daemon[^\n]*(?:network|containerd|overlay)",
    ),
)

NETWORK_RULE = FailureRule(
    category=FailureCategory.NETWORK,
    kind=BuildFailureKind.NETWORK,
    message="Network operation failed",
    possible_cause=(
        "DNS, proxy, registry, or package download connectivity is unavailable or unstable"
    ),
    retryable=True,
    infrastructure_related=True,
    confidence=0.99,
    patterns=(
        r"could not resolve host",
        r"temporary failure in name resolution",
        r"temporary failure resolving(?:\s+['\"]?[^\s'\"]+['\"]?)?",
        r"network is unreachable",
        r"httpsconnectionpool[^\n]*read timed out",
        r"readtimeouterror",
        r"connection (?:timed out|reset)",
        r"tls handshake timeout",
        r"proxyerror",
        r"failed to establish a new connection",
        r"failed to fetch dependency[^\n]*(?:dial tcp|connection refused|timeout)",
        r"dial tcp[^\n]*:443[^\n]*connect: connection refused",
        r"(?:load metadata for|pulling from|container registry)[\s\S]{0,300}unexpected eof",
        r"failed to read expected number of bytes: unexpected eof",
    ),
)

IMAGE_RESOLUTION_RULE = FailureRule(
    category=FailureCategory.DOCKER,
    kind=BuildFailureKind.DOCKER_INFRASTRUCTURE,
    message="Container base image could not be resolved for this environment",
    possible_cause=(
        "The registry image, tag, manifest, or requested host platform is unavailable"
    ),
    retryable=False,
    infrastructure_related=True,
    confidence=0.99,
    patterns=(
        r"no match for platform in manifest",
        r"manifest for [^\n]+ not found",
        r"failed to resolve source metadata for [^\n]+(?:pull access denied|not found)",
    ),
)

GIT_RULE = FailureRule(
    category=FailureCategory.SOURCE,
    kind=BuildFailureKind.GIT,
    message="Git source operation failed",
    possible_cause=(
        "Repository access, revision, submodule, authentication, or VCS metadata is invalid"
    ),
    patterns=(
        r"fatal: not a git repository",
        r"repository [^\n]+ not found",
        r"could not read from remote repository",
        r"authentication failed for [^\n]+\.git",
        r"fatal: (?:bad object|invalid reference|reference is not a tree)",
        r"submodule path [^\n]+ did not contain",
    ),
)

VCS_METADATA_RULE = FailureRule(
    category=FailureCategory.BUILD_TOOL,
    message="Python package version metadata is unavailable",
    possible_cause=(
        "The source snapshot omits VCS metadata required by setuptools-scm or another version tool"
    ),
    retryable=True,
    patterns=(
        r"setuptools-scm was unable to detect version",
        r"building from a fully intact git repository",
    ),
)

OMITTED_VCS_METADATA_RULE = FailureRule(
    category=FailureCategory.POLICY,
    message="Build requires VCS metadata omitted from the generated context",
    possible_cause=(
        "A build plugin reads Git metadata, while the generated container context excludes .git"
    ),
    patterns=(
        r"no \.git directory found",
        r"could not find (?:the )?\.git directory",
        r"git repository metadata (?:is )?(?:missing|required)",
    ),
    confidence=0.99,
    suggestions=(
        "Use a repository-supported version override or disable a metadata-only build plugin.",
        "Do not copy credentials or the host Git configuration into the image.",
    ),
)

BENCHMARK_SENTINEL_POLICY_RULE = FailureRule(
    category=FailureCategory.POLICY,
    message="Evaluation-only source marker entered the project build context",
    possible_cause=(
        "A dataset sentinel absent from the repository was copied into the image and rejected by "
        "a license or source policy check"
    ),
    patterns=(r"/\.cnb-benchmark-source-ready",),
    confidence=0.99,
)

MAVEN_WRAPPER_CONFIG_RULE = FailureRule(
    category=FailureCategory.BUILD_TOOL,
    message="Maven Wrapper inherited incompatible launcher configuration",
    possible_cause=(
        "The base image MAVEN_CONFIG path was expanded by the repository wrapper as a lifecycle "
        "argument"
    ),
    patterns=(r"unknown lifecycle phase [\"']?/root/\.m2[\"']?",),
    confidence=0.99,
)

MAVEN_LICENSE_METADATA_RULE = FailureRule(
    category=FailureCategory.POLICY,
    message="Maven license formatting requires omitted Git metadata",
    possible_cause=(
        "A license-format goal expects a Git work tree, while the generated container context "
        "intentionally excludes repository metadata"
    ),
    patterns=(
        r"license-maven-plugin[^\n]+one of setgitdir or setworktree must be called",
    ),
    confidence=0.99,
)

MAVEN_GIT_HOOK_METADATA_RULE = FailureRule(
    category=FailureCategory.POLICY,
    message="Maven client Git-hook installation requires omitted Git metadata",
    possible_cause=(
        "A developer-workstation hook goal ran inside a generated container context that "
        "intentionally excludes repository metadata"
    ),
    patterns=(
        r"git-build-hook-maven-plugin[^\n]+could not find or initialise a local git repository",
    ),
    confidence=0.99,
)

GRADLE_WRAPPER_DOWNLOAD_RULE = FailureRule(
    category=FailureCategory.NETWORK,
    kind=BuildFailureKind.NETWORK,
    message="Gradle Wrapper distribution download timed out",
    possible_cause=(
        "The wrapper download exceeded its repository-configured network timeout before the "
        "Gradle distribution was cached"
    ),
    retryable=True,
    infrastructure_related=True,
    patterns=(
        r"downloading from https?://[^\n]+gradle-[^\n]+ failed: timeout \(\d+ms\)",
        r"downloading https?://[^\n]+gradle-[^\n]+\n[^\n]*sockettimeoutexception: read timed out",
        r"downloading https?://[^\n]+gradle-[\s\S]{0,2000}"
        r"java\.net\.connectexception: connection refused",
    ),
    confidence=0.99,
)

JVM_TOOLCHAIN_RULE = FailureRule(
    category=FailureCategory.TOOLCHAIN,
    message="Required JVM toolchain is not installed",
    possible_cause=(
        "The selected container JDK does not match the repository's explicit Java toolchain "
        "language version or vendor"
    ),
    patterns=(
        r"cannot find a java installation[^\n]+matching: \{languageversion=\d+[^\n]+\}",
        r"cannot find a java installation[^\n]+matching the daemon jvm defined requirements",
        r"no matching toolchains found for requested specification",
        r"no toolchain found for type jdk",
        r"cannot find matching toolchain definitions[^\n]*",
    ),
    confidence=0.99,
    suggestions=(
        "Select the build-launcher JDK independently from the compilation target JDK.",
        "Materialize only repository-declared JDK toolchains in the generated image.",
    ),
)

JVM_RUNTIME_REQUIREMENT_RULE = FailureRule(
    category=FailureCategory.RUNTIME_VERSION,
    message="JVM runtime is too old for a build dependency",
    possible_cause=(
        "The build launcher JDK was selected from a compilation target instead of the runtime "
        "required by Gradle or one of its plugins"
    ),
    patterns=(
        r"dependency requires at least jvm runtime version \d+[^\n]+uses a java \d+ jvm",
        r"run this build using a java \d+ or newer jvm",
        r"unsupportedclassversionerror:[^\n]+class file version [0-9.]+[^\n]+"
        r"recognizes class file versions up to [0-9.]+",
    ),
    confidence=0.99,
    suggestions=(
        "Raise the build-launcher JDK to the minimum version evidenced by the failing plugin.",
        "Keep the repository compilation target unchanged unless its own manifest requires it.",
    ),
)

PROJECT_RULES = (
    FailureRule(
        category=FailureCategory.BUILD_COMMAND,
        message="Verification command references an invalid workspace path",
        possible_cause=(
            "The selected repository command assumes a different working directory or a clean "
            "build tree"
        ),
        patterns=(
            r"(?:python(?:[0-9.]+)?): can(?:not|'t) open file [^\n]+(?:no such file or directory)",
            r"mkdir: cannot create director(?:y|ies) [^\n]+file exists",
        ),
        suggestions=(
            "Resolve command paths relative to the selected build root and container workdir.",
            "Make setup commands idempotent when a generated image already contains build output.",
        ),
    ),
    FailureRule(
        category=FailureCategory.BUILD_COMMAND,
        message="Dockerfile references a missing build-context path",
        possible_cause=(
            "The selected Dockerfile belongs to another context or copies a path absent from the repository"
        ),
        patterns=(
            r"failed to calculate checksum[^\n]+not found",
            r"failed to compute cache key[^\n]+not found",
        ),
    ),
    FailureRule(
        category=FailureCategory.RUNTIME_VERSION,
        message="Python version is incompatible",
        possible_cause=(
            "The selected Python interpreter does not satisfy package or project constraints"
        ),
        patterns=(
            r"versions? that require a different python version[^\n]+",
            r"unsupported python version[^\n]+",
            r"python [0-9.]+ is not supported",
            r"package [^\n]+ requires python [^\n]+",
            r"requires-python [^\n]+(?:not satisfied|is incompatible)",
        ),
    ),
    FailureRule(
        category=FailureCategory.BUILD_TOOL,
        message="Build tool version is too old",
        possible_cause=(
            "The container build tool does not satisfy the minimum version declared by the project"
        ),
        patterns=(
            r"cmake [0-9.]+ or higher is required[^\n]+running version [0-9.]+",
            r"requires cmake (?:version )?[0-9.]+ or higher",
            r"(?:gradle|maven) [0-9.]+ or (?:higher|newer) is required",
        ),
        confidence=0.99,
        suggestions=(
            "Use the exact minimum build-tool version declared by the repository.",
            "Do not change project source to weaken its minimum-version check.",
        ),
    ),
    FailureRule(
        category=FailureCategory.SYSTEM_DEPENDENCY,
        message="System dependency is missing",
        possible_cause=(
            "A native header, library, compiler, or operating-system package is not installed"
        ),
        patterns=(
            r"fatal error: [^\n]+\.h: no such file or directory",
            r"(?:looking for include file )?[^\n ]+\.h (?:-|was )?not found",
            r"pg_config executable not found",
            r"pkg-config[^\n]*(?:not found|could not find)",
            r'''package ['"][^'"]+['"][^\n]+required by ['"]virtual:world['"][^\n]+not found''',
            r"cannot find required librar(?:y|ies) [^\n]+",
            r"could not find [a-z0-9_.+-]+ \(missing: [^)]+\)",
            r"could not find a package configuration file provided by",
            r"(?:nasm|yasm) not found or too old",
            r"cannot find -l[a-z0-9_.+-]+",
            r"(?:gcc|g\+\+|clang|make): (?:command )?not found",
            r"unable to execute ['\"]?(?:gcc|g\+\+|clang|cc)['\"]?: no such file or directory",
            r"cannot find command ['\"]git['\"]",
            r"no such file or directory: ['\"]git['\"]",
        ),
        suggestions=(
            "Resolve the named capability through the dependency evidence safety table.",
            "Install only packages supported by repository build-file evidence.",
        ),
    ),
    FailureRule(
        category=FailureCategory.PACKAGE_DEPENDENCY,
        message="Declared package artifact is unavailable",
        possible_cause=(
            "A Maven or Gradle dependency is absent from the repositories configured by the project"
        ),
        patterns=(
            r"could not find artifact [a-z0-9_.+-]+:[a-z0-9_.+-]+:[^\s]+",
            r"could not find [a-z0-9_.+-]+:[a-z0-9_.+-]+:[a-z0-9_.+${}-]+",
            r"the following artifacts could not be resolved:[^\n]+\(absent\)",
            r"no versions? (?:of [^\n]+ )?(?:are|is) available",
        ),
        confidence=0.98,
        suggestions=(
            "Check repository declarations, snapshot availability, and the pinned source revision.",
            "Do not repeatedly download an artifact that the configured repository reports absent.",
        ),
    ),
    FailureRule(
        category=FailureCategory.DEPENDENCY_CONFLICT,
        message="Dependency resolution conflict",
        possible_cause=(
            "Pinned package versions or transitive constraints cannot be resolved together"
        ),
        patterns=(
            r"resolutionimpossible",
            r"package versions have conflicting dependencies",
            r"conflicting dependencies",
            r"version solving failed",
            r"because [^\n]+ depends on [^\n]+ and [^\n]+ depends on",
            r"pyproject\.toml changed significantly since [^\n]*lock",
            r"invalid requirement: [^\n]+expected end or semicolon",
        ),
    ),
    FailureRule(
        category=FailureCategory.PYTHON_DEPENDENCY,
        message="Python dependency is missing or unavailable",
        possible_cause=(
            "A required Python module is not installed, not importable, or unavailable for this "
            "platform"
        ),
        patterns=(
            r"modulenotfounderror: no module named [^\n]+",
            r"importerror: cannot import name [^\n]+",
            r"no matching distribution found for [^\n]+",
            r"could not find a version that satisfies the requirement [^\n]+",
        ),
    ),
    FailureRule(
        category=FailureCategory.COMPILATION,
        message="Native compilation failed",
        possible_cause=(
            "C/C++ extension source, compiler flags, ABI, or linked library is incompatible"
        ),
        patterns=(
            r"distutils[^\n]*compileerror",
            r"error: command ['\"]?(?:/usr/bin/)?(?:gcc|g\+\+|clang|cc)['\"]? failed",
            r"undefined reference to [^\n]+",
            r"collect2: error: ld returned",
            r"compilation terminated",
            r"failed building wheel for [^\n]+",
            r"\.c(?:pp|xx|c)?[:(][0-9]+[^\n]*error:",
        ),
    ),
    FailureRule(
        category=FailureCategory.BUILD_TOOL,
        message="Build tool failed or was not detected",
        possible_cause=(
            "The required build tool is absent, misconfigured, or cannot recognize the project "
            "layout"
        ),
        patterns=(
            r"no buildpack groups passed detection",
            r"failed to detect: buildpack",
            r"(?:cmake|make|ninja|poetry|pip|python): (?:command )?not found",
            r"(?:make:\s+)?(?:go|cargo|rustc|javac): no such file or directory",
            r"/bin/sh: [0-9]+: [^\n]+: not found",
            r"could not find a package configuration file provided by",
            r"unknown build backend",
            r"unknown build hook: [^\n]+",
            r"not supporting pep 517 builds",
            r"pytest: error: unrecognized arguments:",
        ),
        suggestions=(
            "Add only a build tool explicitly invoked by a selected root build manifest.",
        ),
    ),
)

EXTERNAL_SERVICE_RULE = FailureRule(
    category=FailureCategory.EXTERNAL_SERVICE,
    message="Required external service is unavailable",
    possible_cause=(
        "A database, cache, HTTP service, credential, or API dependency is missing or unreachable"
    ),
    retryable=True,
    patterns=(
        r"connectionrefusederror: \[errno 111\] connection refused",
        r"could not connect to server:[^\n]*connection refused",
        r"(?:redis|postgres|postgresql|mysql|neo4j)[^\n]*(?:connection refused|unavailable)",
        r"(?:api[_ -]?key|credential)[^\n]*(?:not set|missing|not found|required)",
        r"nocredentialserror",
        r"(?:http[^\n]*\b503\b|503 service unavailable)",
    ),
)


def ordered_failure_rules(stage: BuildStage) -> tuple[FailureRule, ...]:
    """Return the shared, deterministic rule precedence for one build stage."""

    strong_project_rules = tuple(
        rule
        for rule in PROJECT_RULES
        if rule.category is not FailureCategory.PYTHON_DEPENDENCY
    )
    python_dependency_rule = next(
        rule
        for rule in PROJECT_RULES
        if rule.category is FailureCategory.PYTHON_DEPENDENCY
    )
    rules: list[FailureRule] = [
        DOCKER_RULE,
        IMAGE_RESOLUTION_RULE,
        BENCHMARK_SENTINEL_POLICY_RULE,
        MAVEN_WRAPPER_CONFIG_RULE,
        MAVEN_LICENSE_METADATA_RULE,
        MAVEN_GIT_HOOK_METADATA_RULE,
        OMITTED_VCS_METADATA_RULE,
        GRADLE_WRAPPER_DOWNLOAD_RULE,
        JVM_TOOLCHAIN_RULE,
        JVM_RUNTIME_REQUIREMENT_RULE,
    ]
    if stage in {BuildStage.STARTUP, BuildStage.TEST}:
        rules.append(EXTERNAL_SERVICE_RULE)
    # Explicit terminal project errors beat transient retry warnings. Network
    # still precedes generic missing-distribution symptoms, which pip may emit
    # after an index outage.
    rules.extend(
        (
            GIT_RULE,
            VCS_METADATA_RULE,
            *strong_project_rules,
            NETWORK_RULE,
            python_dependency_rule,
        )
    )
    if stage not in {BuildStage.STARTUP, BuildStage.TEST}:
        rules.append(EXTERNAL_SERVICE_RULE)
    return tuple(rules)


def classify_failure_evidence(
    text: str,
    stage: BuildStage,
) -> tuple[FailureRule, str] | None:
    """Classify already-bounded log evidence without requiring storage artifacts."""

    for rule in ordered_failure_rules(stage):
        matches: list[re.Match[str]] = []
        for pattern in rule.patterns:
            matches.extend(re.finditer(pattern, text, re.IGNORECASE))
        if matches:
            # Build tools often print optional missing dependencies before the
            # actual fatal one. Within the same failure class, the last match
            # is closest to the terminating error and is the safer repair
            # target. Rule ordering still preserves infrastructure/category
            # precedence.
            match = max(matches, key=lambda item: item.start())
            return rule, match.group(0).strip()
    return None


class RuleBasedBuildFailureClassifier:
    def __init__(self, storage: Storage, *, inspection_bytes: int = 512 * 1024) -> None:
        if inspection_bytes <= 0:
            raise ValueError("inspection_bytes must be positive")
        self.storage = storage
        self.inspection_bytes = inspection_bytes

    def classify(
        self,
        profile: ProjectProfile,
        plan: BuildPlan,
        result: BuildResult,
    ) -> FailureInfo | None:
        if result.status is BuildStatus.SUCCEEDED:
            return None

        command = self._failed_command(plan, result)
        stage = self._failure_stage(result, command)
        text = self._normalized_log(result)

        classified = classify_failure_evidence(text, stage)
        if classified is not None:
            rule, match = classified
            return self._with_timeout_guidance(
                self._failure(profile, plan, stage, command, text, rule, match),
                text,
                command,
                result,
            )

        fallback_category, message, cause = self._stage_fallback(stage, result)
        fallback_rule = FailureRule(
            category=fallback_category,
            message=message,
            possible_cause=cause,
            patterns=(),
            confidence=0.75 if fallback_category is not FailureCategory.UNKNOWN else 0.2,
        )
        return self._with_timeout_guidance(
            self._failure(
                profile,
                plan,
                stage,
                command,
                text,
                fallback_rule,
                self._causal_fallback(text) or result.summary or "unclassified failure",
            ),
            text,
            command,
            result,
        )

    def _normalized_log(self, result: BuildResult) -> str:
        chunks: list[bytes] = []
        remaining = self.inspection_bytes
        for artifact in reversed(result.logs):
            if remaining <= 0:
                break
            content = self.storage.load(artifact)
            chunk = content[-remaining:]
            chunks.append(chunk)
            remaining -= len(chunk)
        text = b"\n".join(reversed(chunks)).decode("utf-8", errors="replace")
        text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
        return "\n".join(
            re.sub(r"^(?:#[0-9]+\s+)?\[(?:builder|detector|analyzer|exporter)\]\s*", "", line)
            for line in text.splitlines()
        )

    @staticmethod
    def _failed_command(plan: BuildPlan, result: BuildResult) -> CommandSpec | None:
        for command_result in reversed(result.command_results):
            if not command_result.succeeded:
                return command_result.command
        if result.command_results:
            return result.command_results[-1].command
        if result.failed_stage:
            matching = [step.command for step in plan.steps if step.stage is result.failed_stage]
            if matching:
                return matching[-1]
        return plan.steps[-1].command if plan.steps else None

    @staticmethod
    def _failure_stage(result: BuildResult, command: CommandSpec | None) -> BuildStage:
        if result.failed_stage:
            return result.failed_stage
        if command:
            return {
                CommandPurpose.INSTALL: BuildStage.DEPENDENCY_INSTALLATION,
                CommandPurpose.BUILD: BuildStage.BUILD,
                CommandPurpose.TEST: BuildStage.TEST,
                CommandPurpose.RUN: BuildStage.STARTUP,
                CommandPurpose.SMOKE: BuildStage.STARTUP,
                CommandPurpose.SECURITY: BuildStage.SECURITY,
            }.get(command.purpose, BuildStage.UNKNOWN)
        return BuildStage.UNKNOWN

    @staticmethod
    def _stage_fallback(
        stage: BuildStage,
        result: BuildResult,
    ) -> tuple[FailureCategory, str, str]:
        timed_out = result.status is BuildStatus.TIMED_OUT
        if stage is BuildStage.TEST:
            return FailureCategory.TEST, "Test command failed", "Tests failed or timed out"
        if stage is BuildStage.STARTUP:
            return (
                FailureCategory.RUN,
                "Run or startup command failed",
                "The application failed to start or stay healthy",
            )
        if stage in {BuildStage.BUILD, BuildStage.DEPENDENCY_INSTALLATION}:
            if timed_out:
                return (
                    FailureCategory.BUILD_COMMAND,
                    "Build command timed out",
                    "The project build exceeded its configured timeout",
                )
            return (
                FailureCategory.UNKNOWN,
                "Build failure is not recognized",
                "No deterministic failure rule matched the bounded log evidence",
            )
        return (
            FailureCategory.UNKNOWN,
            "Failure is not recognized",
            "No deterministic failure rule matched the bounded log evidence",
        )

    def _failure(
        self,
        profile: ProjectProfile,
        plan: BuildPlan,
        stage: BuildStage,
        command: CommandSpec | None,
        text: str,
        rule: FailureRule,
        match: str,
    ) -> FailureInfo:
        key_log = self._key_log(text, match)
        normalized = re.sub(r"\s+", " ", key_log.lower()).strip()[:1000]
        digest = hashlib.sha256(normalized.encode()).hexdigest()[:16]
        fingerprint = f"{rule.category.value}:{digest}"
        return FailureInfo(
            category=rule.category,
            failure_stage=stage,
            message=rule.message,
            fingerprint=fingerprint,
            kind=rule.kind,
            failed_command=command,
            key_log=key_log,
            environment=self._environment(profile, plan),
            possible_cause=rule.possible_cause,
            evidence=(match,) if match else (),
            retryable=rule.retryable,
            infrastructure_related=rule.infrastructure_related,
            confidence=rule.confidence,
            suggestions=rule.suggestions,
        )

    @staticmethod
    def _key_log(text: str, match: str, *, context_lines: int = 2, limit: int = 3000) -> str:
        lines = text.splitlines()
        matched_index = next(
            (index for index, line in enumerate(lines) if match.lower() in line.lower()),
            len(lines) - 1,
        )
        start = max(0, matched_index - context_lines)
        end = min(len(lines), matched_index + context_lines + 1)
        selected = "\n".join(line for line in lines[start:end] if line.strip()).strip()
        return selected[-limit:]

    @staticmethod
    def _last_meaningful_line(text: str) -> str:
        return next((line.strip() for line in reversed(text.splitlines()) if line.strip()), "")

    @staticmethod
    def _causal_fallback(text: str) -> str:
        """Prefer the project-facing error over a trailing BuildKit stack frame."""

        excluded = (
            "github.com/moby/",
            "golang.org/",
            "/root/build-deb/engine/",
            "runtime.goexit",
            "runtime/asm_",
            "failed to solve:",
        )
        candidates: list[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            lowered = stripped.lower()
            if not stripped or any(value in lowered for value in excluded):
                continue
            if re.search(r"\b(error|failed|failure|not found|no such file|unable to)\b", lowered):
                candidates.append(stripped)
        return candidates[-1] if candidates else RuleBasedBuildFailureClassifier._last_meaningful_line(text)

    @staticmethod
    def _environment(profile: ProjectProfile, plan: BuildPlan) -> dict[str, str]:
        environment = {
            "languages": ",".join(profile.languages),
            "package_managers": ",".join(profile.package_managers),
            "strategy": plan.strategy,
            "network_allowed": str(plan.network_allowed).lower(),
        }
        environment.update(
            {f"runtime.{name}": value for name, value in profile.runtime_constraints.items()}
        )
        if plan.platform:
            environment["platform"] = plan.platform
        for name in ("base_image", "builder", "image_reference"):
            value = plan.metadata.get(name)
            if value:
                environment[name] = str(value)
        return environment

    @classmethod
    def _with_timeout_guidance(
        cls,
        failure: FailureInfo,
        text: str,
        command: CommandSpec | None,
        result: BuildResult,
    ) -> FailureInfo:
        if result.status is not BuildStatus.TIMED_OUT:
            return failure
        profile = cls._timeout_profile(text, command)
        last_line = cls._last_activity_line(text)
        evidence = [
            *failure.evidence,
            f"timeout_profile={profile}",
        ]
        if command is not None:
            evidence.append(f"timeout_command={command.display}")
        if last_line:
            evidence.append(f"last_active_line={last_line[:500]}")
        if cls._dependency_download_active(text):
            evidence.append("timeout_activity=dependency-download")
        suggestions = (
            "Do not add dependencies, change Python version, or change the base image for a timeout alone.",
            "Prefer reducing installation/build scope, disabling docs/lint/full test extras, or using a narrower project-owned test command.",
            "Do not repeat a repair that produced the same timeout profile.",
        )
        if profile == "python-package-install":
            suggestions += (
                "For pip/poetry/uv stalls, prefer existing lock/main dependencies and avoid dev/docs/test extras unless required by the selected verification command.",
            )
        elif profile == "system-package-install":
            suggestions += (
                "For apt/apk/yum stalls, avoid adding system packages unless the log names a missing native header or binary.",
            )
        elif profile == "native-compilation":
            suggestions += (
                "For native compilation stalls, prefer prebuilt wheels or reducing optional compiled extras; do not add unrelated build tools.",
            )
        updates = {}
        if profile == "system-package-install":
            evidence.append(
                "network_inference=system package manager remained active until timeout"
            )
            updates = {
                "category": FailureCategory.NETWORK,
                "kind": BuildFailureKind.NETWORK,
                "message": "System package repository operation timed out",
                "possible_cause": (
                    "DNS, package repository, proxy, or container network connectivity stalled"
                ),
                "fingerprint": f"network:{failure.fingerprint.split(':', 1)[-1]}",
                "retryable": True,
                "infrastructure_related": True,
                "confidence": 0.85,
            }
        return replace(
            failure,
            evidence=tuple(dict.fromkeys(evidence)),
            suggestions=tuple(dict.fromkeys((*failure.suggestions, *suggestions))),
            **updates,
        )

    @staticmethod
    def _last_activity_line(text: str) -> str:
        """Return the last project-facing line before timeout/BuildKit epilogue."""

        ignored = (
            "command timed out after ",
            "context canceled",
            "context cancelled",
            "failed to solve:",
            "canceled: context",
            "cancelled: context",
        )
        return next(
            (
                line.strip()
                for line in reversed(text.splitlines())
                if line.strip()
                and not any(marker in line.casefold() for marker in ignored)
            ),
            "",
        )

    @staticmethod
    def _dependency_download_active(text: str) -> bool:
        normalized = "\n".join(text.splitlines()[-30:]).casefold()
        return bool(
            re.search(r"\b(?:downloading|fetching)\b", normalized)
            or re.search(r"\bget:\d+\s+(?:https?|ftp):", normalized)
            or re.search(
                r"\b(?:running command\s+)?git clone\b[^\n]*(?:https?|ssh|git)://",
                normalized,
            )
            or re.search(
                r"\b\d+(?:\.\d+)?/\d+(?:\.\d+)?\s*(?:kb|mb|gb)\b",
                normalized,
            )
        )

    @staticmethod
    def _timeout_profile(text: str, command: CommandSpec | None) -> str:
        command_text = command.display if command is not None else ""
        tail = "\n".join(text.splitlines()[-120:])
        combined = f"{command_text}\n{tail}".casefold()
        last_activity = RuleBasedBuildFailureClassifier._last_activity_line(text).casefold()
        system_manager_active = bool(
            re.search(r"\b(?:apt-get|apt|apk|dnf|yum)\b", combined)
            and (
                re.search(r"\b(?:get|hit|ign|err):\d+\s+", last_activity)
                or re.search(
                    r"\b(?:apt-get|apk|dnf|yum)\b|reading package lists|"
                    r"building dependency tree|unpacking |setting up ",
                    last_activity,
                )
            )
        )
        if system_manager_active:
            return "system-package-install"
        if re.search(r"\b(?:pip|poetry|uv|pdm|pipenv)\b", combined) or any(
            marker in combined
            for marker in ("collecting ", "downloading ", "building wheel", "installing collected")
        ):
            if any(marker in combined for marker in ("building wheel", "gcc", "g++", "clang", "compiling")):
                return "native-compilation"
            if any(marker in combined for marker in ("resolving dependencies", "version solving", "locking")):
                return "dependency-resolution"
            return "python-package-install"
        if re.search(r"\b(?:apt-get|apt|apk|dnf|yum)\b", combined):
            return "system-package-install"
        if any(marker in combined for marker in ("could not resolve", "connection timed out", "read timed out", "tls handshake timeout")):
            return "network-transfer"
        if "docker build" in combined or "buildkit" in combined:
            return "image-build"
        return "unknown"

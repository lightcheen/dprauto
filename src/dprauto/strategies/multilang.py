"""Deterministic JVM and native container build strategies."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import PurePosixPath

from dprauto.adapters.multilang.dependencies import validated_native_system_packages
from dprauto.command_semantics import GRADLE_PROXY_EXECUTABLE
from dprauto.config import BuildConfig
from dprauto.domain.enums import BuildStage, CommandPurpose, ProjectType
from dprauto.domain.models import (
    BuildPlan,
    BuildResult,
    BuildStep,
    CommandSpec,
    GeneratedFile,
    ProjectProfile,
)
from dprauto.proxy import docker_proxy_build_arguments
from dprauto.strategies.common import (
    GENERATED_FILE_PREFIX,
    RecordedBuildRunner,
    stable_image_reference,
    stable_plan_id,
)
from dprauto.strategies.context import generated_dockerignore

JVM_RUNTIME_MARKER = "DPRAUTO_JVM_ARTIFACT_OK"
NATIVE_RUNTIME_MARKER = "DPRAUTO_NATIVE_BUILD_OK"
GRADLE_WRAPPER_NETWORK_TIMEOUT_MILLISECONDS = 120_000
GRADLE_WRAPPER_PREFETCH_ATTEMPTS = 3
GRADLE_WRAPPER_RETRY_DELAY_SECONDS = 5


def _container_workdir(profile: ProjectProfile) -> str:
    build_root = str(profile.metadata.get("build_root", ".")).strip().strip("/")
    return "/workspace" if not build_root or build_root == "." else f"/workspace/{build_root}"


def _build_file_names(profile: ProjectProfile) -> set[str]:
    return {PurePosixPath(path).name for path in profile.build_files}


def _docker_build_command(config: BuildConfig, image: str) -> CommandSpec:
    argv = [
        config.docker_binary,
        "build",
        "--file",
        f"{GENERATED_FILE_PREFIX}Dockerfile",
        "--tag",
        image,
    ]
    if not config.use_cache:
        argv.append("--no-cache")
    if not config.allow_network:
        argv.extend(("--network", "none"))
    elif config.docker_network:
        argv.extend(("--network", config.docker_network))
    argv.extend(docker_proxy_build_arguments(config))
    argv.append(".")
    return CommandSpec(
        tuple(argv),
        purpose=CommandPurpose.BUILD,
        timeout_seconds=config.timeout_seconds,
    )


def _plan(
    strategy: str,
    profile: ProjectProfile,
    config: BuildConfig,
    dockerfile: str,
    metadata: dict[str, object],
) -> BuildPlan:
    image = stable_image_reference(profile, config.image_repository)
    generated = (
        GeneratedFile("Dockerfile", dockerfile, media_type="text/x-dockerfile"),
        # Docker gives a Dockerfile-specific ignore file precedence over the
        # repository root .dockerignore. Project-owned ignore rules are often
        # tailored to a different Dockerfile and may exclude pom.xml,
        # CMakeLists.txt, or other inputs required by this generated template.
        generated_dockerignore(profile),
    )
    command = _docker_build_command(config, image)
    complete_metadata = {
        "image_reference": image,
        "dockerfile": "Dockerfile",
        "proxy_environment_forwarded": (
            config.allow_network and config.forward_proxy_environment
        ),
        **metadata,
    }
    payload = {
        "command": command,
        "generated_files": generated,
        "metadata": complete_metadata,
    }
    return BuildPlan(
        plan_id=stable_plan_id(strategy, profile, payload),
        project_id=profile.project_id,
        strategy=strategy,
        steps=(BuildStep("docker-build", BuildStage.BUILD, command),),
        network_allowed=config.allow_network,
        cache_enabled=config.use_cache,
        metadata=complete_metadata,
        generated_files=generated,
    )


class JVMTemplateStrategy:
    """Build Maven or Gradle projects from parser-owned root markers only."""

    name = "jvm-template"
    _SYSTEMS = frozenset({"maven", "gradle"})

    def __init__(self, runner: RecordedBuildRunner, config: BuildConfig | None = None) -> None:
        self.runner = runner
        self.config = config or BuildConfig()

    def supports(self, profile: ProjectProfile) -> bool:
        languages = {language.casefold() for language in profile.languages}
        return bool(languages & {"java", "kotlin", "groovy"}) and (
            self._build_system(profile) in self._SYSTEMS
        )

    def create_plan(self, profile: ProjectProfile) -> BuildPlan:
        system = self._build_system(profile)
        if system not in self._SYSTEMS:
            raise ValueError(f"unsupported JVM build system: {system or 'missing'}")
        java_version = self._java_version(profile)
        wrapper = self._wrapper(profile, system)
        toolchain_version = self._gradle_toolchain_version(
            profile,
            system,
            java_version,
        )
        toolchain_base_image = (
            self.config.maven_base_image.format(version=toolchain_version)
            if toolchain_version
            else ""
        )
        license_skip = ""
        git_hook_source = ""
        if system == "maven":
            base_image = self.config.maven_base_image.format(version=java_version)
            executable = "./mvnw" if wrapper else "mvn"
            license_skip = self._maven_license_skip(profile)
            license_option = " -Dlicense.skip=true" if license_skip else ""
            git_hook_source = str(
                profile.metadata.get("maven_git_hook_install_source", "")
            )
            git_hook_option = (
                " -Dgitbuildhook.install.skip=true" if git_hook_source else ""
            )
            build_command = (
                f"{executable} -B -Dmaven.test.skip=true"
                f"{license_option}{git_hook_option} package"
            )
            artifact_glob = "*/target/*.jar"
            prefetch_command = ""
        else:
            base_image = self.config.gradle_base_image.format(version=java_version)
            executable = "./gradlew" if wrapper else "gradle"
            gradle_launcher = (
                f"{GRADLE_PROXY_EXECUTABLE} {executable}" if wrapper else executable
            )
            build_command = f"{gradle_launcher} --no-daemon assemble"
            artifact_glob = "*/build/libs/*.jar"
            prefetch_command = self._gradle_wrapper_prefetch(executable) if wrapper else ""
        probe = self._runtime_probe(artifact_glob)
        lines = ["# syntax=docker/dockerfile:1"] if self.config.use_cache else []
        if toolchain_base_image:
            lines.append(f"FROM {toolchain_base_image} AS dprauto-jvm-toolchain")
        lines.extend([
            f"FROM {base_image}",
            "USER root",
            "ENTRYPOINT []",
            f"WORKDIR {_container_workdir(profile)}",
            "COPY . /workspace",
        ])
        if toolchain_version:
            toolchain_path = f"/opt/dprauto-jdks/temurin-{toolchain_version}"
            lines.extend(
                (
                    "COPY --from=dprauto-jvm-toolchain "
                    f"/opt/java/openjdk {toolchain_path}",
                    "RUN printf '\\norg.gradle.java.installations.paths="
                    f"{toolchain_path}\\n' >> gradle.properties",
                )
            )
        if system == "maven" and wrapper:
            # Some Maven Wrapper scripts expand MAVEN_CONFIG as CLI arguments.
            # The official Maven image sets it to /root/.m2 for the system Maven
            # launcher, which an older wrapper interprets as a lifecycle phase.
            lines.append('ENV MAVEN_CONFIG=""')
        if system == "gradle" and wrapper:
            lines.extend(
                (
                    "ENV GRADLE_USER_HOME=/opt/dprauto-gradle",
                    self._gradle_proxy_instruction(),
                    self._gradle_wrapper_timeout_instruction(),
                )
            )
        if wrapper:
            lines.append(f"RUN chmod +x {executable}")
        if prefetch_command:
            lines.append(f"RUN {prefetch_command}")
        lines.extend(
            (
                # Keep resolved dependencies in the image so later Testability
                # can execute without reconstructing a separate JVM environment.
                f"RUN {build_command}",
                f"CMD {json.dumps(['/bin/sh', '-lc', probe])}",
            )
        )
        dockerfile = "\n".join(lines) + "\n"
        return _plan(
            self.name,
            profile,
            self.config,
            dockerfile,
            {
                "base_image": base_image,
                "runtime_base_image": base_image,
                "language_family": "jvm",
                "build_system": system,
                "java_version": java_version,
                "java_toolchain_version": toolchain_version,
                "java_toolchain_base_image": toolchain_base_image,
                "build_command": build_command,
                "build_command_source": f"deterministic:{system}-root-marker",
                "dependency_installation_commands": (build_command,),
                "runtime_probe_command": probe,
                "runtime_probe_marker": JVM_RUNTIME_MARKER,
                "runtime_probe_type": "jvm-jar-classes",
                "wrapper": wrapper,
                "maven_config_isolated": system == "maven" and wrapper,
                "maven_license_skip_evidence": license_skip,
                "maven_git_hook_skip_evidence": git_hook_source,
                "wrapper_distribution_prefetch_command": prefetch_command,
                "wrapper_network_timeout_milliseconds": (
                    GRADLE_WRAPPER_NETWORK_TIMEOUT_MILLISECONDS
                    if system == "gradle" and wrapper
                    else 0
                ),
                "wrapper_prefetch_attempts": (
                    GRADLE_WRAPPER_PREFETCH_ATTEMPTS
                    if system == "gradle" and wrapper
                    else 0
                ),
                "gradle_proxy_launcher": (
                    GRADLE_PROXY_EXECUTABLE if system == "gradle" and wrapper else ""
                ),
                "dependency_state_retained_in_image": True,
            },
        )

    def build(
        self,
        plan: BuildPlan,
        workspace,
        *,
        deadline_at: datetime | None = None,
    ) -> BuildResult:
        return self.runner.run(plan, workspace, deadline_at=deadline_at)

    @staticmethod
    def _build_system(profile: ProjectProfile) -> str:
        value = str(profile.metadata.get("primary_build_system", "")).casefold()
        if value:
            return value
        managers = {manager.casefold() for manager in profile.package_managers}
        return next((system for system in ("maven", "gradle") if system in managers), "")

    def _java_version(self, profile: ProjectProfile) -> str:
        constraint = profile.runtime_constraints.get("java", "")
        match = re.search(r"(?<!\d)(?:1\.)?(\d{1,2})(?!\d)", constraint)
        return match.group(1) if match else self.config.default_java_version

    @staticmethod
    def _gradle_toolchain_version(
        profile: ProjectProfile,
        system: str,
        java_version: str,
    ) -> str:
        if system != "gradle":
            return ""
        evidence = str(profile.metadata.get("java_target_version_evidence", ""))
        target = str(profile.metadata.get("java_target_version", ""))
        if evidence.endswith(":toolchain") and target and target != java_version:
            return target
        return ""

    @staticmethod
    def _wrapper(profile: ProjectProfile, system: str) -> bool:
        expected = "mvnw" if system == "maven" else "gradlew"
        return expected in _build_file_names(profile)

    @staticmethod
    def _maven_license_skip(profile: ProjectProfile) -> str:
        for command in profile.commands:
            display = command.command.display
            if re.search(
                r"\bmvnw?\b|(?:^|\s)\./mvnw\b",
                display,
                re.IGNORECASE,
            ) and re.search(
                r"-D[\"']?license\.skip(?:[\"']?=true)?\b",
                display,
                re.IGNORECASE,
            ):
                return command.source
        return ""

    @staticmethod
    def _gradle_wrapper_timeout_instruction() -> str:
        path = "gradle/wrapper/gradle-wrapper.properties"
        timeout = GRADLE_WRAPPER_NETWORK_TIMEOUT_MILLISECONDS
        return (
            f"RUN if grep -q '^networkTimeout=' {path}; then "
            f"sed -i 's/^networkTimeout=.*/networkTimeout={timeout}/' {path}; "
            f"else printf '\\nnetworkTimeout={timeout}\\n' >> {path}; fi"
        )

    @staticmethod
    def _gradle_proxy_instruction() -> str:
        lines = (
            "#!/bin/sh",
            "proxy=${HTTPS_PROXY:-${https_proxy:-${HTTP_PROXY:-${http_proxy:-}}}}",
            'if [ -n "$proxy" ]; then',
            "  authority=${proxy#*://}",
            "  authority=${authority%%/*}",
            "  hostport=${authority##*@}",
            "  host=${hostport%:*}",
            "  port=${hostport##*:}",
            '  if [ "$host" != "$hostport" ]; then',
            '    JAVA_TOOL_OPTIONS="${JAVA_TOOL_OPTIONS:+$JAVA_TOOL_OPTIONS }'
            '-Dhttps.proxyHost=$host -Dhttps.proxyPort=$port '
            '-Dhttp.proxyHost=$host -Dhttp.proxyPort=$port"',
            "    export JAVA_TOOL_OPTIONS",
            "  fi",
            "fi",
            'exec "$@"',
        )
        quoted = " ".join("'" + line.replace("'", "'\\''") + "'" for line in lines)
        return (
            f"RUN printf '%s\\n' {quoted} > {GRADLE_PROXY_EXECUTABLE} && "
            f"chmod +x {GRADLE_PROXY_EXECUTABLE}"
        )

    @staticmethod
    def _gradle_wrapper_prefetch(executable: str) -> str:
        attempts = GRADLE_WRAPPER_PREFETCH_ATTEMPTS
        delay = GRADLE_WRAPPER_RETRY_DELAY_SECONDS
        cache = "/var/cache/dprauto-gradle"
        return (
            f"--mount=type=cache,id=dprauto-gradle-wrapper,target={cache},sharing=locked "
            f"mkdir -p \"$GRADLE_USER_HOME\" {cache}; "
            f"cp -a {cache}/. \"$GRADLE_USER_HOME/\" 2>/dev/null || true; "
            f"for attempt in $(seq 1 {attempts}); do "
            'echo "DPRAUTO_GRADLE_PREFETCH_ATTEMPT=$attempt"; '
            f"{GRADLE_PROXY_EXECUTABLE} {executable} --no-daemon --version && "
            f"{{ cp -a \"$GRADLE_USER_HOME/.\" {cache}/; exit 0; }}; "
            f"cp -a \"$GRADLE_USER_HOME/.\" {cache}/ 2>/dev/null || true; "
            f"test \"$attempt\" -lt {attempts} || exit 1; sleep {delay}; "
            "done"
        )

    @staticmethod
    def _runtime_probe(artifact_glob: str) -> str:
        return (
            "artifact=$(find /workspace -type f -path '"
            + artifact_glob
            + "' ! -name '*-sources.jar' ! -name '*-javadoc.jar' | sort | head -n 1); "
            "test -n \"$artifact\"; "
            "count=$(jar tf \"$artifact\" | grep -c '\\.class$' || true); "
            "test \"$count\" -gt 0; "
            f"echo {JVM_RUNTIME_MARKER}; echo DPRAUTO_API_COUNT=$count"
        )


class NativeTemplateStrategy:
    """Build C/C++ roots with a bounded toolchain and fixed build pipelines."""

    name = "native-template"
    _SYSTEMS = frozenset({"cmake", "meson", "autotools", "make"})
    def __init__(self, runner: RecordedBuildRunner, config: BuildConfig | None = None) -> None:
        self.runner = runner
        self.config = config or BuildConfig()

    def supports(self, profile: ProjectProfile) -> bool:
        languages = {language.casefold() for language in profile.languages}
        return bool(languages & {"c", "c++", "cpp"}) and (
            self._build_system(profile) in self._SYSTEMS
        )

    def create_plan(self, profile: ProjectProfile) -> BuildPlan:
        system = self._build_system(profile)
        if system not in self._SYSTEMS:
            raise ValueError(f"unsupported native build system: {system or 'missing'}")
        packages = self._packages(profile, system)
        install_command = self._install_command(packages)
        build_commands = self._build_commands(profile, system)
        probe, probe_type = self._runtime_probe(profile, system)
        lines = (["# syntax=docker/dockerfile:1"] if self.config.use_cache else []) + [
            f"FROM {self.config.native_base_image}",
            "USER root",
            "ENTRYPOINT []",
            f"WORKDIR {_container_workdir(profile)}",
        ]
        apt_mount = (
            "--mount=type=cache,id=dprauto-apt-cache,target=/var/cache/apt,sharing=locked "
            "--mount=type=cache,id=dprauto-apt-lists,target=/var/lib/apt,sharing=locked "
            if self.config.use_cache
            else ""
        )
        lines.extend(
            (
                f"RUN {apt_mount}{install_command}",
                "COPY . /workspace",
                *(f"RUN {command}" for command in build_commands),
                f"CMD {json.dumps(['/bin/sh', '-lc', probe])}",
            )
        )
        dockerfile = "\n".join(lines) + "\n"
        return _plan(
            self.name,
            profile,
            self.config,
            dockerfile,
            {
                "base_image": self.config.native_base_image,
                "runtime_base_image": self.config.native_base_image,
                "language_family": "native",
                "build_system": system,
                "system_packages": packages,
                "build_commands": build_commands,
                "build_command_source": f"deterministic:{system}-root-marker",
                "dependency_installation_commands": (install_command,),
                "runtime_probe_command": probe,
                "runtime_probe_marker": NATIVE_RUNTIME_MARKER,
                "runtime_probe_type": probe_type,
                "max_build_jobs": self.config.max_build_jobs,
            },
        )

    def build(
        self,
        plan: BuildPlan,
        workspace,
        *,
        deadline_at: datetime | None = None,
    ) -> BuildResult:
        return self.runner.run(plan, workspace, deadline_at=deadline_at)

    @staticmethod
    def _build_system(profile: ProjectProfile) -> str:
        value = str(profile.metadata.get("primary_build_system", "")).casefold()
        if value:
            return value
        managers = {manager.casefold() for manager in profile.package_managers}
        return next(
            (system for system in ("cmake", "meson", "autotools", "make") if system in managers),
            "",
        )

    @staticmethod
    def _packages(profile: ProjectProfile, system: str) -> tuple[str, ...]:
        languages = {language.casefold() for language in profile.languages}
        compiler = "g++" if languages & {"c++", "cpp"} else "gcc"
        packages = ["ca-certificates", compiler, "make", "pkg-config"]
        if system == "cmake":
            packages.append("cmake")
        elif system == "meson":
            packages.extend(("meson", "ninja-build", "python3"))
        elif system == "autotools":
            root_files = _build_file_names(profile)
            if "configure" not in root_files or "autogen.sh" in root_files:
                packages.extend(("autoconf", "automake", "libtool"))
        packages.extend(validated_native_system_packages(profile.metadata)[:24])
        return tuple(dict.fromkeys(packages))

    def _install_command(self, packages: tuple[str, ...]) -> str:
        cleanup = "" if self.config.use_cache else " && rm -rf /var/lib/apt/lists/*"
        apt_install = (
            "export DEBIAN_FRONTEND=noninteractive; "
            "rm -f /etc/apt/apt.conf.d/docker-clean; "
            "apt-get -o Acquire::Retries=2 -o Acquire::http::Timeout=30 "
            "-o Acquire::https::Timeout=30 update && "
            "apt-get -o Acquire::Retries=2 -o Acquire::http::Timeout=30 "
            "-o Acquire::https::Timeout=30 install -y --no-install-recommends "
            + " ".join(packages)
            + cleanup
        )
        package_check = " && ".join(
            "dpkg-query -W -f='${db:Status-Abbrev}' "
            f"{package} 2>/dev/null | grep -q '^ii '"
            for package in packages
        )
        return (
            f"if {package_check}; then "
            "echo 'native build dependencies already present'; "
            f"else {apt_install}; fi"
        )

    def _build_commands(self, profile: ProjectProfile, system: str) -> tuple[str, ...]:
        jobs = self.config.max_build_jobs
        if system == "cmake":
            raw_arguments = profile.metadata.get("cmake_configuration_arguments", ())
            arguments = (
                tuple(
                    value
                    for value in raw_arguments[:16]
                    if isinstance(value, str)
                    and re.fullmatch(
                        r"-D[A-Za-z_][A-Za-z0-9_]{0,63}=(?:ON|OFF|DOWNLOAD)",
                        value,
                    )
                )
                if isinstance(raw_arguments, (list, tuple))
                else ()
            )
            configure = "cmake -S . -B build"
            if arguments:
                configure += " " + " ".join(arguments)
            build = "cmake --build build"
            build += f" --parallel {jobs}"
            return (configure, build)
        if system == "meson":
            return ("meson setup build", f"meson compile -C build -j {jobs}")
        if system == "autotools":
            root_files = _build_file_names(profile)
            if "autogen.sh" in root_files:
                configure = (
                    "chmod +x ./autogen.sh && ./autogen.sh && "
                    "if [ ! -f Makefile ]; then chmod +x ./configure && ./configure; fi"
                )
            elif "configure" in root_files:
                configure = "chmod +x ./configure && ./configure"
            else:
                configure = "autoreconf -fi && ./configure"
            return (configure, f"make -j{jobs}")
        target = str(profile.metadata.get("make_build_target", ""))
        if target not in {"all", "build"}:
            target = ""
        return (f"make -j{jobs}" + (f" {target}" if target else ""),)

    @staticmethod
    def _runtime_probe(profile: ProjectProfile, system: str) -> tuple[str, str]:
        if profile.project_type is ProjectType.CLI:
            candidates = tuple(
                item
                for item in profile.commands
                if item.command.purpose is CommandPurpose.RUN
                and re.fullmatch(
                    r"(?:\./|build/)[A-Za-z0-9_.+-]+"
                    r"(?: [A-Za-z0-9_.+:/=-]+){1,8}",
                    item.command.display,
                )
            )
            selected = max(candidates, key=lambda item: item.confidence, default=None)
            command = selected.command.display if selected is not None else ""
            if command:
                return (
                    f"{command} && echo {NATIVE_RUNTIME_MARKER} && echo DPRAUTO_CLI_COMMAND_OK=1",
                    "native-cli-command",
                )
        build_directory = "build" if system in {"cmake", "meson"} else "."
        return (
            (
                f"test -d {build_directory} && "
                f"count=$(find {build_directory} -type f "
                "\\( -name '*.a' -o -name '*.so' -o -name '*.dylib' \\) "
                "| wc -l) && test \"$count\" -gt 0 && "
                f"echo {NATIVE_RUNTIME_MARKER} && echo DPRAUTO_ARTIFACT_COUNT=$count"
            ),
            "native-build-artifacts",
        )

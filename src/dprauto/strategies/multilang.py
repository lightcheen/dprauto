"""Deterministic JVM and native container build strategies."""

from __future__ import annotations

import json
import re
from datetime import datetime

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

JVM_RUNTIME_MARKER = "DPRAUTO_JVM_ARTIFACT_OK"
NATIVE_RUNTIME_MARKER = "DPRAUTO_NATIVE_BUILD_OK"
GENERATED_DOCKERIGNORE = """# DPRAuto template context policy
.git
.dprauto
"""


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
        GeneratedFile(
            "Dockerfile.dockerignore",
            GENERATED_DOCKERIGNORE,
            media_type="text/plain",
        ),
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
        if system == "maven":
            base_image = self.config.maven_base_image.format(version=java_version)
            executable = "./mvnw" if wrapper else "mvn"
            build_command = f"{executable} -B -DskipTests package"
            artifact_glob = "*/target/*.jar"
        else:
            base_image = self.config.gradle_base_image.format(version=java_version)
            executable = "./gradlew" if wrapper else "gradle"
            build_command = f"{executable} --no-daemon assemble"
            artifact_glob = "*/build/libs/*.jar"
        probe = self._runtime_probe(artifact_glob)
        lines = (["# syntax=docker/dockerfile:1"] if self.config.use_cache else []) + [
            f"FROM {base_image}",
            "USER root",
            "ENTRYPOINT []",
            "WORKDIR /workspace",
            "COPY . /workspace",
        ]
        if wrapper:
            lines.append(f"RUN chmod +x {executable}")
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
                "build_command": build_command,
                "build_command_source": f"deterministic:{system}-root-marker",
                "dependency_installation_commands": (build_command,),
                "runtime_probe_command": probe,
                "runtime_probe_marker": JVM_RUNTIME_MARKER,
                "runtime_probe_type": "jvm-jar-classes",
                "wrapper": wrapper,
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
    def _wrapper(profile: ProjectProfile, system: str) -> bool:
        expected = "mvnw" if system == "maven" else "gradlew"
        return expected in profile.build_files

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
    _EVIDENCE_PACKAGES = frozenset(
        {"liblzma-dev", "libpopt-dev", "libssl-dev", "python3", "zlib1g-dev"}
    )

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
            "WORKDIR /workspace",
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
            root_files = set(profile.build_files)
            if "configure" not in root_files or "autogen.sh" in root_files:
                packages.extend(("autoconf", "automake", "libtool"))
        evidence_packages = profile.metadata.get("system_dependency_packages", ())
        if isinstance(evidence_packages, (list, tuple)):
            packages.extend(
                package
                for package in evidence_packages[:16]
                if isinstance(package, str)
                and package in NativeTemplateStrategy._EVIDENCE_PACKAGES
            )
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
            root_files = set(profile.build_files)
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
        return (f"make -j{jobs}",)

    @staticmethod
    def _runtime_probe(profile: ProjectProfile, system: str) -> tuple[str, str]:
        if profile.project_type is ProjectType.CLI:
            command = next(
                (
                    item.command.display
                    for item in profile.commands
                    if item.command.purpose is CommandPurpose.RUN
                    and item.source == "inferred:root-executable-target"
                    and re.fullmatch(
                        r"(?:\./|build/)[A-Za-z0-9_.+-]+ (?:--version|--help|-h)",
                        item.command.display,
                    )
                ),
                "",
            )
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
                "\\( -perm -111 -o -name '*.a' -o -name '*.so' -o -name '*.dylib' \\) "
                "| wc -l) && test \"$count\" -gt 0 && "
                f"echo {NATIVE_RUNTIME_MARKER} && echo DPRAUTO_ARTIFACT_COUNT=$count"
            ),
            "native-build-artifacts",
        )

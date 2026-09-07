"""Generated Dockerfile strategy for standard Python projects."""

from __future__ import annotations

import json
import re
import shlex
from datetime import datetime
from pathlib import PurePosixPath

from dprauto.config import BuildConfig
from dprauto.command_semantics import select_run_command
from dprauto.domain.enums import BuildStage, CommandPurpose
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


def poetry_tool_image_reference(config: BuildConfig, python_version: str) -> str:
    """Resolve the versioned Poetry tool image configured by the deployment."""

    if not config.poetry_tool_image:
        return ""
    return config.poetry_tool_image.format(
        version=python_version,
        poetry_version=config.poetry_version,
    )


class TemplateStrategy:
    """Create a conservative Python image from normalized project metadata."""

    name = "template"

    def __init__(self, runner: RecordedBuildRunner, config: BuildConfig | None = None) -> None:
        self.runner = runner
        self.config = config or BuildConfig()

    def supports(self, profile: ProjectProfile) -> bool:
        return any(language.lower() == "python" for language in profile.languages)

    def create_plan(self, profile: ProjectProfile) -> BuildPlan:
        version = self._python_version(profile)
        runtime_base_image = self.config.python_base_image.format(version=version)
        tool_image = (
            poetry_tool_image_reference(self.config, version)
            if "poetry" in profile.package_managers
            else ""
        )
        base_image = tool_image or runtime_base_image
        setup_commands = self._setup_commands(profile)
        scm_pretend_version = self._scm_pretend_version(profile)
        deterministic_fixes = self._deterministic_fixes(profile, scm_pretend_version)
        setup_lines = ["#!/bin/sh", "set -eu"]
        if scm_pretend_version:
            key, value = scm_pretend_version
            setup_lines.append(f"export {key}={value}")
        setup_lines.extend(setup_commands)
        setup_script = "\n".join(setup_lines) + "\n"
        dockerfile = self._dockerfile(
            profile,
            base_image,
            setup_commands,
            scm_pretend_version=scm_pretend_version,
            use_cache=self.config.use_cache,
        )
        image = stable_image_reference(profile, self.config.image_repository)
        generated_files = (
            GeneratedFile("Dockerfile", dockerfile, media_type="text/x-dockerfile"),
            generated_dockerignore(profile),
            GeneratedFile(
                "setup.sh",
                setup_script,
                executable=True,
                media_type="text/x-shellscript",
            ),
        )
        argv = [
            self.config.docker_binary,
            "build",
            "--file",
            f"{GENERATED_FILE_PREFIX}Dockerfile",
            "--tag",
            image,
        ]
        if not self.config.use_cache:
            argv.append("--no-cache")
        if not self.config.allow_network:
            argv.extend(("--network", "none"))
        elif self.config.docker_network:
            argv.extend(("--network", self.config.docker_network))
        argv.extend(docker_proxy_build_arguments(self.config))
        argv.append(".")
        command = CommandSpec(
            tuple(argv),
            purpose=CommandPurpose.BUILD,
            timeout_seconds=self.config.timeout_seconds,
        )
        payload = {"command": command, "generated_files": generated_files, "image": image}
        return BuildPlan(
            plan_id=stable_plan_id(self.name, profile, payload),
            project_id=profile.project_id,
            strategy=self.name,
            steps=(BuildStep("docker-build", BuildStage.BUILD, command),),
            network_allowed=self.config.allow_network,
            cache_enabled=self.config.use_cache,
            metadata={
                "image_reference": image,
                "base_image": base_image,
                "runtime_base_image": runtime_base_image,
                "poetry_version": self.config.poetry_version,
                "proxy_environment_forwarded": (
                    self.config.allow_network
                    and self.config.forward_proxy_environment
                ),
                "dockerfile": "Dockerfile",
                "setup_script": "setup.sh",
                "dependency_installation_commands": setup_commands,
                "deterministic_fixes": deterministic_fixes,
                "scm_pretend_version": (
                    dict((scm_pretend_version,)) if scm_pretend_version else {}
                ),
                "scm_pretend_version_source": (
                    str(profile.metadata.get("scm_versioning", {}).get("version_source", ""))
                    if scm_pretend_version
                    and isinstance(profile.metadata.get("scm_versioning", {}), dict)
                    else ""
                ),
            },
            generated_files=generated_files,
        )

    def build(
        self,
        plan: BuildPlan,
        workspace,
        *,
        deadline_at: datetime | None = None,
    ) -> BuildResult:
        return self.runner.run(plan, workspace, deadline_at=deadline_at)

    def _python_version(self, profile: ProjectProfile) -> str:
        return self.resolve_python_version(
            profile.runtime_constraints.get("python", ""),
            self.config.default_python_version,
        )

    @classmethod
    def resolve_python_version(cls, constraint: str, default: str) -> str:
        if not constraint:
            return default
        if cls._version_satisfies(default, constraint):
            return default
        versions = tuple(
            dict.fromkeys(re.findall(r"(?<!\d)(3\.\d{1,2})(?:\.\d+)?", constraint))
        )
        compatible = [value for value in versions if cls._version_satisfies(value, constraint)]
        if compatible:
            return max(compatible, key=cls._version_tuple)
        # A strict upper bound such as ``<3.10`` contains no directly usable
        # version token.  Select the immediately preceding Python minor rather
        # than returning the excluded boundary itself.
        upper_bounds = re.findall(r"(<|<=)\s*(3\.\d{1,2})(?:\.\d+)?", constraint)
        inferred: list[str] = []
        for operator, value in upper_bounds:
            major, minor, _ = cls._version_tuple(value)
            if operator == "<":
                minor -= 1
            if minor >= 0:
                inferred.append(f"{major}.{minor}")
        inferred_compatible = [
            value for value in inferred if cls._version_satisfies(value, constraint)
        ]
        if inferred_compatible:
            return max(inferred_compatible, key=cls._version_tuple)
        return versions[0] if versions else default

    @classmethod
    def _version_satisfies(cls, version: str, constraint: str) -> bool:
        current = cls._version_tuple(version)
        normalized = constraint.replace(" ", "")
        if normalized.startswith("^"):
            lower = cls._version_tuple(normalized[1:])
            return current >= lower and current[0] == lower[0]
        if normalized.startswith("~") and not normalized.startswith("~="):
            lower = cls._version_tuple(normalized[1:])
            return current >= lower and current[:2] == lower[:2]
        comparisons = re.findall(r"(>=|<=|==|!=|>|<|~=)\s*(3\.\d{1,2}(?:\.\d+)?)", constraint)
        if comparisons:
            for operator, value in comparisons:
                expected = cls._version_tuple(value)
                if operator == ">=" and not current >= expected:
                    return False
                if operator == ">" and not current > expected:
                    return False
                if operator == "<=" and not current <= expected:
                    return False
                if operator == "<" and not current < expected:
                    return False
                if operator == "==" and not current[:2] == expected[:2]:
                    return False
                if operator == "!=" and current[:2] == expected[:2]:
                    return False
                if operator == "~=":
                    specified_parts = re.findall(r"\d+", value)
                    if len(specified_parts) >= 3:
                        # A minor Docker tag denotes the latest patch in that
                        # series, so ``python:3.8`` is the right image family
                        # for a constraint such as ``~=3.8.1``.
                        if current[:2] != expected[:2]:
                            return False
                    elif not (current >= expected and current[0] == expected[0]):
                        return False
            return True
        listed = re.findall(r"(?<!\d)(3\.\d{1,2})(?:\.\d+)?", normalized)
        return not listed or version in listed

    @staticmethod
    def _version_tuple(value: str) -> tuple[int, int, int]:
        parts = [int(item) for item in re.findall(r"\d+", value)[:3]]
        return tuple((parts + [0, 0, 0])[:3])

    def _setup_commands(self, profile: ProjectProfile) -> tuple[str, ...]:
        files_by_name = {
            PurePosixPath(path).name.lower(): path for path in profile.dependency_files
        }
        managers = set(profile.package_managers)
        system_commands = self._system_dependency_commands(profile)
        existing_setup = next(
            (
                path
                for path in profile.build_files
                if PurePosixPath(path).name.lower() == "setup.sh"
            ),
            "",
        )
        if existing_setup:
            return (*system_commands, f"sh {shlex.quote(existing_setup)}")
        include_test_dependencies = (
            profile.metadata.get("include_test_dependencies_in_build") is True
        )
        if "uv" in managers:
            return (
                *system_commands,
                "python -m pip install uv",
                (
                    "uv sync --frozen --no-editable"
                    if include_test_dependencies
                    else "uv sync --frozen --no-default-groups --no-editable"
                ),
            )
        if "poetry" in managers:
            commands = list(system_commands)
            if not self.config.poetry_tool_image:
                commands.append(
                    "python -m pip install "
                    f"poetry=={shlex.quote(self.config.poetry_version)}"
                )
            commands.append(self._poetry_lock_preflight_command())
            commands.append(
                "poetry install --no-interaction --no-ansi"
                if include_test_dependencies
                else "poetry install --only main --no-interaction --no-ansi"
            )
            return tuple(commands)
        if "pdm" in managers:
            pdm_command = (
                "pdm sync"
                if any(
                    PurePosixPath(path).name.casefold() == "pdm.lock"
                    for path in profile.dependency_files
                )
                else "pdm install"
            )
            return (
                *system_commands,
                "python -m pip install pdm",
                (
                    f"{pdm_command} --no-editable"
                    if include_test_dependencies
                    else f"{pdm_command} --prod --no-editable"
                ),
            )
        if "pipenv" in managers:
            return (
                *system_commands,
                "python -m pip install pipenv",
                (
                    "pipenv sync --dev --system"
                    if include_test_dependencies
                    else "pipenv sync --system"
                ),
            )
        requirements = files_by_name.get("requirements.txt")
        build_names = {PurePosixPath(path).name.lower() for path in profile.build_files}
        commands: list[str] = list(system_commands)
        if requirements:
            commands.append(
                f"python -m pip install -r {shlex.quote(requirements)}"
            )
        if build_names & {"pyproject.toml", "setup.py", "setup.cfg"}:
            commands.append("python -m pip install .")
        if include_test_dependencies:
            commands.extend(TemplateStrategy._test_dependency_commands(profile, requirements))
        return tuple(commands) or ("python --version",)

    def _poetry_lock_preflight_command(self) -> str:
        try:
            major = int(self.config.poetry_version.split(".", 1)[0])
        except ValueError:
            major = 1
        refresh = "poetry lock" if major >= 2 else "poetry lock --no-update"
        return (
            "poetry check --lock --no-interaction --no-ansi || "
            f"{refresh} --no-interaction --no-ansi"
        )

    def _system_dependency_commands(self, profile: ProjectProfile) -> tuple[str, ...]:
        hints = set(profile.metadata.get("system_dependency_hints", ()))
        apt_packages: list[str] = []
        apk_packages: list[str] = []
        if "git-vcs" in hints:
            apt_packages.extend(("ca-certificates", "git"))
            apk_packages.extend(("ca-certificates", "git"))
        if "pyscard-native" in hints:
            apt_packages.extend(("gcc", "libc6-dev", "libpcsclite-dev", "swig"))
            apk_packages.extend(("gcc", "musl-dev", "pcsc-lite-dev", "swig"))
        if "tmux-executable" in hints:
            apt_packages.append("tmux")
            apk_packages.append("tmux")
        apt_packages = list(dict.fromkeys(apt_packages))
        apk_packages = list(dict.fromkeys(apk_packages))
        if not apt_packages:
            return ()
        cleanup = (
            ""
            if self.config.use_cache
            else " && rm -rf /var/lib/apt/lists/*"
        )
        command = (
            "if command -v apt-get >/dev/null 2>&1; then "
            "export DEBIAN_FRONTEND=noninteractive; "
            "rm -f /etc/apt/apt.conf.d/docker-clean; "
            "apt-get -o Acquire::Retries=2 -o Acquire::http::Timeout=30 "
            "-o Acquire::https::Timeout=30 update && "
            "apt-get -o Acquire::Retries=2 -o Acquire::http::Timeout=30 "
            "-o Acquire::https::Timeout=30 install -y --no-install-recommends "
            + " ".join(apt_packages)
            + cleanup
            + "; elif command -v apk >/dev/null 2>&1; then apk add --no-cache "
            + " ".join(apk_packages)
            + "; else echo 'deterministic system dependency repair requires apt-get or apk' "
            " >&2; exit 1; fi"
        )
        return (command,)

    @staticmethod
    def _deterministic_fixes(
        profile: ProjectProfile,
        scm_pretend_version: tuple[str, str] | None = None,
    ) -> tuple[str, ...]:
        fixes = [
            f"system-dependency:{hint}"
            for hint in profile.metadata.get("system_dependency_hints", ())
        ]
        if "poetry" in profile.package_managers:
            fixes.append("poetry-lock:validate-or-refresh-without-update")
        if scm_pretend_version:
            scm = profile.metadata.get("scm_versioning", {})
            source = scm.get("version_source", "") if isinstance(scm, dict) else ""
            fixes.append(
                "setuptools-scm:pretend-version-from-project-evidence"
                if source
                else "setuptools-scm:pretend-version-from-revision"
            )
        return tuple(fixes)

    @staticmethod
    def _scm_pretend_version(profile: ProjectProfile) -> tuple[str, str] | None:
        scm = profile.metadata.get("scm_versioning", {})
        if not isinstance(scm, dict) or scm.get("provider") != "setuptools-scm":
            return None
        project_name = profile.metadata.get("project_name", "")
        revision = profile.source.revision or ""
        if profile.metadata.get("project_name_declared") is not True:
            return None
        if (
            not isinstance(project_name, str)
            or len(project_name) > 128
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", project_name)
        ):
            return None
        if not re.fullmatch(r"[0-9A-Fa-f]{7,64}", revision):
            return None
        normalized_name = re.sub(r"[-_.]+", "_", project_name).upper()
        source_version = scm.get("version", "")
        version_kind = scm.get("version_kind", "")
        if isinstance(source_version, str) and re.fullmatch(
            r"(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){1,3}"
            r"(?:(?:a|b|rc)[0-9]+|\.post[0-9]+|\.dev[0-9]+)?"
            r"(?:\+[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)*)?",
            source_version,
            re.IGNORECASE,
        ):
            pretend_version = (
                f"{source_version}.dev0+g{revision[:12].lower()}"
                if version_kind == "unreleased-changelog"
                else source_version
            )
        else:
            pretend_version = f"0.0+g{revision[:12].lower()}"
        return (
            f"SETUPTOOLS_SCM_PRETEND_VERSION_FOR_{normalized_name}",
            pretend_version,
        )

    @staticmethod
    def _test_dependency_commands(
        profile: ProjectProfile,
        requirements: str | None,
    ) -> tuple[str, ...]:
        commands: list[str] = []
        for path in profile.dependency_files:
            name = PurePosixPath(path).name.lower()
            if (
                path != requirements
                and name.endswith((".txt", ".in"))
                and re.search(r"(?:^|[-_.])(dev|test|tests|testing|tox)(?:[-_.]|$)", name)
            ):
                commands.append(f"python -m pip install -r {shlex.quote(path)}")
        build_names = {PurePosixPath(path).name.lower() for path in profile.build_files}
        if build_names & {"pyproject.toml", "setup.py", "setup.cfg"}:
            groups = tuple(profile.metadata.get("test_dependency_groups", ()))
            if groups:
                target = ".[" + ",".join(groups) + "]"
                commands.append(
                    f"python -m pip install {shlex.quote(target)}"
                )
        test_commands = "\n".join(
            item.command.display
            for item in profile.commands
            if item.command.purpose is CommandPurpose.TEST
        ).lower()
        tools: list[str] = []
        if "tox.ini" in build_names or re.search(r"\btox\b", test_commands):
            tools.append("tox")
        if "noxfile.py" in build_names or re.search(r"\bnox\b", test_commands):
            tools.append("nox")
        if re.search(r"\b(pytest|py\.test)\b", test_commands):
            tools.append("pytest")
        if tools:
            commands.append("python -m pip install " + " ".join(dict.fromkeys(tools)))
        return tuple(commands)

    @staticmethod
    def _dockerfile(
        profile: ProjectProfile,
        base_image: str,
        setup_commands: tuple[str, ...],
        *,
        scm_pretend_version: tuple[str, str] | None = None,
        use_cache: bool,
    ) -> str:
        lines = (["# syntax=docker/dockerfile:1"] if use_cache else []) + [
            f"FROM {base_image}",
            "ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1",
            "WORKDIR /workspace",
            "COPY . /workspace",
        ]
        if scm_pretend_version:
            key, value = scm_pretend_version
            lines.append(f"ENV {key}={value}")
        managers = set(profile.package_managers)
        if "poetry" in managers:
            lines.append("ENV POETRY_VIRTUALENVS_CREATE=false")
        if managers & {"uv", "pdm"}:
            lines.append('ENV PATH="/workspace/.venv/bin:$PATH"')
        for command in setup_commands:
            mounts = ""
            if use_cache:
                mounts = (
                    "--mount=type=cache,id=dprauto-python-packages,"
                    "target=/root/.cache,sharing=locked "
                )
                if "apt-get" in command:
                    mounts += (
                        "--mount=type=cache,id=dprauto-apt-cache,"
                        "target=/var/cache/apt,sharing=locked "
                        "--mount=type=cache,id=dprauto-apt-lists,"
                        "target=/var/lib/apt,sharing=locked "
                    )
            lines.append(f"RUN {mounts}{command}")
        selected = select_run_command(profile)
        if selected is not None:
            lines.append(f"CMD {json.dumps(['/bin/sh', '-lc', selected.command.display])}")
        return "\n".join(lines) + "\n"

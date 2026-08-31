"""Select project-owned test and run commands from normalized profile evidence."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Mapping

from dprauto.adapters.python.test_matrix import matrix_entry, preferred_matrix_name
from dprauto.command_semantics import (
    GRADLE_PROXY_EXECUTABLE,
    is_smoke_command,
    select_run_command,
)
from dprauto.domain.enums import CommandPurpose
from dprauto.domain.models import CommandSpec, ProjectCommand, ProjectProfile

@dataclass(frozen=True, slots=True)
class TestCommandSelection:
    command: ProjectCommand
    original_command: CommandSpec
    kind: str = "project-command"
    targets: tuple[str, ...] = ()
    reason: str = "selected the narrowest local project test command"


class TestCommandSelector:
    """Select one bounded, local project test command."""

    __test__ = False

    def __init__(
        self,
        *,
        max_test_files: int = 8,
        max_parallel_workers: int = 1,
    ) -> None:
        if not 1 <= max_test_files <= 32:
            raise ValueError("max_test_files must be between 1 and 32")
        if not 1 <= max_parallel_workers <= 32:
            raise ValueError("max_parallel_workers must be between 1 and 32")
        self.max_test_files = max_test_files
        self.max_parallel_workers = max_parallel_workers

    def select(
        self,
        profile: ProjectProfile,
        *,
        python_version: str = "",
    ) -> ProjectCommand | None:
        selection = self.select_with_details(
            profile,
            python_version=python_version,
        )
        return selection.command if selection is not None else None

    def select_with_details(
        self,
        profile: ProjectProfile,
        *,
        python_version: str = "",
    ) -> TestCommandSelection | None:
        if self._required_environment(profile):
            return None
        test_files = self._metadata_paths(profile, "test_files")
        safe_files = self._metadata_paths(profile, "safe_test_files")
        if test_files and not safe_files:
            return None
        candidates: list[ProjectCommand] = []
        for item in profile.commands:
            if item.command.purpose is not CommandPurpose.TEST or is_smoke_command(item):
                continue
            normalized = self._without_optional_profile_variables(profile, item)
            if self._has_unresolved_variables(normalized.command.display):
                continue
            if self._unsafe_test_candidate(profile, normalized):
                continue
            candidates.append(normalized)
        if not candidates:
            return None
        selected = min(
            candidates,
            key=lambda item: (
                self._command_rank(item.command.display),
                self._runtime_rank(item.command.display, python_version),
                self._source_rank(profile, item),
                -item.confidence,
            ),
        )
        selected = self._with_jvm_environment_options(profile, selected)
        selected = self._bounded_matrix_command(profile, selected, python_version)
        original = selected.command
        bounded, targets, reason = self._bounded_local_command(profile, selected)
        capture_safe = self._with_pytest_capture_disabled(profile, bounded)
        parallel_safe = self._with_ctest_parallelism(capture_safe)
        kinds: list[str] = []
        if targets:
            kinds.append("bounded-file-slice")
        elif bounded.command != original:
            kinds.append("coverage-normalized")
        if capture_safe.command != bounded.command:
            kinds.append("pytest-capture-disabled")
            reason += (
                "; disabled pytest output capture because project source rewraps "
                "sys.stdout/sys.stderr at import time"
            )
        if parallel_safe.command != capture_safe.command:
            kinds.append("ctest-bounded-parallel")
            reason += f"; bounded CTest concurrency to {self.max_parallel_workers} workers"
        kind = "+".join(kinds) or "project-command"
        return TestCommandSelection(
            parallel_safe,
            original,
            kind,
            targets,
            reason,
        )

    def _with_ctest_parallelism(self, command: ProjectCommand) -> ProjectCommand:
        if self.max_parallel_workers <= 1:
            return command
        try:
            tokens = shlex.split(command.command.display)
        except ValueError:
            return command
        if (
            not tokens
            or PurePosixPath(tokens[0]).name.casefold() != "ctest"
            or any(
                token in {"-j", "--parallel"}
                or token.startswith(("-j", "--parallel="))
                for token in tokens[1:]
            )
        ):
            return command
        spec = CommandSpec(
            (*tokens, "--parallel", str(self.max_parallel_workers)),
            purpose=command.command.purpose,
            cwd=command.command.cwd,
            environment=command.command.environment,
            timeout_seconds=command.command.timeout_seconds,
        )
        return ProjectCommand(
            command.name,
            spec,
            f"parallel-safe:{command.source}",
            command.confidence,
        )

    def _bounded_local_command(
        self,
        profile: ProjectProfile,
        command: ProjectCommand,
    ) -> tuple[ProjectCommand, tuple[str, ...], str]:
        normalized = self._without_coverage_observation(command)
        safe_files = self._metadata_paths(profile, "safe_test_files")
        risk_files = tuple(
            dict.fromkeys(
                (
                    *self._metadata_paths(profile, "external_test_files"),
                    *self._metadata_paths(profile, "unstable_test_files"),
                )
            )
        )
        needs_targets = len(safe_files) > self.max_test_files or bool(risk_files)
        if not needs_targets or not safe_files or command.command.cwd:
            reason = (
                "removed coverage-only observation arguments"
                if normalized.command != command.command
                else "selected the narrowest local project test command"
            )
            return normalized, (), reason
        if self._is_direct_gradle_test(command.command.display):
            bounded = self._bounded_gradle_command(profile, command, safe_files)
            if bounded is not None:
                bounded_command, targets = bounded
                return (
                    bounded_command,
                    targets,
                    (
                        f"selected {len(targets)} stable JVM test class(es) from "
                        f"{len(safe_files)} safe and {len(risk_files)} risk file(s)"
                    ),
                )
        if not self._is_direct_pytest(command.command.display):
            return normalized, (), "selected the narrowest local project test command"
        targets = self._representative_targets(safe_files)
        bounded_spec = CommandSpec(
            ("python", "-m", "pytest", *targets),
            purpose=command.command.purpose,
            environment=command.command.environment,
            timeout_seconds=command.command.timeout_seconds,
        )
        return (
            ProjectCommand(
                command.name,
                bounded_spec,
                f"bounded:{command.source}",
                command.confidence,
            ),
            targets,
            (
                f"selected {len(targets)} local test file(s) from "
                f"{len(safe_files)} safe and {len(risk_files)} risk file(s)"
            ),
        )

    def _bounded_gradle_command(
        self,
        profile: ProjectProfile,
        command: ProjectCommand,
        safe_files: tuple[str, ...],
    ) -> tuple[ProjectCommand, tuple[str, ...]] | None:
        converted = [
            value
            for path in safe_files
            if (value := self._gradle_test_class(path)) is not None
        ]
        if not converted:
            return None
        first_project = converted[0][0]
        same_project = [value for value in converted if value[0] == first_project]
        selected = same_project[: self.max_test_files]
        targets = tuple(path for _project, _class_name, path in selected)
        classes = tuple(class_name for _project, class_name, _path in selected)
        try:
            tokens = shlex.split(command.command.display)
        except ValueError:
            return None
        test_index = next(
            (index for index, token in enumerate(tokens) if token.casefold() == "test"),
            -1,
        )
        if test_index < 0:
            return None
        project_name = first_project
        mapped = profile.metadata.get("gradle_projects_by_directory", {})
        if isinstance(mapped, Mapping):
            value = mapped.get(first_project, "")
            if isinstance(value, str) and value:
                project_name = value
        task = "test" if not project_name else f":{project_name.replace('/', ':')}:test"
        normalized = [*tokens]
        normalized[test_index] = task
        for class_name in classes:
            normalized.extend(("--tests", class_name))
        spec = CommandSpec(
            (shlex.join(normalized),),
            purpose=command.command.purpose,
            cwd=command.command.cwd,
            environment=command.command.environment,
            timeout_seconds=command.command.timeout_seconds,
            shell=True,
        )
        return (
            ProjectCommand(
                command.name,
                spec,
                f"bounded:{command.source}",
                command.confidence,
            ),
            targets,
        )

    @staticmethod
    def _gradle_test_class(path: str) -> tuple[str, str, str] | None:
        match = re.fullmatch(
            r"(?:(.*?)/)?src/test/(?:java|kotlin|groovy)/(.+)\.(?:java|kt|groovy)",
            path,
            re.IGNORECASE,
        )
        if not match:
            return None
        project = (match.group(1) or "").strip("/")
        class_name = match.group(2).replace("/", ".")
        return project, class_name, path

    def _representative_targets(self, paths: tuple[str, ...]) -> tuple[str, ...]:
        groups: dict[str, list[str]] = {}
        for path in paths:
            groups.setdefault(PurePosixPath(path).parent.as_posix(), []).append(path)
        ordered = [
            sorted(groups[parent])
            for parent in sorted(
                groups,
                key=lambda item: (len(PurePosixPath(item).parts), item),
            )
        ]
        selected: list[str] = []
        while len(selected) < self.max_test_files and any(ordered):
            for group in ordered:
                if group and len(selected) < self.max_test_files:
                    selected.append(group.pop(0))
        return tuple(selected)

    @staticmethod
    def _metadata_paths(profile: ProjectProfile, key: str) -> tuple[str, ...]:
        values = profile.metadata.get(key, ())
        if not isinstance(values, (list, tuple)):
            return ()
        selected: list[str] = []
        for value in values[:512]:
            if not isinstance(value, str):
                continue
            path = PurePosixPath(value)
            if (
                path.is_absolute()
                or ".." in path.parts
                or path.suffix.casefold() not in {".py", ".java", ".kt", ".groovy"}
            ):
                continue
            selected.append(path.as_posix())
        return tuple(dict.fromkeys(selected))

    @staticmethod
    def _required_environment(profile: ProjectProfile) -> tuple[str, ...]:
        values = profile.metadata.get("test_required_environment_variables", ())
        if not isinstance(values, (list, tuple)):
            return ()
        supplied = profile.metadata.get("test_environment_variables", {})
        supplied_names = set(supplied) if isinstance(supplied, Mapping) else set()
        return tuple(
            value
            for value in values[:32]
            if isinstance(value, str)
            and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", value)
            and value not in supplied_names
        )

    @staticmethod
    def _is_direct_pytest(command: str) -> bool:
        return bool(
            re.search(
                r"(?:^|\s)(?:python\S*\s+-m\s+)?(?:pytest|py\.test)(?:\s|$)",
                command,
                re.IGNORECASE,
            )
        ) and not re.search(r"[;&|]", command)

    @staticmethod
    def _without_coverage_observation(command: ProjectCommand) -> ProjectCommand:
        display = command.command.display
        if not TestCommandSelector._is_direct_pytest(display) or "cov" not in display.casefold():
            return command
        try:
            tokens = shlex.split(display)
        except ValueError:
            return command
        filtered: list[str] = []
        skip_next = False
        value_options = {"--cov", "--cov-report", "--cov-config", "--cov-fail-under"}
        for token in tokens:
            if skip_next:
                skip_next = False
                continue
            lowered = token.casefold()
            if lowered in value_options:
                skip_next = True
                continue
            if lowered.startswith(
                ("--cov=", "--cov-report=", "--cov-config=", "--cov-fail-under=")
            ) or lowered in {"--cov-append", "--cov-branch", "--no-cov-on-fail"}:
                continue
            filtered.append(token)
        if filtered == tokens or not filtered:
            return command
        spec = CommandSpec(
            tuple(filtered),
            purpose=command.command.purpose,
            cwd=command.command.cwd,
            environment=command.command.environment,
            timeout_seconds=command.command.timeout_seconds,
        )
        return ProjectCommand(
            command.name,
            spec,
            f"normalized:{command.source}",
            command.confidence,
        )

    @staticmethod
    def _with_pytest_capture_disabled(
        profile: ProjectProfile,
        command: ProjectCommand,
    ) -> ProjectCommand:
        evidence = profile.metadata.get("pytest_capture_incompatible_files", ())
        if (
            not isinstance(evidence, (list, tuple))
            or not any(isinstance(item, str) and item for item in evidence)
            or not TestCommandSelector._is_direct_pytest(command.command.display)
        ):
            return command
        try:
            tokens = shlex.split(command.command.display)
        except ValueError:
            return command
        lowered = tuple(token.casefold() for token in tokens)
        if (
            "-s" in lowered
            or "--capture=no" in lowered
            or any(
                token == "--capture" and index + 1 < len(lowered)
                and lowered[index + 1] == "no"
                for index, token in enumerate(lowered)
            )
        ):
            return command
        spec = CommandSpec(
            (*tokens, "-s"),
            purpose=command.command.purpose,
            cwd=command.command.cwd,
            environment=command.command.environment,
            timeout_seconds=command.command.timeout_seconds,
        )
        return ProjectCommand(
            command.name,
            spec,
            f"capture-safe:{command.source}",
            command.confidence,
        )

    @staticmethod
    def _has_unresolved_variables(command: str) -> bool:
        return bool(
            re.search(
                r"\$\{\{|\$\{?[A-Za-z_][A-Za-z0-9_.-]*\}?|"
                r"%[A-Za-z_][A-Za-z0-9_]*%",
                command,
            )
        )

    @staticmethod
    def _is_direct_gradle_test(command: str) -> bool:
        try:
            tokens = shlex.split(command)
        except ValueError:
            return False
        executables = {PurePosixPath(token).name.casefold() for token in tokens[:2]}
        return bool(executables & {"gradle", "gradlew"}) and "test" in {
            token.casefold() for token in tokens
        }

    @staticmethod
    def _without_optional_profile_variables(
        profile: ProjectProfile,
        command: ProjectCommand,
    ) -> ProjectCommand:
        raw = profile.metadata.get("optional_test_profile_variables", ())
        if not isinstance(raw, (list, tuple)):
            return command
        optional = {
            item
            for item in raw[:32]
            if isinstance(item, str) and re.fullmatch(r"[A-Z][A-Z0-9_]*", item)
        }
        if not optional:
            return command
        try:
            tokens = shlex.split(command.command.display)
        except ValueError:
            return command
        filtered = [
            token
            for token in tokens
            if not (
                (match := re.fullmatch(r"\$\{?([A-Z][A-Z0-9_]*)\}?", token))
                and match.group(1) in optional
            )
        ]
        if filtered == tokens or not filtered:
            return command
        spec = CommandSpec(
            (shlex.join(filtered),),
            purpose=command.command.purpose,
            cwd=command.command.cwd,
            environment=command.command.environment,
            timeout_seconds=command.command.timeout_seconds,
            shell=True,
        )
        return ProjectCommand(
            command.name,
            spec,
            command.source,
            command.confidence,
        )

    @staticmethod
    def _with_jvm_environment_options(
        profile: ProjectProfile,
        command: ProjectCommand,
    ) -> ProjectCommand:
        try:
            tokens = shlex.split(command.command.display)
        except ValueError:
            return command
        if not tokens:
            return command
        executable = PurePosixPath(tokens[0]).name.casefold()
        normalized = list(tokens)
        if (
            str(profile.metadata.get("primary_build_system", "")).casefold() == "gradle"
            and executable in {"gradle", "gradlew"}
        ):
            normalized.insert(0, GRADLE_PROXY_EXECUTABLE)
        elif (
            profile.metadata.get("maven_git_hook_install_source")
            and executable in {"mvn", "mvnw"}
        ):
            option = "-Dgitbuildhook.install.skip=true"
            if not any(token.casefold() == option.casefold() for token in normalized):
                normalized.append(option)
        if normalized == tokens:
            return command
        spec = CommandSpec(
            (shlex.join(normalized),),
            purpose=command.command.purpose,
            cwd=command.command.cwd,
            environment=command.command.environment,
            timeout_seconds=command.command.timeout_seconds,
            shell=True,
        )
        return ProjectCommand(
            command.name,
            spec,
            f"container-contract:{command.source}",
            command.confidence,
        )

    @staticmethod
    def _command_rank(command: str) -> int:
        text = command.lower()
        if TestCommandSelector._is_direct_jvm_test(command):
            return 0
        if re.fullmatch(
            r"(?:\./)?mvnw?\s+-b\s+test|"
            r"(?:\./)?gradlew?\s+(?:--no-daemon\s+)?test|"
            r"ctest\s+--test-dir\s+build\s+--output-on-failure|"
            r"meson\s+test\s+-c\s+build\s+--print-errorlogs",
            text.strip(),
        ):
            return 0
        if re.search(r"\b(pytest|py\.test)\b", text):
            return 0
        if re.search(r"\bpython\S*\s+(?:manage\.py\s+test|runtests\.py)\b", text):
            return 0
        if re.search(r"\bpython\S*\s+[^ ]*(?:u?tests?)/[^ ]+\.py\b", text):
            return 0
        if re.search(r"\bunittest\b", text):
            return 0
        if re.search(r"\b(tox|nox)\b", text):
            if re.search(r"\b(docs?|lint|format|typecheck|mypy|ruff)\b", text):
                return 3
            return 1
        if re.search(r"\b(integration|postgres|mysql|cockroach|redis)\b", text):
            return 3
        if re.fullmatch(r"make\s+(?:test|check)", text.strip()):
            return 1
        if re.search(r"\b(ruff|mypy|format|typecheck|docs?)\b", text):
            return 4
        return 2

    @staticmethod
    def _is_direct_jvm_test(command: str) -> bool:
        try:
            tokens = shlex.split(command)
        except ValueError:
            return False
        if not tokens:
            return False
        executable = PurePosixPath(tokens[0]).name.casefold()
        if executable in {"mvn", "mvnw"}:
            goals = [
                token.casefold()
                for token in tokens[1:]
                if not token.startswith("-") and "=" not in token
            ]
            return goals == ["test"]
        if executable in {"gradle", "gradlew"}:
            tasks = [
                token.casefold()
                for token in tokens[1:]
                if not token.startswith("-") and "=" not in token
            ]
            return bool(tasks) and all(
                task == "test" or task.endswith(":test") for task in tasks
            )
        return False

    @classmethod
    def _unsafe_test_candidate(
        cls,
        profile: ProjectProfile,
        command: ProjectCommand,
    ) -> bool:
        display = command.command.display
        source = command.source.casefold()
        if re.search(
            r"(?:^|[/_.-])(docs?|lint|format|fuzz|benchmarks?|integration|e2e|"
            r"performance|slow|remote|release)(?:[/_.-]|$)",
            source,
        ):
            return True
        if re.search(
            r"--(?:remote-data|benchmark|run-slow)\b|"
            r"(?:^|\s)(?:tests?/)?(?:integration|e2e|benchmarks?|slow)(?:/|\s|$)",
            display,
            re.IGNORECASE,
        ):
            return True
        tool, name = cls._matrix_invocation(display)
        if not tool or not name:
            return False
        entries = cls._metadata_entries(profile, f"{tool}_environments")
        if tool == "nox":
            entries = cls._metadata_entries(profile, "nox_sessions")
        known = matrix_entry(entries, name)
        if known is not None:
            return known.get("safe") is not True
        return bool(
            re.search(
                r"(?:^|[-_.])(docs?|lint|format|typecheck|mypy|ruff|fuzz|"
                r"benchmarks?|integration|e2e|remote|release)(?:[-_.]|$)",
                name,
                re.IGNORECASE,
            )
        )

    @staticmethod
    def _runtime_rank(command: str, python_version: str) -> int:
        normalized = TestCommandSelector._normalize_python_version(python_version)
        if not normalized:
            return 1
        versions = re.findall(
            r"(?:python|py)(\d(?:\.\d{1,2}|\d{1,2}))",
            command,
            re.IGNORECASE,
        )
        if not versions:
            return 1
        normalized_versions = {
            TestCommandSelector._normalize_python_version(item) for item in versions
        }
        return 0 if normalized in normalized_versions else 2

    @staticmethod
    def _source_rank(profile: ProjectProfile, command: ProjectCommand) -> int:
        source = command.source.lower()
        if source.startswith("framework:"):
            return 0
        if command.source in profile.ci_files or source.startswith((".github/", ".circleci/")):
            return 0
        if command.source in profile.readme_files or "readme" in source:
            return 1
        text = command.command.display.lower()
        if re.search(r"\b(pytest|unittest)\b", text) or source == "inferred:test-layout":
            return 2
        if any(name in source for name in ("pyproject", "tox.ini", "noxfile", "setup.cfg")):
            return 3
        return 4

    @staticmethod
    def _bounded_matrix_command(
        profile: ProjectProfile,
        command: ProjectCommand,
        python_version: str = "",
    ) -> ProjectCommand:
        display = command.command.display.strip()
        replacement = ""
        tool, selected_name = TestCommandSelector._matrix_invocation(display)
        if tool == "tox":
            entries = TestCommandSelector._metadata_entries(profile, "tox_environments")
            env = preferred_matrix_name(entries, python_version=python_version)
            if not env:
                env = str(profile.metadata.get("default_tox_env", "")).strip()
            selected = matrix_entry(entries, selected_name) if selected_name else None
            selected_version = str((selected or {}).get("python_version", ""))
            if env and (
                not selected_name
                or selected is None
                or selected.get("safe") is not True
                or (
                    python_version
                    and selected_version
                    and selected_version
                    != TestCommandSelector._normalize_python_version(python_version)
                )
            ):
                replacement = f"tox -e {env}"
        elif tool == "nox":
            entries = TestCommandSelector._metadata_entries(profile, "nox_sessions")
            session = preferred_matrix_name(entries, python_version=python_version)
            if not session:
                session = str(profile.metadata.get("default_nox_session", "")).strip()
            selected = matrix_entry(entries, session) if session else None
            versions = tuple((selected or {}).get("python_versions", ()))
            normalized = TestCommandSelector._normalize_python_version(python_version)
            if normalized and normalized in {
                TestCommandSelector._normalize_python_version(item) for item in versions
            }:
                session = f"{session}-{normalized}"
            if session and (not selected_name or selected_name != session):
                replacement = f"nox -s {session}"
        if not replacement:
            return command
        bounded = CommandSpec(
            (replacement,),
            purpose=command.command.purpose,
            cwd=command.command.cwd,
            environment=command.command.environment,
            timeout_seconds=command.command.timeout_seconds,
            shell=True,
        )
        return ProjectCommand(
            command.name,
            bounded,
            command.source,
            command.confidence,
        )

    @staticmethod
    def _metadata_entries(
        profile: ProjectProfile,
        key: str,
    ) -> tuple[Mapping[str, Any], ...]:
        value = profile.metadata.get(key, ())
        if not isinstance(value, (list, tuple)):
            return ()
        return tuple(item for item in value if isinstance(item, Mapping))

    @staticmethod
    def _matrix_invocation(command: str) -> tuple[str, str]:
        tox = re.search(r"\btox\b(?:\s+[^;&|\s]+)*?\s+(?:-e|--environment)\s+([^\s,;&|]+)", command)
        if tox:
            return "tox", tox.group(1)
        if re.fullmatch(r"(?:python\S*\s+-m\s+)?tox", command):
            return "tox", ""
        nox = re.search(r"\bnox\b(?:\s+[^;&|\s]+)*?\s+(?:-s|--session)\s+([^\s,;&|]+)", command)
        if nox:
            return "nox", nox.group(1)
        if re.fullmatch(r"(?:python\S*\s+-m\s+)?nox", command):
            return "nox", ""
        return "", ""

    @staticmethod
    def _normalize_python_version(value: object) -> str:
        text = str(value).strip().casefold().removeprefix("python").removeprefix("py")
        dotted = re.fullmatch(r"(\d)\.(\d{1,2})(?:\.\d+)?", text)
        if dotted:
            return f"{dotted.group(1)}.{int(dotted.group(2))}"
        compact = re.fullmatch(r"(\d)(\d{1,2})", text)
        if compact:
            return f"{compact.group(1)}.{int(compact.group(2))}"
        return ""

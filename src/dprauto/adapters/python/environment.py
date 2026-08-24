"""Python/Docker workspace snapshot adapter for structured environment diffs."""

from __future__ import annotations

import hashlib
import re
import shlex
from pathlib import Path, PurePosixPath

from dprauto.domain.models import EnvironmentDiff, EnvironmentSnapshot, ProjectProfile
from dprauto.environment import compare_environment_snapshots
from dprauto.inspection.scanner import FileScanner


_SECRET_NAME = re.compile(r"(?:SECRET|TOKEN|PASSWORD|PASSWD|API_KEY|PRIVATE_KEY)", re.I)
_NON_SOURCE_NAMES = {"conftest.py", "noxfile.py", "setup.py", "toxfile.py"}
_VERIFICATION_REQUIREMENTS_PATH = ".dprauto/requirements-verification.txt"


class PythonEnvironmentDiffer:
    """Capture normalized build/runtime inputs without executing project code."""

    def __init__(self, scanner: FileScanner | None = None) -> None:
        self.scanner = scanner or FileScanner()

    def snapshot(self, profile: ProjectProfile, workspace: Path) -> EnvironmentSnapshot:
        scanned = self.scanner.scan(workspace)
        build_paths = tuple(
            path
            for path in scanned.files
            if self._is_build_script(path)
        )
        build_content = {path: scanned.read_text(path) for path in build_paths}
        combined_build = "\n".join(build_content.values())
        docker_content = "\n".join(
            content
            for path, content in build_content.items()
            if PurePosixPath(path).name.lower().startswith("dockerfile")
        )

        return EnvironmentSnapshot(
            build_scripts={
                path: self._digest(content) for path, content in sorted(build_content.items())
            },
            business_source={
                path: self._digest(scanned.read_text(path))
                for path in scanned.files
                if self._is_business_source(path)
            },
            base_image=self._base_image(docker_content),
            python_version=self._python_version(profile, scanned, docker_content),
            system_packages=self._system_packages(combined_build),
            python_dependencies=self._python_dependencies(scanned, combined_build),
            environment_variables=self._environment_variables(combined_build),
            startup_arguments=self._startup_arguments(build_content),
        )

    def compare(
        self,
        before: EnvironmentSnapshot,
        after: EnvironmentSnapshot,
    ) -> EnvironmentDiff:
        return compare_environment_snapshots(before, after)

    @staticmethod
    def _is_build_script(path: str) -> bool:
        name = PurePosixPath(path).name.lower()
        return (
            PurePosixPath(path).as_posix() == _VERIFICATION_REQUIREMENTS_PATH
            or name == "setup.sh"
            or name.startswith("dockerfile")
        )

    @staticmethod
    def _is_business_source(path: str) -> bool:
        pure = PurePosixPath(path)
        if pure.suffix.lower() != ".py" or pure.name.lower() in _NON_SOURCE_NAMES:
            return False
        lowered_parts = {part.lower() for part in pure.parts}
        return not lowered_parts & {"tests", "test"}

    @staticmethod
    def _digest(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    @staticmethod
    def _logical_lines(content: str) -> tuple[str, ...]:
        return tuple(
            line.strip()
            for line in re.sub(r"\\\s*\n", " ", content).splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )

    @staticmethod
    def _base_image(content: str) -> str | None:
        match = re.search(
            r"(?im)^\s*FROM\s+(?:--platform=\S+\s+)?([^\s]+)",
            content,
        )
        return match.group(1) if match else None

    def _python_version(self, profile, scanned, docker_content: str) -> str | None:
        for pattern in (
            r"(?im)^\s*(?:ARG|ENV)\s+PYTHON_VERSION(?:=|\s+)([^\s]+)",
            r"(?im)^\s*FROM\s+(?:--platform=\S+\s+)?python:([0-9]+(?:\.[0-9]+){1,2})",
        ):
            match = re.search(pattern, docker_content)
            if match:
                return match.group(1)
        for path in (".python-version", "runtime.txt"):
            value = scanned.read_text(path).strip()
            match = re.search(r"[0-9]+(?:\.[0-9]+){1,2}", value)
            if match:
                return match.group(0)
        pyproject = scanned.read_text("pyproject.toml")
        match = re.search(r"(?m)^\s*requires-python\s*=\s*[\"']([^\"']+)", pyproject)
        if match:
            return match.group(1)
        poetry = self._toml_section(pyproject, "tool.poetry.dependencies")
        match = re.search(r"(?m)^\s*python\s*=\s*[\"']([^\"']+)", poetry)
        if match:
            return match.group(1)
        setup_py = scanned.read_text("setup.py")
        match = re.search(r"python_requires\s*=\s*[\"']([^\"']+)", setup_py)
        if match:
            return match.group(1)
        return profile.runtime_constraints.get("python") or None

    def _system_packages(self, content: str) -> dict[str, str | None]:
        packages: dict[str, str | None] = {}
        normalized = re.sub(r"\\\s*\n", " ", content)
        patterns = (
            r"\b(?:apt-get|apt)\b(?:\s+(?!install\b)[^;&\s]+)*\s+install\s+([^;&\n]+)",
            r"\bapk\b(?:\s+(?!add\b)[^;&\s]+)*\s+add\s+([^;&\n]+)",
            r"\b(?:dnf|yum)\b(?:\s+(?!install\b)[^;&\s]+)*\s+install\s+([^;&\n]+)",
        )
        for pattern in patterns:
            for match in re.finditer(pattern, normalized, re.IGNORECASE):
                for token in self._tokens(match.group(1)):
                    if token.startswith("-") or token in {"apt-get", "apt", "apk", "dnf", "yum"}:
                        continue
                    name, version = self._split_version(token)
                    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9+_.-]*", name):
                        packages[name.lower()] = version
        return dict(sorted(packages.items()))

    def _python_dependencies(self, scanned, build_content: str) -> dict[str, str | None]:
        dependencies: dict[str, str | None] = {}
        for path in scanned.files:
            name = PurePosixPath(path).name.lower()
            text = scanned.read_text(path)
            if name.startswith("requirements") and name.endswith((".txt", ".in")):
                for line in text.splitlines():
                    self._add_python_requirement(dependencies, line)
            elif name == "pyproject.toml":
                for block in re.findall(r"(?s)dependencies\s*=\s*\[(.*?)\]", text):
                    for value in re.findall(r"[\"']([^\"']+)[\"']", block):
                        self._add_python_requirement(dependencies, value)
                poetry = self._toml_section(text, "tool.poetry.dependencies")
                for match in re.finditer(
                    r"(?m)^\s*([A-Za-z0-9_.-]+)\s*=\s*[\"']([^\"']+)", poetry
                ):
                    if match.group(1).lower() != "python":
                        dependencies[self._canonical(match.group(1))] = match.group(2).strip()
            elif name == "setup.py":
                block = re.search(r"install_requires\s*=\s*\[(.*?)\]", text, re.DOTALL)
                if block:
                    for value in re.findall(r"[\"']([^\"']+)[\"']", block.group(1)):
                        self._add_python_requirement(dependencies, value)

        normalized = re.sub(r"\\\s*\n", " ", build_content)
        for match in re.finditer(
            r"\b(?:python\d*\s+-m\s+)?pip(?:3)?\s+install\s+([^;&\n]+)",
            normalized,
            re.IGNORECASE,
        ):
            tokens = self._tokens(match.group(1))
            skip_next = False
            for token in tokens:
                if skip_next:
                    skip_next = False
                    continue
                if token in {"-r", "--requirement", "-c", "--constraint"}:
                    skip_next = True
                    continue
                if token.startswith("-") or token in {".", "./"}:
                    continue
                self._add_python_requirement(dependencies, token)
        return dict(sorted(dependencies.items()))

    def _environment_variables(self, content: str) -> dict[str, str]:
        values: dict[str, str] = {}
        for line in self._logical_lines(content):
            match = re.match(r"(?i)^ENV\s+(.+)$", line)
            if match:
                tokens = self._tokens(match.group(1))
                if len(tokens) == 2 and "=" not in tokens[0]:
                    values[tokens[0]] = self._safe_value(tokens[0], tokens[1])
                else:
                    for token in tokens:
                        if "=" in token:
                            name, value = token.split("=", 1)
                            values[name] = self._safe_value(name, value)
            export = re.match(r"(?i)^export\s+([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
            if export:
                values[export.group(1)] = self._safe_value(
                    export.group(1), export.group(2).strip()
                )
        return dict(sorted(values.items()))

    def _startup_arguments(self, contents: dict[str, str]) -> dict[str, str]:
        values: dict[str, str] = {}
        for path, content in sorted(contents.items()):
            for line in self._logical_lines(content):
                match = re.match(r"(?i)^(CMD|ENTRYPOINT)\s+(.+)$", line)
                if match:
                    values[f"{path}:{match.group(1).upper()}"] = match.group(2).strip()
                elif PurePosixPath(path).name.lower() == "setup.sh" and re.match(
                    r"^exec\s+", line
                ):
                    values[f"{path}:EXEC"] = line.removeprefix("exec ").strip()
        return values

    def _add_python_requirement(
        self,
        dependencies: dict[str, str | None],
        raw: str,
    ) -> None:
        value = raw.strip()
        if not value or value.startswith(("#", "-", "git+", "http://", "https://")):
            return
        value = value.split(";", 1)[0].strip()
        match = re.match(r"([A-Za-z0-9_.-]+)(?:\[[^\]]+\])?\s*(.*)$", value)
        if not match:
            return
        dependencies[self._canonical(match.group(1))] = match.group(2).strip() or None

    @staticmethod
    def _canonical(name: str) -> str:
        return re.sub(r"[-_.]+", "-", name).lower()

    @staticmethod
    def _split_version(token: str) -> tuple[str, str | None]:
        if "=" not in token:
            return token, None
        name, version = token.split("=", 1)
        return name, version or None

    @staticmethod
    def _tokens(value: str) -> tuple[str, ...]:
        try:
            return tuple(shlex.split(value, comments=True))
        except ValueError:
            return tuple(value.split())

    @staticmethod
    def _toml_section(text: str, name: str) -> str:
        match = re.search(
            rf"(?ms)^\s*\[{re.escape(name)}\]\s*$\n(.*?)(?=^\s*\[|\Z)", text
        )
        return match.group(1) if match else ""

    @staticmethod
    def _safe_value(name: str, value: str) -> str:
        if not _SECRET_NAME.search(name):
            return value
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
        return f"<redacted:sha256:{digest}>"

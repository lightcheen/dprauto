"""Rule-first ProjectParser implementation for Python repositories."""

from __future__ import annotations

import ast
import re
from pathlib import Path, PurePosixPath
from typing import Iterable

from dprauto.adapters.python.test_matrix import (
    nox_session_metadata,
    preferred_matrix_name,
    tox_environment_metadata,
    tox_pytest_parallel_metadata,
)
from dprauto.domain.enums import CommandPurpose, ProjectType
from dprauto.domain.models import CommandSpec, ProjectCommand, ProjectProfile, SourceReference
from dprauto.errors import ProjectParsingError
from dprauto.inspection.commands import CommandExtractor, ExtractedCommand
from dprauto.inspection.scanner import FileScanner, ScannedProject


_PYTHON_MANIFEST_NAMES = {
    "environment.yml",
    "environment.yaml",
    "pdm.lock",
    "pipfile",
    "pipfile.lock",
    "poetry.lock",
    "pyproject.toml",
    "setup.cfg",
    "setup.py",
    "uv.lock",
}
_PYTHON_BUILD_NAMES = {
    "hatch.toml",
    "makefile",
    "noxfile.py",
    "pyproject.toml",
    "setup.cfg",
    "setup.py",
    "tox.ini",
    "setup.sh",
}
_CI_ROOT_FILES = {
    ".gitlab-ci.yml",
    ".gitlab-ci.yaml",
    ".travis.yml",
    "azure-pipelines.yml",
    "jenkinsfile",
}
_SCM_CHANGELOG_NAMES = (
    "CHANGES.rst",
    "CHANGES.md",
    "CHANGELOG.rst",
    "CHANGELOG.md",
    "HISTORY.rst",
    "HISTORY.md",
)
_SCM_VERSION_LITERAL = re.compile(
    r"(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){1,3}"
    r"(?:(?:a|b|rc)[0-9]+|\.post[0-9]+|\.dev[0-9]+)?"
    r"(?:\+[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)*)?",
    re.IGNORECASE,
)
_WEB_DEPENDENCIES = {
    "bottle",
    "dash",
    "django",
    "falcon",
    "fastapi",
    "flask",
    "gradio",
    "gunicorn",
    "sanic",
    "starlette",
    "streamlit",
    "tornado",
    "uvicorn",
}
_TEST_PATH_RISK = re.compile(
    r"(?:^|[-_.])(integration|e2e|functional|remote|live|slow|notebooks?|benchmark|"
    r"bench|performance|fuzz)(?:[-_.]|$)",
    re.IGNORECASE,
)
_TEST_CONTENT_RISK = re.compile(
    r"(?:pytest\.mark\.(?:integration|e2e|remote|live|slow)|"
    r"\.download\s*\(\s*['\"]https?://|"
    r"(?:requests|httpx)\.(?:get|post|put|delete|request)\s*\(\s*['\"]https?://)",
    re.IGNORECASE,
)


class PythonProjectParser:
    """Build a ProjectProfile from repository evidence without invoking an LLM."""

    def __init__(
        self,
        scanner: FileScanner | None = None,
        command_extractor: CommandExtractor | None = None,
    ) -> None:
        self.scanner = scanner or FileScanner()
        self.command_extractor = command_extractor or CommandExtractor()

    def parse(self, source: SourceReference, workspace: Path) -> ProjectProfile:
        scanned = self.scanner.scan(workspace)
        if not self._is_python_project(scanned):
            raise ProjectParsingError(f"no Python project indicators found in {workspace}")

        dependency_files = self._dependency_files(scanned)
        build_files = self._build_files(scanned)
        dockerfiles = self._dockerfiles(scanned)
        readme_files = self._readme_files(scanned)
        ci_files = self._ci_files(scanned)
        package_managers = self._package_managers(scanned, dependency_files)
        python_constraint, version_evidence = self._python_version(scanned, ci_files)
        entry_points = self._entry_points(scanned)
        dependencies = self._dependencies(scanned, dependency_files)
        system_dependency_hints = self._system_dependency_hints(
            scanned,
            dependency_files,
            dependencies,
        )
        test_dependency_extras, test_dependency_manager_groups = (
            self._test_dependency_groups(scanned)
        )
        test_dependency_groups = tuple(
            dict.fromkeys((*test_dependency_extras, *test_dependency_manager_groups))
        )
        extracted_commands = self._extract_commands(scanned, readme_files, ci_files)
        inferred_commands = self._infer_commands(
            scanned,
            dependency_files,
            build_files,
            package_managers,
            entry_points,
        )
        commands = self._project_commands((*extracted_commands, *inferred_commands))
        project_type, type_evidence = self._project_type(
            scanned,
            dependencies,
            entry_points,
            commands,
            build_files,
        )
        declared_project_name = self._project_name(scanned)
        project_name = declared_project_name or scanned.root.name
        import_modules = self._import_modules(scanned)
        project_id = f"{project_name}@{source.revision}" if source.revision else project_name
        default_tox_env = self._default_tox_env(scanned)
        default_nox_session = self._default_nox_session(scanned)
        tox_environments = tox_environment_metadata(scanned.read_text("tox.ini"))
        nox_sessions = nox_session_metadata(scanned.read_text("noxfile.py"))
        pytest_parallel = dict(
            tox_pytest_parallel_metadata(scanned.read_text("tox.ini"))
        )
        if pytest_parallel:
            pytest_parallel["ci_confirmed"] = self._ci_uses_tox_factor(
                scanned,
                ci_files,
                str(pytest_parallel["factor"]),
            )
        scm_versioning = self._scm_versioning(scanned)
        test_file_metadata = self._test_file_metadata(scanned)

        runtime_constraints = {"python": python_constraint} if python_constraint else {}
        return ProjectProfile(
            project_id=project_id,
            source=source,
            languages=("Python",),
            project_type=project_type,
            runtime_constraints=runtime_constraints,
            package_managers=package_managers,
            dependency_files=dependency_files,
            build_files=build_files,
            dockerfiles=dockerfiles,
            readme_files=readme_files,
            ci_files=ci_files,
            commands=commands,
            metadata={
                "parser": "python-rules-v1",
                "project_name": project_name,
                "project_name_declared": bool(declared_project_name),
                "import_modules": import_modules,
                "python_version_evidence": version_evidence,
                "entry_points": entry_points,
                "dependency_names": tuple(sorted(dependencies)),
                "system_dependency_hints": system_dependency_hints,
                "test_dependency_groups": test_dependency_groups,
                "test_dependency_extras": test_dependency_extras,
                "test_dependency_manager_groups": test_dependency_manager_groups,
                "default_tox_env": default_tox_env,
                "default_nox_session": default_nox_session,
                "tox_environments": tox_environments,
                "nox_sessions": nox_sessions,
                "pytest_parallel": pytest_parallel,
                "scm_versioning": scm_versioning,
                **test_file_metadata,
                "project_type_evidence": type_evidence,
                "scan_file_count": len(scanned.files),
                "scan_skipped_files": scanned.skipped_files,
                "scan_truncated": scanned.truncated,
            },
        )

    @staticmethod
    def _depth(path: str) -> int:
        return len(PurePosixPath(path).parts)

    def _is_python_project(self, scanned: ScannedProject) -> bool:
        return any(
            path.endswith(".py")
            or PurePosixPath(path).name.lower() in _PYTHON_MANIFEST_NAMES
            or PurePosixPath(path).name.lower().startswith("requirements")
            for path in scanned.files
        )

    def _dependency_files(self, scanned: ScannedProject) -> tuple[str, ...]:
        selected = []
        for path in scanned.files:
            name = PurePosixPath(path).name.lower()
            if self._depth(path) > 3:
                continue
            if name in _PYTHON_MANIFEST_NAMES or (
                name.startswith("requirements") and name.endswith((".txt", ".in"))
            ):
                selected.append(path)
        return tuple(selected)

    def _build_files(self, scanned: ScannedProject) -> tuple[str, ...]:
        return tuple(
            path
            for path in scanned.files
            if self._depth(path) <= 2
            and PurePosixPath(path).name.lower() in _PYTHON_BUILD_NAMES
        )

    @staticmethod
    def _dockerfiles(scanned: ScannedProject) -> tuple[str, ...]:
        excluded_roots = {
            ".circleci",
            ".github",
            ".gitlab",
            "docs",
            "examples",
            "test",
            "tests",
        }
        return tuple(
            path
            for path in scanned.files
            if (
                PurePosixPath(path).name.lower().startswith("dockerfile")
                or PurePosixPath(path).suffix.lower() == ".dockerfile"
            )
            and len(PurePosixPath(path).parts) <= 4
            and PurePosixPath(path).parts[0].lower() not in excluded_roots
        )

    def _test_dependency_groups(
        self, scanned: ScannedProject
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Discover test extras and manager-only dependency groups separately."""

        accepted = {"dev", "develop", "development", "qa", "test", "tests", "testing"}
        extras: list[str] = []
        manager_groups: list[str] = []
        pyproject = scanned.read_text("pyproject.toml")
        for section_name in ("project.optional-dependencies", "tool.poetry.extras"):
            section = self._toml_section(pyproject, section_name)
            extras.extend(self._test_group_names(section, accepted))
        for section_name in ("dependency-groups", "tool.pdm.dev-dependencies"):
            section = self._toml_section(pyproject, section_name)
            manager_groups.extend(self._test_group_names(section, accepted))
        manager_groups.extend(
            match.group(1)
            for match in re.finditer(
                r"(?m)^\s*\[tool\.poetry\.group\.([A-Za-z0-9_.-]+)\.dependencies\]\s*$",
                pyproject,
            )
            if match.group(1).lower() in accepted
        )
        if "[tool.poetry.dev-dependencies]" in pyproject:
            manager_groups.append("dev")

        setup_cfg = scanned.read_text("setup.cfg")
        extras_section = self._ini_section(setup_cfg, "options.extras_require")
        extras.extend(
            match.group(1)
            for match in re.finditer(r"(?m)^\s*([A-Za-z0-9_.-]+)\s*=", extras_section)
            if match.group(1).lower() in accepted
        )
        setup_py = scanned.read_text("setup.py")
        extras_match = re.search(r"extras_require\s*=\s*\{(.*?)\}", setup_py, re.DOTALL)
        if extras_match:
            extras.extend(
                match.group(1)
                for match in re.finditer(r"[\"']([A-Za-z0-9_.-]+)[\"']\s*:", extras_match.group(1))
                if match.group(1).lower() in accepted
            )
        return tuple(dict.fromkeys(extras)), tuple(dict.fromkeys(manager_groups))

    @staticmethod
    def _test_group_names(section: str, accepted: set[str]) -> tuple[str, ...]:
        """Recognize conventional and pytest-semantic dependency groups."""

        names: list[str] = []
        assignments = re.finditer(
            r"(?ms)^\s*([A-Za-z0-9_.-]+)\s*=\s*\[(.*?)\](?=\s*^[A-Za-z0-9_.-]+\s*=|\Z)",
            section,
        )
        for match in assignments:
            name = match.group(1)
            normalized = name.casefold()
            values = match.group(2).casefold()
            conventional = normalized in accepted or bool(
                re.search(r"(?:^|[-_.])tests?(?:ing)?(?:[-_.]|$)", normalized)
                or normalized in {"pytest", "pytesting"}
            )
            semantic = bool(
                re.search(
                    r"['\"](?:pytest(?:[-_][a-z0-9_.-]+)?|tox|nox|hypothesis)"
                    r"(?:\[|[<>=!~'\"])",
                    values,
                )
            )
            if conventional or semantic:
                names.append(name)
        return tuple(names)

    @staticmethod
    def _test_file_metadata(scanned: ScannedProject) -> dict[str, object]:
        """Return bounded local-test, external-test, and required-secret evidence."""

        test_files: list[str] = []
        safe_files: list[str] = []
        external_files: list[str] = []
        required_environment: list[str] = []
        for path in scanned.files:
            pure = PurePosixPath(path)
            name = pure.name.casefold()
            in_test_tree = any(part.casefold() in {"test", "tests"} for part in pure.parts[:-1])
            is_test = path.endswith(".py") and (
                name.startswith("test_")
                or name.endswith("_test.py")
                or (in_test_tree and name == "test.py")
            )
            if is_test:
                test_files.append(path)
                content = scanned.read_text(path)
                path_risky = any(_TEST_PATH_RISK.search(part) for part in pure.parts)
                if path_risky or _TEST_CONTENT_RISK.search(content):
                    external_files.append(path)
                else:
                    safe_files.append(path)
            if name == "conftest.py":
                required_environment.extend(
                    PythonProjectParser._required_environment_literals(
                        scanned.read_text(path)
                    )
                )
        maximum = 512
        return {
            "test_files": tuple(test_files[:maximum]),
            "safe_test_files": tuple(safe_files[:maximum]),
            "external_test_files": tuple(external_files[:maximum]),
            "test_file_count": len(test_files),
            "safe_test_file_count": len(safe_files),
            "test_files_truncated": len(test_files) > maximum,
            "test_required_environment_variables": tuple(
                dict.fromkeys(required_environment)
            )[:32],
        }

    @staticmethod
    def _required_environment_literals(content: str) -> tuple[str, ...]:
        """Find literal environment lookups evaluated at module or class scope."""

        try:
            tree = ast.parse(content)
        except SyntaxError:
            return ()

        names: list[str] = []

        class Visitor(ast.NodeVisitor):
            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                return None

            def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
                return None

            def visit_Lambda(self, node: ast.Lambda) -> None:
                return None

            def visit_Subscript(self, node: ast.Subscript) -> None:
                value = node.value
                environment = (
                    isinstance(value, ast.Attribute)
                    and isinstance(value.value, ast.Name)
                    and value.value.id == "os"
                    and value.attr == "environ"
                ) or (isinstance(value, ast.Name) and value.id == "environ")
                key = node.slice
                if environment and isinstance(key, ast.Constant) and isinstance(key.value, str):
                    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", key.value):
                        names.append(key.value)
                self.generic_visit(node)

        Visitor().visit(tree)
        return tuple(dict.fromkeys(names))

    @staticmethod
    def _system_dependency_hints(
        scanned: ScannedProject,
        dependency_files: tuple[str, ...],
        dependency_names: set[str],
    ) -> tuple[str, ...]:
        """Return only high-confidence OS-tool hints derived from dependency manifests."""

        manifests = "\n".join(scanned.read_text(path) for path in dependency_files)
        hints: list[str] = []
        if re.search(r"(?i)\bgit\+(?:https?|ssh|file)://", manifests):
            hints.append("git-vcs")
        if "pyscard" in dependency_names:
            hints.append("pyscard-native")
        return tuple(hints)

    @staticmethod
    def _ini_section(text: str, name: str) -> str:
        match = re.search(
            rf"(?ms)^\s*\[{re.escape(name)}\]\s*$\n(.*?)(?=^\s*\[|\Z)",
            text,
        )
        return match.group(1) if match else ""

    def _readme_files(self, scanned: ScannedProject) -> tuple[str, ...]:
        return tuple(
            path
            for path in scanned.files
            if self._depth(path) <= 3
            and PurePosixPath(path).name.lower().startswith("readme")
            and PurePosixPath(path).suffix.lower() in {"", ".md", ".rst", ".txt"}
        )

    @staticmethod
    def _ci_files(scanned: ScannedProject) -> tuple[str, ...]:
        selected = []
        for path in scanned.files:
            lower = path.lower()
            name = PurePosixPath(path).name.lower()
            if lower.startswith(".github/workflows/") and lower.endswith((".yml", ".yaml")):
                selected.append(path)
            elif lower == ".circleci/config.yml" or name in _CI_ROOT_FILES:
                selected.append(path)
        return tuple(selected)

    def _package_managers(
        self,
        scanned: ScannedProject,
        dependency_files: tuple[str, ...],
    ) -> tuple[str, ...]:
        names = {PurePosixPath(path).name.lower() for path in dependency_files}
        pyproject = scanned.read_text("pyproject.toml")
        managers: list[str] = []
        if "uv.lock" in names:
            managers.append("uv")
        if "poetry.lock" in names or "[tool.poetry]" in pyproject:
            managers.append("poetry")
        if "pdm.lock" in names or "[tool.pdm]" in pyproject:
            managers.append("pdm")
        if "pipfile" in names or "pipfile.lock" in names:
            managers.append("pipenv")
        if "environment.yml" in names or "environment.yaml" in names:
            managers.append("conda")
        if "[tool.hatch" in pyproject or "hatch.toml" in names:
            managers.append("hatch")
        if not managers or any(
            name.startswith("requirements") or name in {"setup.py", "setup.cfg"}
            for name in names
        ):
            managers.append("pip")
        return tuple(dict.fromkeys(managers))

    def _python_version(
        self,
        scanned: ScannedProject,
        ci_files: tuple[str, ...],
    ) -> tuple[str, tuple[dict[str, str], ...]]:
        evidence: list[dict[str, str]] = []

        pyproject = scanned.read_text("pyproject.toml")
        match = re.search(r"(?m)^\s*requires-python\s*=\s*[\"']([^\"']+)", pyproject)
        if match:
            evidence.append({"source": "pyproject.toml", "value": match.group(1), "kind": "constraint"})
        poetry_section = self._toml_section(pyproject, "tool.poetry.dependencies")
        match = re.search(r"(?m)^\s*python\s*=\s*[\"']([^\"']+)", poetry_section)
        if match:
            evidence.append({"source": "pyproject.toml", "value": match.group(1), "kind": "constraint"})

        setup_py = scanned.read_text("setup.py")
        match = re.search(r"python_requires\s*=\s*[\"']([^\"']+)", setup_py)
        if match:
            evidence.append({"source": "setup.py", "value": match.group(1), "kind": "constraint"})

        setup_cfg = scanned.read_text("setup.cfg")
        match = re.search(r"(?m)^\s*python_requires\s*=\s*([^\n#]+)", setup_cfg)
        if match:
            evidence.append({"source": "setup.cfg", "value": match.group(1).strip(), "kind": "constraint"})

        for filename, pattern in (
            (".python-version", r"^\s*([^\s]+)"),
            ("runtime.txt", r"(?i)^\s*python[- ]?([^\s]+)"),
            ("Pipfile", r"(?m)^\s*python_version\s*=\s*[\"']([^\"']+)"),
        ):
            match = re.search(pattern, scanned.read_text(filename))
            if match:
                evidence.append({"source": filename, "value": match.group(1), "kind": "version"})

        for path in ci_files:
            text = scanned.read_text(path)
            versions: list[str] = []
            for values in re.findall(r"python-version\s*:\s*\[([^\]]+)\]", text, re.IGNORECASE):
                versions.extend(re.findall(r"\d+(?:\.\d+){1,2}", values))
            versions.extend(
                re.findall(
                    r"python-version\s*:\s*[\"']?(\d+(?:\.\d+){1,2})",
                    text,
                    re.IGNORECASE,
                )
            )
            versions.extend(
                re.findall(r"set\s+up\s+python\s+(\d+(?:\.\d+){1,2})", text, re.IGNORECASE)
            )
            for version in dict.fromkeys(versions):
                evidence.append({"source": path, "value": version, "kind": "ci"})

        unique: list[dict[str, str]] = []
        seen: set[tuple[str, str, str]] = set()
        for item in evidence:
            key = (item["source"], item["value"], item["kind"])
            if key not in seen:
                seen.add(key)
                unique.append(item)

        declared = next((item["value"] for item in unique if item["kind"] == "constraint"), "")
        pinned = next((item["value"] for item in unique if item["kind"] == "version"), "")
        ci_versions = tuple(dict.fromkeys(item["value"] for item in unique if item["kind"] == "ci"))
        return declared or pinned or ",".join(ci_versions), tuple(unique)

    @staticmethod
    def _toml_section(text: str, name: str) -> str:
        match = re.search(
            rf"(?ms)^\s*\[{re.escape(name)}\]\s*$\n(.*?)(?=^\s*\[|\Z)",
            text,
        )
        return match.group(1) if match else ""

    def _entry_points(self, scanned: ScannedProject) -> tuple[str, ...]:
        entries: list[str] = []
        pyproject = scanned.read_text("pyproject.toml")
        for section_name in ("project.scripts", "tool.poetry.scripts"):
            section = self._toml_section(pyproject, section_name)
            entries.extend(
                match.group(1)
                for match in re.finditer(r"(?m)^\s*([A-Za-z0-9_.-]+)\s*=\s*[\"'][^\"']+", section)
            )

        setup_py = scanned.read_text("setup.py")
        console_match = re.search(
            r"console_scripts[\"']?\s*:\s*\[(.*?)\]",
            setup_py,
            re.DOTALL,
        )
        if console_match:
            entries.extend(
                match.group(1)
                for match in re.finditer(r"[\"']\s*([A-Za-z0-9_.-]+)\s*=", console_match.group(1))
            )
        return tuple(dict.fromkeys(entries))

    def _dependencies(
        self,
        scanned: ScannedProject,
        dependency_files: tuple[str, ...],
    ) -> set[str]:
        dependencies: set[str] = set()
        for path in dependency_files:
            name = PurePosixPath(path).name.lower()
            text = scanned.read_text(path)
            if name.startswith("requirements"):
                for line in text.splitlines():
                    candidate = line.strip()
                    if not candidate or candidate.startswith(("#", "-")):
                        continue
                    match = re.match(r"([A-Za-z0-9_.-]+)", candidate)
                    if match:
                        dependencies.add(match.group(1).lower().replace("_", "-"))
            elif name == "pyproject.toml":
                project = self._toml_section(text, "project")
                dependency_block = re.search(r"dependencies\s*=\s*\[(.*?)\]", project, re.DOTALL)
                if dependency_block:
                    for candidate in re.findall(r"[\"']([^\"']+)[\"']", dependency_block.group(1)):
                        match = re.match(r"([A-Za-z0-9_.-]+)", candidate)
                        if match:
                            dependencies.add(match.group(1).lower().replace("_", "-"))
                poetry = self._toml_section(text, "tool.poetry.dependencies")
                for match in re.finditer(r"(?m)^\s*([A-Za-z0-9_.-]+)\s*=", poetry):
                    if match.group(1).lower() != "python":
                        dependencies.add(match.group(1).lower().replace("_", "-"))
            elif name == "setup.py":
                block = re.search(r"install_requires\s*=\s*\[(.*?)\]", text, re.DOTALL)
                if block:
                    for candidate in re.findall(r"[\"']([^\"']+)[\"']", block.group(1)):
                        match = re.match(r"([A-Za-z0-9_.-]+)", candidate)
                        if match:
                            dependencies.add(match.group(1).lower().replace("_", "-"))
        return dependencies

    def _extract_commands(
        self,
        scanned: ScannedProject,
        readme_files: tuple[str, ...],
        ci_files: tuple[str, ...],
    ) -> tuple[ExtractedCommand, ...]:
        commands: list[ExtractedCommand] = []
        for path in readme_files:
            if self._depth(path) == 1:
                commands.extend(self.command_extractor.extract_markdown(path, scanned.read_text(path)))
        for path in ci_files:
            commands.extend(self.command_extractor.extract_ci(path, scanned.read_text(path)))
        return tuple(commands)

    def _infer_commands(
        self,
        scanned: ScannedProject,
        dependency_files: tuple[str, ...],
        build_files: tuple[str, ...],
        package_managers: tuple[str, ...],
        entry_points: tuple[str, ...],
    ) -> tuple[ExtractedCommand, ...]:
        inferred: list[ExtractedCommand] = []

        install_command = ""
        install_source = ""
        if "uv" in package_managers:
            install_command, install_source = "uv sync", "inferred:uv.lock"
        elif "poetry" in package_managers:
            install_command, install_source = "poetry install", "inferred:poetry.lock"
        elif "pdm" in package_managers:
            install_command, install_source = "pdm install", "inferred:pdm.lock"
        elif "pipenv" in package_managers:
            install_command, install_source = "pipenv install --dev", "inferred:Pipfile"
        elif "conda" in package_managers:
            environment_file = next(
                (
                    path
                    for path in dependency_files
                    if PurePosixPath(path).name.lower() in {"environment.yml", "environment.yaml"}
                ),
                "environment.yml",
            )
            install_command = f"conda env create -f {environment_file}"
            install_source = f"inferred:{environment_file}"
        else:
            requirements = next(
                (
                    path
                    for path in dependency_files
                    if PurePosixPath(path).name.lower() == "requirements.txt"
                ),
                "",
            )
            if requirements:
                install_command = f"python -m pip install -r {requirements}"
                install_source = f"inferred:{requirements}"
            elif any(PurePosixPath(path).name.lower() in {"pyproject.toml", "setup.py", "setup.cfg"} for path in build_files):
                install_command, install_source = "python -m pip install .", "inferred:package-metadata"
        if install_command:
            inferred.append(
                ExtractedCommand(install_command, CommandPurpose.INSTALL, install_source, 0.8)
            )

        test_command = ""
        test_source = ""
        names = {PurePosixPath(path).name.lower() for path in build_files}
        if "tox.ini" in names:
            tox_env = self._default_tox_env(scanned)
            test_command = f"tox -e {tox_env}" if tox_env else "tox"
            test_source = "inferred:tox.ini"
        elif "noxfile.py" in names:
            nox_session = self._default_nox_session(scanned)
            test_command = f"nox -s {nox_session}" if nox_session else "nox"
            test_source = "inferred:noxfile.py"
        elif self._has_pytest_evidence(scanned):
            test_command, test_source = "python -m pytest", "inferred:test-layout"
        elif self._has_unittest_evidence(scanned):
            test_command, test_source = "python -m unittest discover", "inferred:test-layout"
        if test_command:
            inferred.append(ExtractedCommand(test_command, CommandPurpose.TEST, test_source, 0.75))

        for entry in entry_points:
            inferred.append(
                ExtractedCommand(entry, CommandPurpose.RUN, "inferred:entry-point", 0.9)
            )
        if not entry_points:
            module_entry = self._module_entry(scanned)
            if module_entry:
                inferred.append(
                    ExtractedCommand(
                        f"python -m {module_entry}",
                        CommandPurpose.RUN,
                        "inferred:__main__.py",
                        0.85,
                    )
                )
            else:
                script_entry = self._script_entry(scanned)
                if script_entry:
                    inferred.append(
                        ExtractedCommand(
                            f"python {script_entry}",
                            CommandPurpose.RUN,
                            f"inferred:{script_entry}",
                            0.7,
                        )
                    )
        return tuple(inferred)

    @staticmethod
    def _has_pytest_evidence(scanned: ScannedProject) -> bool:
        if scanned.by_name(("pytest.ini",)):
            return True
        pyproject = scanned.read_text("pyproject.toml")
        if "[tool.pytest" in pyproject:
            return True
        return any(
            PurePosixPath(path).name.startswith("test_") and path.endswith(".py")
            for path in scanned.files
        )

    @staticmethod
    def _has_unittest_evidence(scanned: ScannedProject) -> bool:
        for path in scanned.files:
            if path.endswith(".py") and "test" in PurePosixPath(path).parts:
                if "unittest" in scanned.read_text(path):
                    return True
        return False

    @staticmethod
    def _default_tox_env(scanned: ScannedProject) -> str:
        environments = tox_environment_metadata(scanned.read_text("tox.ini"))
        return preferred_matrix_name(environments)

    @staticmethod
    def _default_nox_session(scanned: ScannedProject) -> str:
        sessions = nox_session_metadata(scanned.read_text("noxfile.py"))
        return preferred_matrix_name(sessions)

    @staticmethod
    def _ci_uses_tox_factor(
        scanned: ScannedProject,
        ci_files: tuple[str, ...],
        factor: str,
    ) -> bool:
        factor_pattern = re.compile(
            rf"(?:^|[-_.]){re.escape(factor)}(?:[-_.]|$)",
            re.IGNORECASE,
        )
        for path in ci_files:
            for line in scanned.read_text(path).splitlines():
                match = re.match(
                    r"^\s*-\s+(?:[A-Za-z_][A-Za-z0-9_-]*:\s*)?([^\s#]+)\s*$",
                    line,
                )
                if match and factor_pattern.search(match.group(1)):
                    return True
        return False

    @staticmethod
    def _module_entry(scanned: ScannedProject) -> str:
        candidates = [path for path in scanned.files if path.endswith("/__main__.py")]
        if not candidates:
            return ""
        path = min(candidates, key=lambda candidate: (len(PurePosixPath(candidate).parts), candidate))
        parts = list(PurePosixPath(path).parts[:-1])
        if parts and parts[0] == "src":
            parts.pop(0)
        return ".".join(parts)

    @staticmethod
    def _script_entry(scanned: ScannedProject) -> str:
        preferred_names = ("main.py", "app.py", "server.py", "run.py")
        for name in preferred_names:
            candidates = [
                path
                for path in scanned.files
                if PurePosixPath(path).name.lower() == name and len(PurePosixPath(path).parts) <= 2
            ]
            if candidates:
                return min(candidates, key=lambda path: (len(PurePosixPath(path).parts), path))
        return ""

    def _project_type(
        self,
        scanned: ScannedProject,
        dependencies: set[str],
        entry_points: tuple[str, ...],
        commands: tuple[ProjectCommand, ...],
        build_files: tuple[str, ...],
    ) -> tuple[ProjectType, tuple[str, ...]]:
        evidence: list[str] = []
        web_dependencies = sorted(dependencies & _WEB_DEPENDENCIES)
        run_commands = [
            command.command.display.lower()
            for command in commands
            if command.command.purpose is CommandPurpose.RUN
        ]
        web_command = next(
            (
                command
                for command in run_commands
                if re.search(
                    r"\b(uvicorn|gunicorn|flask\s+run|runserver|streamlit\s+run|docker\s+compose\s+up)\b",
                    command,
                )
            ),
            "",
        )
        if web_dependencies:
            evidence.append(f"web dependencies: {', '.join(web_dependencies)}")
        if web_command:
            evidence.append(f"web command: {web_command}")
        if web_dependencies and (web_command or self._web_entry_source(scanned)):
            return ProjectType.WEB, tuple(evidence)
        if entry_points:
            return ProjectType.CLI, (f"console entry points: {', '.join(entry_points)}",)
        module_entry = self._module_entry(scanned)
        if module_entry:
            return ProjectType.CLI, (f"module entry point: {module_entry}",)
        script_entry = self._script_entry(scanned)
        if script_entry:
            return ProjectType.SCRIPT, (f"script entry point: {script_entry}",)
        if build_files or self._package_directories(scanned):
            return ProjectType.LIBRARY, ("package metadata or importable package directory",)
        return ProjectType.UNKNOWN, ()

    @staticmethod
    def _web_entry_source(scanned: ScannedProject) -> bool:
        for path in scanned.files:
            name = PurePosixPath(path).name.lower()
            if name not in {"app.py", "asgi.py", "main.py", "server.py", "wsgi.py"}:
                continue
            if len(PurePosixPath(path).parts) > 4:
                continue
            content = scanned.read_text(path).lower()
            if any(f"import {name}" in content or f"from {name}" in content for name in _WEB_DEPENDENCIES):
                return True
        return False

    @staticmethod
    def _package_directories(scanned: ScannedProject) -> bool:
        return any(
            path.endswith("/__init__.py") and "tests" not in PurePosixPath(path).parts
            for path in scanned.files
        )

    @staticmethod
    def _import_modules(scanned: ScannedProject) -> tuple[str, ...]:
        modules: list[str] = []
        for path in scanned.files:
            parts = list(PurePosixPath(path).parts)
            if not parts or parts[-1] != "__init__.py" or "tests" in parts:
                continue
            package_parts = parts[:-1]
            if package_parts and package_parts[0] == "src":
                package_parts.pop(0)
            if package_parts and all(part.isidentifier() for part in package_parts):
                modules.append(".".join(package_parts))
        return tuple(dict.fromkeys(sorted(modules, key=lambda item: (item.count("."), item))))

    def _project_name(self, scanned: ScannedProject) -> str:
        pyproject = scanned.read_text("pyproject.toml")
        for section_name in ("project", "tool.poetry"):
            section = self._toml_section(pyproject, section_name)
            match = re.search(r"(?m)^\s*name\s*=\s*[\"']([^\"']+)", section)
            if match:
                return match.group(1)
        setup_py = scanned.read_text("setup.py")
        match = re.search(r"(?m)^\s*name\s*=\s*[\"']([^\"']+)", setup_py)
        return match.group(1) if match else ""

    def _scm_versioning(self, scanned: ScannedProject) -> dict[str, str]:
        """Return explicit setuptools-scm configuration evidence, if present."""

        pyproject = scanned.read_text("pyproject.toml")
        result: dict[str, str] = {}
        if re.search(r"(?m)^\s*\[tool\.setuptools_scm\]\s*(?:#.*)?$", pyproject):
            result = {
                "provider": "setuptools-scm",
                "evidence": "pyproject.toml:[tool.setuptools_scm]",
            }
        else:
            setup_cfg = scanned.read_text("setup.cfg")
            if re.search(
                r"(?mi)^\s*use_scm_version\s*=\s*(?:true|1|yes)\s*$",
                setup_cfg,
            ):
                result = {
                    "provider": "setuptools-scm",
                    "evidence": "setup.cfg:use_scm_version",
                }
            else:
                setup_py = scanned.read_text("setup.py")
                if re.search(r"\buse_scm_version\s*=", setup_py):
                    result = {
                        "provider": "setuptools-scm",
                        "evidence": "setup.py:use_scm_version",
                    }
        if not result:
            return {}
        version_evidence = self._scm_source_version(scanned, pyproject)
        if version_evidence:
            result.update(version_evidence)
        return result

    def _scm_source_version(
        self,
        scanned: ScannedProject,
        pyproject: str,
    ) -> dict[str, str]:
        scm_section = self._toml_section(pyproject, "tool.setuptools_scm")
        version_path = re.search(
            r"(?m)^\s*(?:version_file|write_to)\s*=\s*[\"']([^\"']+)[\"']",
            scm_section,
        )
        if version_path:
            relative = PurePosixPath(version_path.group(1))
            if not relative.is_absolute() and ".." not in relative.parts:
                generated = scanned.read_text(relative.as_posix())
                version = re.search(
                    r"(?m)^\s*(?:__version__\s*=\s*)?"
                    r"(?:__version__|version)\s*=\s*[\"']([^\"']+)[\"']",
                    generated,
                )
                if version and self._valid_scm_version(version.group(1)):
                    return {
                        "version": version.group(1),
                        "version_kind": "generated-file",
                        "version_source": relative.as_posix(),
                    }

        package_metadata = scanned.read_text("PKG-INFO")
        package_version = re.search(r"(?m)^Version:\s*([^\s]+)\s*$", package_metadata)
        if package_version and self._valid_scm_version(package_version.group(1)):
            return {
                "version": package_version.group(1),
                "version_kind": "package-metadata",
                "version_source": "PKG-INFO:Version",
            }

        for path in _SCM_CHANGELOG_NAMES:
            content = scanned.read_text(path)
            if not content:
                continue
            for line in content[:8_000].splitlines()[:80]:
                heading = re.match(
                    r"^\s{0,3}(?:#{1,6}\s*)?\[?[vV]?"
                    r"([0-9]+\.[0-9]+\.[0-9]+)\]?\s*(.*?)\s*$",
                    line,
                )
                if not heading:
                    continue
                suffix = heading.group(2)
                if re.fullmatch(
                    r"(?:\(|\[|[-–—:])\s*"
                    r"(?:unreleased|in development|development)\s*"
                    r"(?:\)|\])?",
                    suffix,
                    re.IGNORECASE,
                ):
                    return {
                        "version": heading.group(1),
                        "version_kind": "unreleased-changelog",
                        "version_source": f"{path}:first-version-heading",
                    }
                # Only the first numeric version heading is authoritative. A
                # later unreleased example must not override a released top entry.
                break
        return {}

    @staticmethod
    def _valid_scm_version(value: str) -> bool:
        return bool(_SCM_VERSION_LITERAL.fullmatch(value))

    @staticmethod
    def _project_commands(commands: Iterable[ExtractedCommand]) -> tuple[ProjectCommand, ...]:
        unique: dict[tuple[str, CommandPurpose], ExtractedCommand] = {}
        for command in commands:
            key = (
                re.sub(r"\s+", " ", command.text.strip()).lower(),
                command.purpose,
            )
            current = unique.get(key)
            if current is None or command.confidence > current.confidence:
                unique[key] = command

        counters: dict[CommandPurpose, int] = {}
        result: list[ProjectCommand] = []
        for command in unique.values():
            counters[command.purpose] = counters.get(command.purpose, 0) + 1
            result.append(
                ProjectCommand(
                    name=f"{command.purpose.value}-{counters[command.purpose]}",
                    command=CommandSpec(
                        argv=(command.text,),
                        purpose=command.purpose,
                        shell=True,
                    ),
                    source=command.source,
                    confidence=command.confidence,
                )
            )
        return tuple(result)

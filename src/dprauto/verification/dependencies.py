"""Static, bounded dependency closure for a selected Python test slice."""

from __future__ import annotations

import ast
import re
import shlex
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from dprauto.domain.models import ProjectProfile

_CANONICAL_IMPORTS = {
    "beautifulsoup4": ("bs4",),
    "dataclasses-json": ("dataclasses_json",),
    "gitpython": ("git",),
    "opencv-python": ("cv2",),
    "pillow": ("PIL",),
    "pytest-env": (),
    "pytest-git": ("pytest_git",),
    "pytest-mock": (),
    "python-dateutil": ("dateutil",),
    "pyyaml": ("yaml",),
    "requests-cache": ("requests_cache",),
    "scikit-learn": ("sklearn",),
}
_FIXTURE_DISTRIBUTIONS = {
    "aiohttp_client": "pytest-aiohttp",
    "benchmark": "pytest-benchmark",
    "django_db_blocker": "pytest-django",
    "git_repo": "pytest-git",
    "httpserver": "pytest-httpserver",
    "mocker": "pytest-mock",
    "requests_mock": "requests-mock",
    "snapshot": "syrupy",
}
_PYTEST_CONFIG_DISTRIBUTIONS = {
    "asyncio_mode": "pytest-asyncio",
    "DJANGO_SETTINGS_MODULE": "pytest-django",
    "env": "pytest-env",
}
_DISTRIBUTION_EXECUTABLES = {"pytest-git": "git"}
_REQUIREMENT_NAME = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)")
_SAFE_REQUIREMENT = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*"
    r"(?:\[[A-Za-z0-9._,-]+\])?"
    r"(?:\s*(?:===|==|!=|~=|>=|<=|>|<)\s*[^\s;]+"
    r"(?:\s*,\s*(?:===|==|!=|~=|>=|<=|>|<)\s*[^\s;]+)*)?"
    r"(?:\s*;\s*[^#]+)?$"
)


@dataclass(frozen=True, slots=True)
class TestDependencyPlan:
    applied: bool = False
    mode: str = "declared-source"
    source: str = ""
    install_commands: tuple[str, ...] = ()
    targets: tuple[str, ...] = ()
    analyzed_files: tuple[str, ...] = ()
    import_roots: tuple[str, ...] = ()
    selected_requirements: tuple[str, ...] = ()
    unresolved_imports: tuple[str, ...] = ()
    excluded_targets: tuple[str, ...] = ()
    required_executables: tuple[str, ...] = ()
    additive: bool = False
    reason: str = "use the project's declared test dependency source"


@dataclass(frozen=True, slots=True)
class _RequirementEntry:
    distribution: str
    raw: str
    import_roots: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _SliceAnalysis:
    target: str
    analyzed_files: tuple[str, ...]
    import_roots: tuple[str, ...]
    fixture_names: tuple[str, ...]
    unresolved_imports: tuple[str, ...]
    required_distributions: tuple[str, ...]


class TestDependencyPlanner:
    """Slice broad dev requirements using only repository-owned evidence."""

    __test__ = False

    def __init__(self, *, max_analysis_files: int = 256, max_test_files: int = 8) -> None:
        if not 16 <= max_analysis_files <= 2_048:
            raise ValueError("max_analysis_files must be between 16 and 2048")
        if not 1 <= max_test_files <= 32:
            raise ValueError("max_test_files must be between 1 and 32")
        self.max_analysis_files = max_analysis_files
        self.max_test_files = max_test_files

    def plan(
        self,
        profile: ProjectProfile,
        workspace: Path,
        targets: tuple[str, ...],
        *,
        command: str,
    ) -> TestDependencyPlan:
        if not targets or not re.search(
            r"\b(?:pytest|py\.test)\b", command, re.IGNORECASE
        ):
            return TestDependencyPlan(targets=targets)
        source = self._broad_requirement_source(profile)
        if not source:
            return self._optional_and_config_augmentation(
                profile,
                workspace,
                targets,
            )
        entries = self._requirements(workspace, source)
        if entries is None:
            return TestDependencyPlan(
                source=source,
                targets=targets,
                reason="broad requirements use includes, hashes, URLs, or unsupported syntax",
            )
        if not entries:
            return TestDependencyPlan(
                source=source,
                targets=targets,
                reason="broad requirements file is empty",
            )

        runtime_roots = self._runtime_import_roots(profile)
        entry_by_import: dict[str, _RequirementEntry] = {}
        entry_by_distribution = {entry.distribution: entry for entry in entries}
        for entry in entries:
            for root in entry.import_roots:
                entry_by_import.setdefault(root.casefold(), entry)
        config_distributions = self._pytest_config_distributions(workspace)
        provided_roots = set(runtime_roots) | {"pytest"}

        cache: dict[str, _SliceAnalysis] = {}

        def analyze(target: str) -> _SliceAnalysis:
            cached = cache.get(target)
            if cached is not None:
                return cached
            result = self._analyze_target(
                workspace,
                target,
                provided_roots=provided_roots,
                entry_by_import=entry_by_import,
                entry_by_distribution=entry_by_distribution,
                config_distributions=config_distributions,
            )
            cache[target] = result
            return result

        initial = tuple(analyze(target) for target in targets)
        selected_targets = targets
        excluded: list[str] = []
        if any(item.unresolved_imports for item in initial):
            safe_files = self._metadata_paths(profile, "safe_test_files")
            eligible: list[str] = []
            for target in safe_files[:64]:
                analysis = analyze(target)
                if analysis.unresolved_imports:
                    excluded.append(f"{target}:{','.join(analysis.unresolved_imports)}")
                else:
                    eligible.append(target)
            selected_targets = self._representative_targets(tuple(eligible))
            if not selected_targets:
                unresolved = tuple(
                    sorted({root for item in initial for root in item.unresolved_imports})
                )
                return TestDependencyPlan(
                    source=source,
                    targets=targets,
                    unresolved_imports=unresolved,
                    excluded_targets=tuple(excluded[:64]),
                    reason="no bounded test slice has a complete declared dependency closure",
                )

        selected = tuple(analyze(target) for target in selected_targets)
        unresolved = tuple(sorted({root for item in selected for root in item.unresolved_imports}))
        if unresolved:
            return TestDependencyPlan(
                source=source,
                targets=targets,
                unresolved_imports=unresolved,
                excluded_targets=tuple(excluded[:64]),
                reason="selected test imports are not covered by project declarations",
            )
        distributions = {"pytest"}
        distributions.update(config_distributions)
        for item in selected:
            distributions.update(item.required_distributions)
        selected_entries = tuple(entry for entry in entries if entry.distribution in distributions)
        missing_distributions = tuple(
            sorted(distributions - {entry.distribution for entry in selected_entries})
        )
        if missing_distributions:
            return TestDependencyPlan(
                source=source,
                targets=targets,
                unresolved_imports=missing_distributions,
                excluded_targets=tuple(excluded[:64]),
                reason="pytest plugin or fixture dependency is absent from broad requirements",
            )
        requirements = tuple(entry.raw for entry in selected_entries)
        required_executables = tuple(
            dict.fromkeys(
                _DISTRIBUTION_EXECUTABLES[entry.distribution]
                for entry in selected_entries
                if entry.distribution in _DISTRIBUTION_EXECUTABLES
            )
        )
        rendered = " ".join(shlex.quote(value) for value in requirements)
        analyzed_files = tuple(
            dict.fromkeys(path for item in selected for path in item.analyzed_files)
        )
        import_roots = tuple(sorted({root for item in selected for root in item.import_roots}))
        changed = selected_targets != targets
        return TestDependencyPlan(
            applied=True,
            mode="minimal-slice",
            source=source,
            install_commands=(f"python -m pip install {rendered}",),
            targets=selected_targets,
            analyzed_files=analyzed_files,
            import_roots=import_roots,
            selected_requirements=requirements,
            excluded_targets=tuple(excluded[:64]),
            required_executables=required_executables,
            reason=(
                "reselected dependency-complete test files and sliced broad dev requirements"
                if changed
                else "sliced broad dev requirements to imports used by selected tests"
            ),
        )

    def _optional_and_config_augmentation(
        self,
        profile: ProjectProfile,
        workspace: Path,
        targets: tuple[str, ...],
    ) -> TestDependencyPlan:
        raw_groups = profile.metadata.get("optional_dependency_groups", {})
        groups = raw_groups if isinstance(raw_groups, Mapping) else {}
        entries: list[_RequirementEntry] = []
        group_by_distribution: dict[str, str] = {}
        for group, values in list(groups.items())[:64]:
            if not isinstance(group, str) or not isinstance(values, (list, tuple)):
                continue
            for value in values[:256]:
                if not isinstance(value, str):
                    continue
                distribution = self._canonical(value)
                if not _REQUIREMENT_NAME.fullmatch(distribution):
                    continue
                roots = _CANONICAL_IMPORTS.get(
                    distribution,
                    (distribution.replace("-", "_"),),
                )
                entries.append(_RequirementEntry(distribution, distribution, roots))
                group_by_distribution.setdefault(distribution, group)

        config_distributions = self._pytest_config_distributions(workspace)
        if not entries and not config_distributions:
            return TestDependencyPlan(targets=targets)
        entry_by_import = {
            root.casefold(): entry
            for entry in entries
            for root in entry.import_roots
        }
        entry_by_distribution = {entry.distribution: entry for entry in entries}
        provided_roots = self._runtime_import_roots(profile) | {"pytest"}
        analyses = tuple(
            self._analyze_target(
                workspace,
                target,
                provided_roots=provided_roots,
                entry_by_import=entry_by_import,
                entry_by_distribution=entry_by_distribution,
                config_distributions=(),
            )
            for target in targets
        )
        required = {
            distribution
            for item in analyses
            for distribution in item.required_distributions
        }
        selected_groups = tuple(
            dict.fromkeys(
                group_by_distribution[distribution]
                for distribution in sorted(required)
                if distribution in group_by_distribution
            )
        )
        declared_group_distributions: set[str] = set()
        for mapping_name, selection_name in (
            ("optional_dependency_groups", "test_dependency_extras"),
            ("manager_dependency_groups", "test_dependency_manager_groups"),
        ):
            mapping = profile.metadata.get(mapping_name, {})
            selected = profile.metadata.get(selection_name, ())
            if not isinstance(mapping, Mapping) or not isinstance(selected, (list, tuple)):
                continue
            for group in selected:
                values = mapping.get(group, ())
                if not isinstance(values, (list, tuple)):
                    continue
                declared_group_distributions.update(
                    self._canonical(value)
                    for value in values
                    if isinstance(value, str)
                )
        direct_plugins = tuple(
            distribution
            for distribution in config_distributions
            if distribution not in required
            and distribution not in declared_group_distributions
            and distribution not in self._runtime_import_roots(profile)
        )
        commands: list[str] = []
        if selected_groups:
            rendered = ",".join(selected_groups)
            commands.append(f"python -m pip install {shlex.quote(f'.[{rendered}]')}")
        if direct_plugins:
            rendered = " ".join(shlex.quote(item) for item in direct_plugins)
            commands.append(f"python -m pip install {rendered}")
        if not commands:
            return TestDependencyPlan(targets=targets)
        return TestDependencyPlan(
            applied=True,
            mode="repository-evidenced-augmentation",
            source="project extras and pytest configuration",
            install_commands=tuple(commands),
            targets=targets,
            analyzed_files=tuple(
                dict.fromkeys(path for item in analyses for path in item.analyzed_files)
            ),
            import_roots=tuple(
                sorted({root for item in analyses for root in item.import_roots})
            ),
            selected_requirements=tuple(
                (*selected_groups, *direct_plugins)
            ),
            additive=True,
            reason="install only extras and pytest plugins evidenced by the selected test slice",
        )

    @staticmethod
    def _broad_requirement_source(profile: ProjectProfile) -> str:
        metadata = profile.metadata
        if metadata.get("test_dependency_extras") or metadata.get("test_dependency_manager_groups"):
            return ""
        candidates: list[str] = []
        for path in profile.dependency_files:
            name = PurePosixPath(path).name.casefold()
            if not name.endswith((".txt", ".in")):
                continue
            if re.search(r"(?:^|[-_.])(dev|develop|development|qa)(?:[-_.]|$)", name):
                candidates.append(path)
        if not candidates:
            return ""
        return min(candidates, key=lambda path: (len(PurePosixPath(path).parts), path))

    @staticmethod
    def _requirements(workspace: Path, source: str) -> tuple[_RequirementEntry, ...] | None:
        target = TestDependencyPlanner._safe_file(workspace, source)
        if target is None:
            return None
        try:
            if target.stat().st_size > 256 * 1024:
                return None
            content = target.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeError):
            return None
        if re.search(r"(?m)^\s*(?:-r|--requirement|-c|--constraint|--hash|--index-url)", content):
            return None
        if "\\\n" in content:
            return None
        entries: list[_RequirementEntry] = []
        for line in content.splitlines():
            value = line.split("#", 1)[0].strip()
            if not value:
                continue
            if not _SAFE_REQUIREMENT.fullmatch(value):
                return None
            match = _REQUIREMENT_NAME.match(value)
            if not match:
                return None
            distribution = TestDependencyPlanner._canonical(match.group(1))
            roots = _CANONICAL_IMPORTS.get(
                distribution,
                (distribution.replace("-", "_"),),
            )
            entries.append(_RequirementEntry(distribution, value, roots))
        return tuple(entries[:256])

    def _analyze_target(
        self,
        workspace: Path,
        target: str,
        *,
        provided_roots: set[str],
        entry_by_import: Mapping[str, _RequirementEntry],
        entry_by_distribution: Mapping[str, _RequirementEntry],
        config_distributions: tuple[str, ...],
    ) -> _SliceAnalysis:
        initial = [target, *self._ancestor_conftests(workspace, target)]
        queue = list(dict.fromkeys(initial))
        visited: list[str] = []
        external: set[str] = set()
        fixtures: set[str] = set()
        while queue and len(visited) < self.max_analysis_files:
            relative = queue.pop(0)
            if relative in visited:
                continue
            path = self._safe_file(workspace, relative)
            if path is None or path.suffix.casefold() != ".py":
                continue
            visited.append(relative)
            pure = PurePosixPath(relative)
            imports, names = self._file_evidence(
                path,
                collect_fixtures=any(part.casefold() in {"test", "tests"} for part in pure.parts)
                or pure.name.casefold() == "conftest.py",
            )
            fixtures.update(names)
            for module, level, aliases in imports:
                local = self._local_import_paths(workspace, relative, module, level, aliases)
                if local:
                    queue.extend(path for path in local if path not in visited)
                    continue
                root = (module or (aliases[0] if aliases else "")).split(".", 1)[0]
                if root:
                    external.add(root)

        required_distributions: set[str] = set(config_distributions)
        unresolved: set[str] = set()
        stdlib = getattr(sys, "stdlib_module_names", frozenset())
        for root in external:
            normalized = root.casefold()
            if normalized in stdlib or normalized in provided_roots:
                continue
            entry = entry_by_import.get(normalized)
            if entry is None:
                unresolved.add(root)
            else:
                required_distributions.add(entry.distribution)
        for fixture in fixtures:
            distribution = _FIXTURE_DISTRIBUTIONS.get(fixture)
            if not distribution:
                continue
            canonical = self._canonical(distribution)
            if canonical in entry_by_distribution:
                required_distributions.add(canonical)
            else:
                unresolved.add(distribution)
        return _SliceAnalysis(
            target,
            tuple(visited),
            tuple(sorted(external)),
            tuple(sorted(fixtures)),
            tuple(sorted(unresolved)),
            tuple(sorted(required_distributions)),
        )

    @staticmethod
    def _file_evidence(
        path: Path,
        *,
        collect_fixtures: bool,
    ) -> tuple[tuple[tuple[str, int, tuple[str, ...]], ...], tuple[str, ...]]:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError):
            return (), ()
        imports: list[tuple[str, int, tuple[str, ...]]] = []
        fixtures: list[str] = []

        def visit(statements: list[ast.stmt]) -> None:
            for statement in statements:
                if isinstance(statement, ast.Import):
                    imports.extend((alias.name, 0, ()) for alias in statement.names)
                elif isinstance(statement, ast.ImportFrom):
                    imports.append(
                        (
                            statement.module or "",
                            statement.level,
                            tuple(alias.name for alias in statement.names),
                        )
                    )
                elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if collect_fixtures:
                        fixtures.extend(argument.arg for argument in statement.args.args)
                        fixtures.extend(argument.arg for argument in statement.args.kwonlyargs)
                elif isinstance(statement, ast.ClassDef):
                    visit(statement.body)
                elif isinstance(statement, ast.If):
                    if not TestDependencyPlanner._is_type_checking(statement.test):
                        visit(statement.body)
                    visit(statement.orelse)
                elif isinstance(statement, ast.Try):
                    optional = any(
                        handler.type is None
                        or (
                            isinstance(handler.type, ast.Name)
                            and handler.type.id in {"ImportError", "ModuleNotFoundError"}
                        )
                        for handler in statement.handlers
                    )
                    # A guarded import whose handler immediately raises is a
                    # repository-declared hard test dependency, not an
                    # optional feature that collection can safely skip.
                    handler_raises = any(
                        any(isinstance(node, ast.Raise) for node in ast.walk(handler))
                        for handler in statement.handlers
                    )
                    if not optional or handler_raises:
                        visit(statement.body)
                    visit(statement.orelse)
                    visit(statement.finalbody)
                elif isinstance(statement, (ast.With, ast.AsyncWith)):
                    visit(statement.body)

        visit(tree.body)
        return tuple(imports), tuple(dict.fromkeys(fixtures))

    @staticmethod
    def _is_type_checking(expression: ast.expr) -> bool:
        return (isinstance(expression, ast.Name) and expression.id == "TYPE_CHECKING") or (
            isinstance(expression, ast.Attribute) and expression.attr == "TYPE_CHECKING"
        )

    @staticmethod
    def _local_import_paths(
        workspace: Path,
        current: str,
        module: str,
        level: int,
        aliases: tuple[str, ...],
    ) -> tuple[str, ...]:
        current_parts = list(PurePosixPath(current).with_suffix("").parts[:-1])
        if level:
            keep = max(0, len(current_parts) - level + 1)
            parts = current_parts[:keep]
            if module:
                parts.extend(module.split("."))
        else:
            parts = module.split(".") if module else []
        candidates: list[list[str]] = []
        if parts:
            candidates.append(parts)
        for alias in aliases:
            if alias == "*":
                continue
            candidates.append([*parts, *alias.split(".")])
        resolved: list[str] = []
        for candidate in candidates:
            for prefix in ((), ("src",)):
                base = workspace.joinpath(*prefix, *candidate)
                for path in (base.with_suffix(".py"), base / "__init__.py"):
                    try:
                        relative = path.relative_to(workspace).as_posix()
                    except ValueError:
                        continue
                    if path.is_file():
                        resolved.append(relative)
        return tuple(dict.fromkeys(resolved))

    @staticmethod
    def _ancestor_conftests(workspace: Path, target: str) -> tuple[str, ...]:
        path = PurePosixPath(target).parent
        candidates: list[str] = []
        while True:
            candidate = (path / "conftest.py").as_posix()
            if (workspace / candidate).is_file():
                candidates.append(candidate)
            if path.as_posix() == ".":
                break
            path = path.parent
        return tuple(reversed(candidates))

    @staticmethod
    def _runtime_import_roots(profile: ProjectProfile) -> set[str]:
        values = profile.metadata.get("runtime_dependency_names", ())
        roots: set[str] = set()
        if isinstance(values, (list, tuple)):
            for value in values[:1_024]:
                if not isinstance(value, str):
                    continue
                canonical = TestDependencyPlanner._canonical(value)
                mapped = _CANONICAL_IMPORTS.get(canonical, (canonical.replace("-", "_"),))
                roots.update(root.casefold() for root in mapped)
        return roots

    @staticmethod
    def _pytest_config_distributions(workspace: Path) -> tuple[str, ...]:
        distributions: list[str] = []
        for relative in ("pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini"):
            config = TestDependencyPlanner._safe_file(workspace, relative)
            if config is None:
                continue
            try:
                content = config.read_text(encoding="utf-8", errors="replace")
            except OSError:
                content = ""
            for option, distribution in _PYTEST_CONFIG_DISTRIBUTIONS.items():
                if re.search(
                    rf"(?mi)(?:^\s*{re.escape(option)}\s*=|--{re.escape(option.replace('_', '-'))}\b)",
                    content,
                ):
                    distributions.append(distribution)
        return tuple(dict.fromkeys(distributions))

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
            if path.is_absolute() or ".." in path.parts or path.suffix != ".py":
                continue
            selected.append(path.as_posix())
        return tuple(dict.fromkeys(selected))

    @staticmethod
    def _safe_file(workspace: Path, relative: str) -> Path | None:
        pure = PurePosixPath(relative)
        if pure.is_absolute() or ".." in pure.parts:
            return None
        root = workspace.expanduser().resolve()
        target = (root / pure.as_posix()).resolve()
        if root not in target.parents or not target.is_file() or target.is_symlink():
            return None
        return target

    @staticmethod
    def _canonical(value: str) -> str:
        return re.sub(r"[-_.]+", "-", value).casefold()

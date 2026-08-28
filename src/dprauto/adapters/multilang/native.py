"""Rule-first C and C++ project inspection."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from dprauto.adapters.multilang.common import (
    ci_files,
    depth,
    dockerfiles,
    extracted_commands,
    project_commands,
    readme_files,
    safe_subproject,
)
from dprauto.adapters.multilang.detector import RepositoryLanguageDetector
from dprauto.domain.enums import CommandPurpose, ProjectType
from dprauto.domain.models import ProjectProfile, SourceReference
from dprauto.errors import ProjectParsingError
from dprauto.inspection.commands import CommandExtractor, ExtractedCommand
from dprauto.inspection.scanner import ScannedProject

NATIVE_ROOT_MARKERS = {
    "CMakeLists.txt",
    "Makefile",
    "configure",
    "configure.ac",
    "configure.in",
    "meson.build",
}


class NativeProjectParser:
    """Create a normalized profile for CMake, Meson, Autotools, and Make."""

    name = "native-rules-v1"
    priority = 200

    def __init__(
        self,
        command_extractor: CommandExtractor | None = None,
        language_detector: RepositoryLanguageDetector | None = None,
    ) -> None:
        self.command_extractor = command_extractor or CommandExtractor()
        self.language_detector = language_detector or RepositoryLanguageDetector()

    def supports(self, scanned: ScannedProject) -> bool:
        has_build_root = any(
            depth(path) == 1 and PurePosixPath(path).name in NATIVE_ROOT_MARKERS
            for path in scanned.files
        )
        languages = self.language_detector.counts(scanned)
        return has_build_root and bool({"C", "C++"} & languages.keys())

    def parse_scanned(
        self,
        source: SourceReference,
        scanned: ScannedProject,
    ) -> ProjectProfile:
        if not self.supports(scanned):
            raise ProjectParsingError(f"no root native build indicators found in {scanned.root}")

        build_systems = self._build_systems(scanned)
        build_files = self._build_files(scanned)
        dependencies = self._dependency_files(scanned)
        readmes = readme_files(scanned)
        workflows = ci_files(scanned)
        project_name = self._project_name(scanned, build_systems) or scanned.root.name
        project_type = self._project_type(scanned, build_systems, project_name)
        inferred = self._inferred_commands(
            scanned,
            build_systems,
            project_type=project_type,
            project_name=project_name,
        )
        discovered = extracted_commands(
            scanned,
            readmes,
            workflows,
            self.command_extractor,
        )
        if project_type is ProjectType.LIBRARY:
            discovered = tuple(
                command
                for command in discovered
                if command.purpose is not CommandPurpose.RUN
            )
        commands = project_commands(
            (*discovered, *inferred)
        )
        language_counts = self.language_detector.counts(scanned)
        languages = tuple(
            language
            for language in self.language_detector.languages(scanned)
            if language in {"C", "C++"}
        )
        standards = self._language_standards(scanned)
        subprojects = self._subprojects(scanned, build_systems)
        cmake_arguments = self._cmake_configuration_arguments(scanned, build_systems)
        cmake_test_target = self._cmake_test_build_target(scanned, build_systems)
        system_packages = self._system_dependency_packages(scanned, build_systems)
        test_executables, test_executable_evidence = self._test_required_executables(scanned)
        project_id = f"{project_name}@{source.revision}" if source.revision else project_name
        build_pipeline = tuple(
            command.command.display
            for command in commands
            if command.command.purpose is CommandPurpose.BUILD
            and command.source.startswith("inferred:")
        )
        test_commands = tuple(
            command.command.display
            for command in commands
            if command.command.purpose is CommandPurpose.TEST
        )
        return ProjectProfile(
            project_id=project_id,
            source=source,
            languages=languages,
            project_type=project_type,
            runtime_constraints=standards,
            package_managers=build_systems,
            dependency_files=dependencies,
            build_files=build_files,
            # Nested native Dockerfiles usually build a test harness,
            # benchmark, packaging artifact, or cross-platform helper. Only a
            # root Dockerfile represents this selected native workspace.
            dockerfiles=tuple(path for path in dockerfiles(scanned) if depth(path) == 1),
            readme_files=readmes,
            ci_files=workflows,
            commands=commands,
            metadata={
                "parser": self.name,
                "project_name": project_name,
                "build_systems": build_systems,
                "primary_build_system": build_systems[0],
                "subprojects": subprojects,
                "working_directories": (".", *subprojects),
                "language_file_counts": language_counts,
                "build_pipeline": build_pipeline,
                "cmake_configuration_arguments": cmake_arguments,
                "cmake_test_build_target": cmake_test_target,
                "system_dependency_packages": system_packages,
                "test_required_executables": test_executables,
                "test_prerequisite_evidence": test_executable_evidence,
                "test_commands": test_commands,
                "scan_file_count": len(scanned.files),
                "scan_skipped_files": scanned.skipped_files,
                "scan_truncated": scanned.truncated,
            },
        )

    @staticmethod
    def _root_names(scanned: ScannedProject) -> set[str]:
        return {
            PurePosixPath(path).name
            for path in scanned.files
            if depth(path) == 1
        }

    @classmethod
    def _build_systems(cls, scanned: ScannedProject) -> tuple[str, ...]:
        root_names = cls._root_names(scanned)
        systems = []
        if "CMakeLists.txt" in root_names:
            systems.append("cmake")
        if "meson.build" in root_names:
            systems.append("meson")
        if root_names & {"configure", "configure.ac", "configure.in"}:
            systems.append("autotools")
        if "Makefile" in root_names:
            systems.append("make")
        return tuple(systems)

    @staticmethod
    def _build_files(scanned: ScannedProject) -> tuple[str, ...]:
        names = {
            "CMakeLists.txt",
            "Makefile",
            "Makefile.am",
            "autogen.sh",
            "configure",
            "configure.ac",
            "configure.in",
            "meson.build",
            "meson_options.txt",
        }
        return tuple(
            path
            for path in scanned.files
            if depth(path) <= 4 and PurePosixPath(path).name in names
        )

    @staticmethod
    def _dependency_files(scanned: ScannedProject) -> tuple[str, ...]:
        names = {
            "CMakePresets.json",
            "conanfile.py",
            "conanfile.txt",
            "vcpkg.json",
            "vcpkg-configuration.json",
        }
        return tuple(
            path
            for path in scanned.files
            if depth(path) <= 3
            and (
                PurePosixPath(path).name in names
                or PurePosixPath(path).suffix.casefold() == ".wrap"
            )
        )

    @staticmethod
    def _project_name(scanned: ScannedProject, systems: tuple[str, ...]) -> str:
        if "cmake" in systems:
            match = re.search(
                r"(?im)^\s*project\s*\(\s*['\"]?([A-Za-z0-9_.+-]+)",
                scanned.read_text("CMakeLists.txt"),
            )
            if match:
                return match.group(1)
        if "meson" in systems:
            match = re.search(
                r"(?im)^\s*project\s*\(\s*['\"]([^'\"]+)",
                scanned.read_text("meson.build"),
            )
            if match:
                return match.group(1)
        if "autotools" in systems:
            configure = scanned.read_text("configure.ac") or scanned.read_text("configure.in")
            match = re.search(r"AC_INIT\s*\(\s*\[?([^,\]\s]+)", configure)
            if match:
                return match.group(1)
        return ""

    @staticmethod
    def _project_type(
        scanned: ScannedProject,
        systems: tuple[str, ...],
        project_name: str,
    ) -> ProjectType:
        """Distinguish root CLI targets from libraries and developer tools."""

        escaped = re.escape(project_name)
        if "cmake" in systems and re.search(
            rf"(?im)^\s*add_executable\s*\(\s*{escaped}(?:\s|\))",
            scanned.read_text("CMakeLists.txt"),
        ):
            return ProjectType.CLI
        if "autotools" in systems:
            makefile = scanned.read_text("Makefile.am") or scanned.read_text(
                "Makefile.in"
            )
            lines = makefile.splitlines()
            declared_programs: list[str] = []
            for index, line in enumerate(lines):
                match = re.match(r"^\s*(?:bin|sbin)_PROGRAMS\s*=\s*(.*)$", line)
                if not match:
                    continue
                value = match.group(1)
                parts = [value]
                while parts[-1].rstrip().endswith("\\") and index + 1 < len(lines):
                    index += 1
                    parts.append(lines[index])
                declared_programs.append("\n".join(parts))
            programs = "\n".join(declared_programs)
            if re.search(
                rf"(?<![A-Za-z0-9_.+-]){escaped}(?:@EXEEXT@)?(?![A-Za-z0-9_.+-])",
                programs,
            ):
                return ProjectType.CLI
        if "make" in systems and re.search(
            rf"(?m)^\s*{escaped}\s*:",
            scanned.read_text("Makefile"),
        ):
            shallow_sources = "\n".join(
                scanned.read_text(path)
                for path in scanned.files
                if depth(path) <= 2
                and PurePosixPath(path).suffix.casefold() in {".c", ".cc", ".cpp", ".cxx"}
            )
            if re.search(r"\bmain\s*\(", shallow_sources):
                return ProjectType.CLI
        return ProjectType.LIBRARY

    @staticmethod
    def _runtime_command(
        scanned: ScannedProject,
        systems: tuple[str, ...],
        project_name: str,
    ) -> str:
        evidence = "\n".join(
            scanned.read_text(path)
            for path in scanned.files
            if depth(path) <= 2
            and (
                PurePosixPath(path).name.casefold().startswith("readme")
                or PurePosixPath(path).suffix.casefold() in {".c", ".cc", ".cpp", ".cxx"}
            )
        )
        executable = f"build/{project_name}" if "cmake" in systems else f"./{project_name}"
        if "--version" in evidence:
            return f"{executable} --version"
        if re.search(r"(?m)(?:['\"\s])-h(?:['\"\s]|$)", evidence):
            return f"{executable} -h"
        if "--help" in evidence:
            return f"{executable} --help"
        return ""

    @staticmethod
    def _language_standards(scanned: ScannedProject) -> dict[str, str]:
        cmake = scanned.read_text("CMakeLists.txt")
        constraints: dict[str, str] = {}
        c_standard = re.search(
            r"(?i)(?:CMAKE_C_STANDARD|C_STANDARD)\s+(\d{2})", cmake
        )
        cpp_standard = re.search(
            r"(?i)(?:CMAKE_CXX_STANDARD|CXX_STANDARD)\s+(\d{2})", cmake
        )
        if c_standard:
            constraints["c_standard"] = c_standard.group(1)
        if cpp_standard:
            constraints["cpp_standard"] = cpp_standard.group(1)
        return constraints

    @staticmethod
    def _cmake_configuration_arguments(
        scanned: ScannedProject,
        systems: tuple[str, ...],
    ) -> tuple[str, ...]:
        if "cmake" not in systems:
            return ()
        cmake = scanned.read_text("CMakeLists.txt")
        selected: list[str] = []
        for match in re.finditer(
            r"(?is)\boption\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s+"
            r"(?:\"([^\"]*)\"|'([^']*)')",
            cmake,
        ):
            name = match.group(1)
            normalized_name = name.casefold()
            test_switch = "test" in normalized_name or "regress" in normalized_name
            developer_test_switch = (
                normalized_name.endswith("developer_mode")
                and bool(re.search(r"(?i)\benable_testing\s*\(", cmake))
            )
            if not test_switch and not developer_test_switch:
                continue
            value = "OFF" if "disable" in normalized_name else "ON"
            selected.append(f"-D{name}={value}")
        dependency_files = "\n".join(
            scanned.read_text(path)
            for path in scanned.files
            if len(PurePosixPath(path).parts) <= 3
            and path.casefold().endswith((".cmake", "cmakelists.txt"))
        )
        if re.search(r"(?is)\bset\s*\(\s*DEPS\s+.*?CACHE\s+STRING", dependency_files) and re.search(
            r"(?is)\bDEPS\b.*?\bDOWNLOAD\b", dependency_files
        ):
            selected.append("-DDEPS=DOWNLOAD")
        return tuple(dict.fromkeys(selected))

    @staticmethod
    def _system_dependency_packages(
        scanned: ScannedProject,
        systems: tuple[str, ...],
    ) -> tuple[str, ...]:
        selected: list[str] = []
        if "cmake" in systems:
            cmake = "\n".join(
                scanned.read_text(path)
                for path in scanned.files
                if len(PurePosixPath(path).parts) <= 3
                and path.casefold().endswith((".cmake", "cmakelists.txt"))
            )
            for package, apt_package in (
                ("OpenSSL", "libssl-dev"),
                ("ZLIB", "zlib1g-dev"),
                ("LibLZMA", "liblzma-dev"),
            ):
                if re.search(rf"(?i)\bfind_package\s*\(\s*{package}\b", cmake):
                    selected.append(apt_package)
        if "autotools" in systems:
            configure = scanned.read_text("configure.ac") or scanned.read_text("configure.in")
            if re.search(r"\b(?:AM_PATH_PYTHON|PYTHON)\b", configure):
                selected.append("python3")
        return tuple(dict.fromkeys(selected))

    @staticmethod
    def _cmake_test_build_target(
        scanned: ScannedProject,
        systems: tuple[str, ...],
    ) -> str:
        if "cmake" not in systems:
            return ""
        cmake = scanned.read_text("CMakeLists.txt")
        for target in ("all_tests", "tests", "check"):
            if re.search(
                rf"(?im)^\s*add_custom_target\s*\(\s*{re.escape(target)}(?:\s|\))",
                cmake,
            ):
                return target
        return ""

    @staticmethod
    def _test_required_executables(
        scanned: ScannedProject,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        test_driver_text = "\n".join(
            scanned.read_text(path)
            for path in scanned.files
            if PurePosixPath(path).name.casefold()
            in {"makefile", "gnumakefile", "cmakelists.txt", "meson.build"}
            or PurePosixPath(path).suffix.casefold() == ".mk"
        )
        executables: list[str] = []
        evidence: list[str] = []
        for path in scanned.files:
            parts = PurePosixPath(path).parts
            if not any(part.casefold() in {"test", "tests"} for part in parts[:-1]):
                continue
            # A shebang in an optional helper is not part of the selected
            # Testability dependency contract. Require the ordinary native
            # build/test driver to reference this script path.
            if path not in test_driver_text and f"./{path}" not in test_driver_text:
                continue
            first_line = scanned.read_text(path).splitlines()[:1]
            if not first_line or not first_line[0].startswith("#!"):
                continue
            shebang = first_line[0][2:].strip().split()
            if not shebang:
                continue
            executable = PurePosixPath(shebang[0]).name
            if executable == "env" and len(shebang) > 1:
                executable = shebang[1]
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}", executable):
                continue
            executables.append(executable)
            evidence.append(f"shebang:{path}:{executable}")
        return tuple(dict.fromkeys(executables)), tuple(dict.fromkeys(evidence))

    @staticmethod
    def _subprojects(
        scanned: ScannedProject,
        systems: tuple[str, ...],
    ) -> tuple[str, ...]:
        selected: list[str] = []
        if "cmake" in systems:
            for value in re.findall(
                r"(?im)^\s*add_subdirectory\s*\(\s*([^\s\)]+)",
                scanned.read_text("CMakeLists.txt"),
            ):
                normalized = safe_subproject(value)
                if normalized:
                    selected.append(normalized)
        if "meson" in systems:
            for value in re.findall(
                r"(?im)^\s*subdir\s*\(\s*['\"]([^'\"]+)",
                scanned.read_text("meson.build"),
            ):
                normalized = safe_subproject(value)
                if normalized:
                    selected.append(normalized)
        return tuple(dict.fromkeys(selected))

    @staticmethod
    def _has_tests(scanned: ScannedProject) -> bool:
        if any(
            part.casefold() in {"test", "tests", "unittest", "unittests"}
            for path in scanned.files
            for part in PurePosixPath(path).parts[:-1]
        ):
            return True
        cmake = scanned.read_text("CMakeLists.txt")
        return bool(re.search(r"\b(?:enable_testing|add_test)\s*\(", cmake))

    @classmethod
    def _inferred_commands(
        cls,
        scanned: ScannedProject,
        systems: tuple[str, ...],
        *,
        project_type: ProjectType,
        project_name: str,
    ) -> tuple[ExtractedCommand, ...]:
        commands: list[ExtractedCommand] = []
        if "cmake" in systems:
            commands.extend(
                (
                    ExtractedCommand(
                        "cmake -S . -B build",
                        CommandPurpose.BUILD,
                        "inferred:CMakeLists.txt",
                        0.9,
                    ),
                    ExtractedCommand(
                        "cmake --build build",
                        CommandPurpose.BUILD,
                        "inferred:CMakeLists.txt",
                        0.9,
                    ),
                )
            )
            if cls._has_tests(scanned):
                commands.append(
                    ExtractedCommand(
                        "ctest --test-dir build --output-on-failure",
                        CommandPurpose.TEST,
                        "inferred:CMakeLists.txt:test-layout",
                        0.85,
                    )
                )
        elif "meson" in systems:
            commands.extend(
                (
                    ExtractedCommand(
                        "meson setup build",
                        CommandPurpose.BUILD,
                        "inferred:meson.build",
                        0.9,
                    ),
                    ExtractedCommand(
                        "meson compile -C build",
                        CommandPurpose.BUILD,
                        "inferred:meson.build",
                        0.9,
                    ),
                    ExtractedCommand(
                        "meson test -C build --print-errorlogs",
                        CommandPurpose.TEST,
                        "inferred:meson.build",
                        0.85,
                    ),
                )
            )
        elif "autotools" in systems:
            if scanned.has("autogen.sh"):
                commands.append(
                    ExtractedCommand(
                        "./autogen.sh",
                        CommandPurpose.BUILD,
                        "inferred:autogen.sh",
                        0.9,
                    )
                )
            commands.extend(
                (
                    ExtractedCommand(
                        "./configure",
                        CommandPurpose.BUILD,
                        "inferred:configure.ac",
                        0.9,
                    ),
                    ExtractedCommand(
                        "make",
                        CommandPurpose.BUILD,
                        "inferred:configure.ac",
                        0.9,
                    ),
                    ExtractedCommand(
                        "make check",
                        CommandPurpose.TEST,
                        "inferred:configure.ac",
                        0.85,
                    ),
                )
            )
        elif "make" in systems:
            commands.extend(
                (
                    ExtractedCommand(
                        "make",
                        CommandPurpose.BUILD,
                        "inferred:Makefile",
                        0.8,
                    ),
                    ExtractedCommand(
                        "make test",
                        CommandPurpose.TEST,
                        "inferred:Makefile",
                        0.7,
                    ),
                )
            )
        if project_type is ProjectType.CLI:
            runtime = cls._runtime_command(scanned, systems, project_name)
            if runtime:
                commands.append(
                    ExtractedCommand(
                        runtime,
                        CommandPurpose.RUN,
                        "inferred:root-executable-target",
                        0.9,
                    )
                )
        return tuple(commands)

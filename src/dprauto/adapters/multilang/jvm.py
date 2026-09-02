"""Rule-first Java/Kotlin/Groovy project inspection."""

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
from dprauto.domain.workspace import RepositoryScan
from dprauto.errors import ProjectParsingError
from dprauto.inspection.commands import CommandExtractor, ExtractedCommand

JVM_ROOT_MARKERS = {
    "build.gradle",
    "build.gradle.kts",
    "gradlew",
    "mvnw",
    "pom.xml",
    "settings.gradle",
    "settings.gradle.kts",
}


class JVMProjectParser:
    """Create a language-neutral profile for Maven and Gradle repositories."""

    name = "jvm-rules-v1"
    priority = 300

    def __init__(
        self,
        command_extractor: CommandExtractor | None = None,
        language_detector: RepositoryLanguageDetector | None = None,
    ) -> None:
        self.command_extractor = command_extractor or CommandExtractor()
        self.language_detector = language_detector or RepositoryLanguageDetector()

    def supports(self, scanned: RepositoryScan) -> bool:
        return any(
            depth(path) == 1 and PurePosixPath(path).name.casefold() in JVM_ROOT_MARKERS
            for path in scanned.files
        )

    def parse_scanned(
        self,
        source: SourceReference,
        scanned: RepositoryScan,
    ) -> ProjectProfile:
        if not self.supports(scanned):
            raise ProjectParsingError(f"no root Maven or Gradle indicators found in {scanned.root}")

        build_systems = self._build_systems(scanned)
        build_files = self._build_files(scanned)
        dependencies = self._dependency_files(scanned)
        readmes = readme_files(scanned)
        workflows = ci_files(scanned)
        inferred = self._inferred_commands(scanned, build_systems)
        commands = project_commands(
            (*extracted_commands(scanned, readmes, workflows, self.command_extractor), *inferred)
        )
        project_name = self._project_name(scanned, build_systems) or scanned.root.name
        subprojects = self._subprojects(scanned, build_systems)
        (
            java_version,
            version_evidence,
            java_target_version,
            target_version_evidence,
        ) = self._java_version(scanned, build_systems)
        language_counts = self.language_detector.counts(scanned)
        languages = tuple(
            language
            for language in self.language_detector.languages(scanned)
            if language in {"Java", "Kotlin", "Groovy"}
        ) or ("Java",)
        runtime_constraints = {"java": java_version} if java_version else {}
        project_id = f"{project_name}@{source.revision}" if source.revision else project_name
        test_commands = tuple(
            command.command.display
            for command in commands
            if command.command.purpose is CommandPurpose.TEST
        )
        maven_git_hook_install_source = self._maven_git_hook_install_source(
            scanned,
            build_systems,
        )
        optional_test_profile_variables = self._optional_test_profile_variables(
            scanned,
            workflows,
        )
        gradle_projects_by_directory = self._gradle_projects_by_directory(
            scanned,
            build_systems,
        )
        test_files, safe_test_files, unstable_test_files = self._test_files(scanned)
        test_environment_variables, test_prerequisite_evidence = (
            self._ci_test_environment(scanned, workflows, build_systems)
        )
        return ProjectProfile(
            project_id=project_id,
            source=source,
            languages=languages,
            project_type=ProjectType.LIBRARY,
            runtime_constraints=runtime_constraints,
            package_managers=build_systems,
            dependency_files=dependencies,
            build_files=build_files,
            dockerfiles=dockerfiles(scanned),
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
                "java_version_evidence": version_evidence,
                "java_target_version": java_target_version,
                "java_target_version_evidence": target_version_evidence,
                "language_file_counts": language_counts,
                "test_commands": test_commands,
                "maven_git_hook_install_source": maven_git_hook_install_source,
                "optional_test_profile_variables": optional_test_profile_variables,
                "gradle_projects_by_directory": gradle_projects_by_directory,
                "test_files": test_files,
                "safe_test_files": safe_test_files,
                "unstable_test_files": unstable_test_files,
                "test_environment_variables": test_environment_variables,
                "test_prerequisite_evidence": test_prerequisite_evidence,
                "scan_file_count": len(scanned.files),
                "scan_skipped_files": scanned.skipped_files,
                "scan_truncated": scanned.truncated,
            },
        )

    @staticmethod
    def _gradle_projects_by_directory(
        scanned: RepositoryScan,
        systems: tuple[str, ...],
    ) -> dict[str, str]:
        if "gradle" not in systems:
            return {}
        projects: dict[str, str] = {}
        for path in scanned.files:
            build = PurePosixPath(path)
            if (
                len(build.parts) <= 1
                or build.parts[0] in {"buildSrc", "gradle"}
                or not build.name.endswith((".gradle", ".gradle.kts"))
            ):
                continue
            directory = build.parent.as_posix()
            if build.name in {"build.gradle", "build.gradle.kts"}:
                project_name = build.parent.name
            else:
                project_name = re.sub(r"\.gradle(?:\.kts)?$", "", build.name)
            projects.setdefault(directory, project_name)
        return projects

    @staticmethod
    def _test_files(
        scanned: RepositoryScan,
    ) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        tests: list[str] = []
        safe: list[str] = []
        unstable: list[str] = []
        unstable_pattern = re.compile(
            r"(?:^|[/_.-])(?:async|benchmark|concurrent|e2e|integration|network|"
            r"performance|race|remote|scheduler|slow|stress|timeout)(?:[/_.-]|$)",
            re.IGNORECASE,
        )
        for path in scanned.files:
            normalized = path.casefold()
            if not re.search(r"(?:^|/)src/test/(?:java|kotlin|groovy)/", normalized):
                continue
            if not normalized.endswith(("test.java", "tests.java", "test.kt", "test.groovy")):
                continue
            text = scanned.read_text(path)
            if not re.search(
                r"@(?:org\.junit\.)?(?:Test|ParameterizedTest|RepeatedTest|TestFactory)\b|"
                r"extends\s+(?:TestCase|TestBase)\b",
                text,
            ):
                continue
            tests.append(path)
            if unstable_pattern.search(path) or re.search(
                r"\b(?:Thread\.sleep|CountDownLatch|TestTimedOutException)\b",
                text,
            ):
                unstable.append(path)
            else:
                safe.append(path)
        return tuple(tests), tuple(safe), tuple(unstable)

    @staticmethod
    def _ci_test_environment(
        scanned: RepositoryScan,
        workflows: tuple[str, ...],
        systems: tuple[str, ...],
    ) -> tuple[dict[str, str], tuple[str, ...]]:
        if "gradle" not in systems or not workflows:
            return {}, ()
        for path in ("build.gradle", "build.gradle.kts"):
            if re.search(
                r"System\.getenv\(\s*['\"]CI['\"]\s*\)",
                scanned.read_text(path),
            ):
                return {"CI": "true"}, (f"{path}:System.getenv(CI)",)
        return {}, ()

    @staticmethod
    def _maven_git_hook_install_source(
        scanned: RepositoryScan,
        systems: tuple[str, ...],
    ) -> str:
        if "maven" not in systems:
            return ""
        pom = scanned.read_text("pom.xml")
        plugin = re.search(
            r"<plugin\b[^>]*>.*?<artifactId>\s*git-build-hook-maven-plugin\s*"
            r"</artifactId>.*?</plugin>",
            pom,
            re.DOTALL,
        )
        if plugin and re.search(r"<goal>\s*install\s*</goal>", plugin.group(0)):
            return "pom.xml:git-build-hook-maven-plugin:install"
        return ""

    @staticmethod
    def _optional_test_profile_variables(
        scanned: RepositoryScan,
        workflows: tuple[str, ...],
    ) -> tuple[str, ...]:
        selected: list[str] = []
        for path in workflows:
            text = scanned.read_text(path)
            for name, _profile in re.findall(
                r"(?:echo\s+)?[\"']?([A-Z][A-Z0-9_]*_PROFILE)=(-P[A-Za-z0-9_.:-]+)",
                text,
            ):
                selected.append(name)
        return tuple(dict.fromkeys(selected))

    @staticmethod
    def _build_systems(scanned: RepositoryScan) -> tuple[str, ...]:
        root_names = {
            PurePosixPath(path).name.casefold()
            for path in scanned.files
            if depth(path) == 1
        }
        systems = []
        if "pom.xml" in root_names:
            systems.append("maven")
        if root_names & {
            "build.gradle",
            "build.gradle.kts",
            "settings.gradle",
            "settings.gradle.kts",
        }:
            systems.append("gradle")
        return tuple(systems)

    @staticmethod
    def _build_files(scanned: RepositoryScan) -> tuple[str, ...]:
        names = {
            "build.gradle",
            "build.gradle.kts",
            "gradle.properties",
            "gradlew",
            "mvnw",
            "pom.xml",
            "settings.gradle",
            "settings.gradle.kts",
        }
        return tuple(
            path
            for path in scanned.files
            if depth(path) <= 4 and PurePosixPath(path).name.casefold() in names
        )

    @staticmethod
    def _dependency_files(scanned: RepositoryScan) -> tuple[str, ...]:
        names = {
            "build.gradle",
            "build.gradle.kts",
            "gradle.properties",
            "libs.versions.toml",
            "pom.xml",
            "settings.gradle",
            "settings.gradle.kts",
        }
        return tuple(
            path
            for path in scanned.files
            if depth(path) <= 4 and PurePosixPath(path).name.casefold() in names
        )

    @staticmethod
    def _project_name(scanned: RepositoryScan, systems: tuple[str, ...]) -> str:
        if "gradle" in systems:
            settings = scanned.read_text("settings.gradle") or scanned.read_text(
                "settings.gradle.kts"
            )
            match = re.search(r"rootProject\.name\s*=\s*['\"]([^'\"]+)", settings)
            if match:
                return match.group(1).strip()
        if "maven" in systems:
            pom = re.sub(
                r"<parent\b[^>]*>.*?</parent>",
                "",
                scanned.read_text("pom.xml"),
                flags=re.DOTALL,
            )
            match = re.search(r"<artifactId>\s*([^<]+?)\s*</artifactId>", pom)
            if match:
                return match.group(1).strip()
        return ""

    @staticmethod
    def _subprojects(scanned: RepositoryScan, systems: tuple[str, ...]) -> tuple[str, ...]:
        selected: list[str] = []
        if "maven" in systems:
            for value in re.findall(
                r"<module>\s*([^<]+?)\s*</module>", scanned.read_text("pom.xml")
            ):
                normalized = safe_subproject(value)
                if normalized:
                    selected.append(normalized)
        if "gradle" in systems:
            settings = scanned.read_text("settings.gradle") or scanned.read_text(
                "settings.gradle.kts"
            )
            for line in settings.splitlines():
                if not re.match(r"^\s*include(?:\s|\()", line):
                    continue
                for value in re.findall(r"['\"]([^'\"]+)['\"]", line):
                    normalized = safe_subproject(value)
                    if normalized:
                        selected.append(normalized)
            # Some large Gradle repositories (including Spring Security) use
            # a FileTree loop and dynamic include(projectPath). The build-file
            # parent is the real working directory in that convention.
            for path in scanned.files:
                build_path = PurePosixPath(path)
                if (
                    len(build_path.parts) <= 1
                    or len(build_path.parts) > 5
                    or build_path.parts[0] in {"buildSrc", "gradle"}
                    or not build_path.name.endswith((".gradle", ".gradle.kts"))
                ):
                    continue
                normalized = safe_subproject(build_path.parent.as_posix())
                if normalized:
                    selected.append(normalized)
        return tuple(dict.fromkeys(selected))

    @staticmethod
    def _java_version(
        scanned: RepositoryScan,
        systems: tuple[str, ...],
    ) -> tuple[str, str, str, str]:
        target = ""
        target_evidence = ""
        if "maven" in systems:
            pom = scanned.read_text("pom.xml")
            for tag in (
                "maven.compiler.release",
                "java.version",
                "maven.compiler.source",
            ):
                match = re.search(rf"<{re.escape(tag)}>\s*([^<]+?)\s*</{re.escape(tag)}>", pom)
                if match and re.fullmatch(r"(?:1\.)?\d+", match.group(1).strip()):
                    target = match.group(1).removeprefix("1.")
                    target_evidence = f"pom.xml:{tag}"
                    break
        elif "gradle" in systems:
            for path in ("build.gradle", "build.gradle.kts", "gradle.properties"):
                text = scanned.read_text(path)
                toolchains = [
                    int(value)
                    for value in re.findall(
                        r"JavaLanguageVersion\.of\s*\(\s*(\d{1,2})\s*\)",
                        text,
                    )
                ]
                if toolchains:
                    target = str(min(toolchains))
                    target_evidence = f"{path}:toolchain"
                    break
                match = re.search(
                    r"sourceCompatibility\s*=\s*(?:JavaVersion\.VERSION_)?"
                    r"(?:1[_\.]?)?(\d{1,2})",
                    text,
                )
                if match:
                    target = match.group(1)
                    target_evidence = f"{path}:sourceCompatibility"
                    break

        build_candidates: list[tuple[int, str]] = []
        if "maven" in systems:
            profile_versions = re.findall(
                r"<jdk>\s*\[\s*(?:1\.)?(\d{1,2})\s*,",
                scanned.read_text("pom.xml"),
            )
            for value in profile_versions:
                build_candidates.append((int(value), "pom.xml:profile-jdk-lower-bound"))
        for path in ci_files(scanned):
            text = scanned.read_text(path)
            for match in re.finditer(
                r"(?im)^\s*(?:java|jdk|java-version)\s*:\s*\[([^\]]+)\]",
                text,
            ):
                for value in re.findall(r"(?<![\d.])(?:1\.)?(\d{1,2})(?!\d)", match.group(1)):
                    build_candidates.append((int(value), f"{path}:jdk-matrix"))
            for value in re.findall(
                r"(?im)^\s*java-version\s*:\s*['\"]?(?:1\.)?(\d{1,2})(?!\d)",
                text,
            ):
                build_candidates.append((int(value), f"{path}:java-version"))
        plausible = [item for item in build_candidates if 8 <= item[0] <= 30]
        if plausible:
            minimum, evidence = min(plausible, key=lambda item: item[0])
            if target and int(target) > minimum:
                return target, target_evidence, target, target_evidence
            return str(minimum), evidence, target, target_evidence
        return target, target_evidence, target, target_evidence

    @staticmethod
    def _inferred_commands(
        scanned: RepositoryScan,
        systems: tuple[str, ...],
    ) -> tuple[ExtractedCommand, ...]:
        commands: list[ExtractedCommand] = []
        if "maven" in systems:
            executable = "./mvnw" if scanned.has("mvnw") else "mvn"
            source = "inferred:mvnw" if scanned.has("mvnw") else "inferred:pom.xml"
            commands.extend(
                (
                    ExtractedCommand(
                        f"{executable} -B -DskipTests package",
                        CommandPurpose.BUILD,
                        source,
                        0.9,
                    ),
                    ExtractedCommand(
                        f"{executable} -B test",
                        CommandPurpose.TEST,
                        source,
                        0.9,
                    ),
                )
            )
        if "gradle" in systems:
            executable = "./gradlew" if scanned.has("gradlew") else "gradle"
            source = "inferred:gradlew" if scanned.has("gradlew") else "inferred:build.gradle"
            commands.extend(
                (
                    ExtractedCommand(
                        f"{executable} assemble",
                        CommandPurpose.BUILD,
                        source,
                        0.9,
                    ),
                    ExtractedCommand(
                        f"{executable} test",
                        CommandPurpose.TEST,
                        source,
                        0.9,
                    ),
                )
            )
        return tuple(commands)

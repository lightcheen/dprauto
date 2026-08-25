import json
import tempfile
import unittest
from pathlib import Path

from dprauto.adapters.multilang import (
    MultiLanguageProjectParser,
    ProjectParserRegistry,
    RepositoryLanguageDetector,
)
from dprauto.domain.enums import CommandPurpose
from dprauto.domain.models import SourceReference
from dprauto.inspection.scanner import FileScanner
from dprauto.ports.parser import ProjectParser
from dprauto.strategies import JVMTemplateStrategy, NativeTemplateStrategy, StrategyRegistry
from dprauto.verification.commands import TestCommandSelector

M0_SOURCE_ROOT = Path(
    "/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos"
)
M0_MANIFEST = Path(__file__).resolve().parents[1] / "evaluations/multilang/manifest.json"


class MultilangProjectParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = MultiLanguageProjectParser()

    @staticmethod
    def _commands(profile, purpose: CommandPurpose) -> set[str]:
        return {
            command.command.display
            for command in profile.commands
            if command.command.purpose is purpose
        }

    def test_implements_project_parser_port(self) -> None:
        self.assertIsInstance(self.parser, ProjectParser)

    def test_language_detector_covers_heragent_language_family(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in (
                "main.py",
                "Main.java",
                "native.c",
                "native.cpp",
                "build.rs",
                "main.go",
                "app.ts",
                "query.sql",
                "script.sh",
                "workflow.yml",
            ):
                (root / name).write_text("\n", encoding="utf-8")
            scanned = FileScanner().scan(root)

            counts = RepositoryLanguageDetector().counts(scanned)

            self.assertEqual(counts["Python"], 1)
            self.assertEqual(counts["Java"], 1)
            self.assertEqual(counts["C"], 1)
            self.assertEqual(counts["C++"], 1)
            self.assertEqual(counts["Rust"], 1)
            self.assertEqual(counts["Go"], 1)
            self.assertEqual(counts["TypeScript"], 1)
            self.assertEqual(counts["SQL"], 1)
            self.assertEqual(counts["Bash"], 1)
            self.assertEqual(counts["YAML"], 1)

    def test_maven_multimodule_profile_has_build_test_and_java_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src/main/java/example").mkdir(parents=True)
            (root / "src/main/java/example/App.java").write_text(
                "package example; class App {}\n", encoding="utf-8"
            )
            (root / "mvnw").write_text("#!/bin/sh\n", encoding="utf-8")
            (root / "pom.xml").write_text(
                """<project>
  <artifactId>demo-parent</artifactId>
  <properties><maven.compiler.release>17</maven.compiler.release></properties>
  <modules><module>core</module><module>services/api</module></modules>
</project>
""",
                encoding="utf-8",
            )

            profile = self.parser.parse(SourceReference("fixture://maven", "abc"), root)

            self.assertEqual(profile.languages, ("Java",))
            self.assertEqual(profile.package_managers, ("maven",))
            self.assertEqual(profile.runtime_constraints["java"], "17")
            self.assertEqual(profile.metadata["subprojects"], ("core", "services/api"))
            self.assertEqual(profile.metadata["parser_registry_selection"], "jvm-rules-v1")
            self.assertIn(
                "./mvnw -B -DskipTests package",
                self._commands(profile, CommandPurpose.BUILD),
            )
            self.assertIn("./mvnw -B test", self._commands(profile, CommandPurpose.TEST))

    def test_gradle_root_wins_over_incidental_python_tooling(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src/main/java/example").mkdir(parents=True)
            (root / "src/main/java/example/App.java").write_text(
                "package example; class App {}\n", encoding="utf-8"
            )
            (root / "tools.py").write_text("print('tool')\n", encoding="utf-8")
            (root / "pyproject.toml").write_text("[project]\nname='tools'\n", encoding="utf-8")
            (root / "gradlew").write_text("#!/bin/sh\n", encoding="utf-8")
            (root / "settings.gradle").write_text(
                "rootProject.name = 'demo'\ninclude ':core', ':services:api'\n",
                encoding="utf-8",
            )
            (root / "build.gradle").write_text(
                "java { toolchain { languageVersion = JavaLanguageVersion.of(21) } }\n",
                encoding="utf-8",
            )

            profile = self.parser.parse(SourceReference("fixture://gradle"), root)

            self.assertEqual(profile.metadata["parser_registry_selection"], "jvm-rules-v1")
            self.assertEqual(profile.runtime_constraints["java"], "21")
            self.assertEqual(profile.metadata["working_directories"], (".", "core", "services/api"))
            self.assertIn("./gradlew test", self._commands(profile, CommandPurpose.TEST))

    def test_cmake_profile_discovers_languages_standard_subprojects_and_ctest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src").mkdir()
            (root / "tests").mkdir()
            (root / "src/main.c").write_text("int helper(void) { return 0; }\n", encoding="utf-8")
            (root / "src/lib.cpp").write_text("int value() { return 1; }\n", encoding="utf-8")
            (root / "tests/test.cpp").write_text("int main() { return 0; }\n", encoding="utf-8")
            (root / "CMakeLists.txt").write_text(
                """cmake_minimum_required(VERSION 3.20)
project(native_demo)
set(CMAKE_C_STANDARD 11)
set(CMAKE_CXX_STANDARD 20)
enable_testing()
add_subdirectory(src)
add_subdirectory(tests)
""",
                encoding="utf-8",
            )

            profile = self.parser.parse(SourceReference("fixture://cmake"), root)

            self.assertEqual(set(profile.languages), {"C", "C++"})
            self.assertEqual(profile.package_managers[0], "cmake")
            self.assertEqual(
                profile.runtime_constraints,
                {"c_standard": "11", "cpp_standard": "20"},
            )
            self.assertEqual(profile.metadata["subprojects"], ("src", "tests"))
            self.assertEqual(
                profile.metadata["build_pipeline"],
                ("cmake -S . -B build", "cmake --build build"),
            )
            self.assertIn(
                "ctest --test-dir build --output-on-failure",
                self._commands(profile, CommandPurpose.TEST),
            )

    def test_autotools_profile_preserves_ordered_build_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "main.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
            (root / "autogen.sh").write_text("#!/bin/sh\nautoreconf -i\n", encoding="utf-8")
            (root / "configure.ac").write_text(
                "AC_INIT([native-demo], [1.0])\n", encoding="utf-8"
            )

            profile = self.parser.parse(SourceReference("fixture://autotools"), root)

            self.assertEqual(profile.package_managers, ("autotools",))
            self.assertEqual(
                profile.metadata["build_pipeline"],
                ("./autogen.sh", "./configure", "make"),
            )
            self.assertIn("make check", self._commands(profile, CommandPurpose.TEST))

    def test_registry_rejects_duplicate_parser_names(self) -> None:
        parser = self.parser.registry.registrations[0].parser
        with self.assertRaisesRegex(ValueError, "names must be unique"):
            ProjectParserRegistry((parser, parser))

    def test_m0_real_java_and_native_snapshots_parse_without_execution(self) -> None:
        cases = (
            (
                "executionagent-apache-commons-csv-2d44689ec75e",
                "jvm-rules-v1",
                "maven",
                "Java",
            ),
            (
                "executionagent-spring-projects-spring-security-0d5f42f8529c",
                "jvm-rules-v1",
                "gradle",
                "Java",
            ),
            (
                "executionagent-ccache-ccache-7f3e822efb1b",
                "native-rules-v1",
                "cmake",
                "C++",
            ),
            (
                "executionagent-distcc-distcc-a627b26f08cd",
                "native-rules-v1",
                "autotools",
                "C",
            ),
        )
        for directory, parser_name, build_system, language in cases:
            with self.subTest(directory=directory):
                source_path = M0_SOURCE_ROOT / directory
                self.assertTrue(source_path.is_dir(), source_path)

                profile = self.parser.parse(SourceReference(str(source_path)), source_path)

                self.assertEqual(profile.metadata["parser_registry_selection"], parser_name)
                self.assertIn(build_system, profile.package_managers)
                self.assertIn(language, profile.languages)
                self.assertTrue(self._commands(profile, CommandPurpose.BUILD))
                self.assertTrue(self._commands(profile, CommandPurpose.TEST))
                self.assertLessEqual(len(self._commands(profile, CommandPurpose.BUILD)), 12)
                self.assertLessEqual(len(self._commands(profile, CommandPurpose.TEST)), 12)
                self.assertNotIn("**/*.gradle", profile.metadata["subprojects"])

    def test_all_m0_ready_sources_route_to_expected_parser_and_test_command(self) -> None:
        manifest = json.loads(M0_MANIFEST.read_text(encoding="utf-8"))
        expected_parser = {
            "python": "python-rules-v1",
            "java": "jvm-rules-v1",
            "c": "native-rules-v1",
            "cpp": "native-rules-v1",
        }
        expected_language = {
            "python": "Python",
            "java": "Java",
            "c": "C",
            "cpp": "C++",
        }
        ready_cases = [
            case for case in manifest["cases"] if case["source"]["state"] == "ready"
        ]

        self.assertEqual(len(ready_cases), 17)
        for case in ready_cases:
            with self.subTest(case_id=case["case_id"]):
                source_path = Path(case["source"]["path"])
                profile = self.parser.parse(
                    SourceReference(str(source_path), case["source"]["revision"]),
                    source_path,
                )

                self.assertEqual(
                    profile.metadata["parser_registry_selection"],
                    expected_parser[case["primary_language"]],
                )
                self.assertIn(
                    expected_language[case["primary_language"]],
                    profile.languages,
                )
                self.assertTrue(self._commands(profile, CommandPurpose.TEST))

                if case["case_id"] == "py-yubikey-manager":
                    download_commands = [
                        command
                        for command in profile.commands
                        if "pip download" in command.command.display
                    ]
                    self.assertTrue(download_commands)
                    self.assertTrue(
                        all(
                            command.command.purpose is CommandPurpose.INSTALL
                            for command in download_commands
                        )
                    )

    def test_all_m0_ready_jvm_and_native_sources_get_deterministic_build_plans(self) -> None:
        manifest = json.loads(M0_MANIFEST.read_text(encoding="utf-8"))
        cases = [
            case
            for case in manifest["cases"]
            if case["source"]["state"] == "ready"
            and case["primary_language"] in {"java", "c", "cpp"}
        ]
        registry = StrategyRegistry(
            (JVMTemplateStrategy(None), NativeTemplateStrategy(None))  # type: ignore[arg-type]
        )

        self.assertEqual(len(cases), 9)
        for case in cases:
            with self.subTest(case_id=case["case_id"]):
                source_path = Path(case["source"]["path"])
                profile = self.parser.parse(
                    SourceReference(str(source_path), case["source"]["revision"]),
                    source_path,
                )
                plan = registry.select(profile).create_plan(profile)
                selected_test = TestCommandSelector().select(profile)

                self.assertIn(plan.strategy, {"jvm-template", "native-template"})
                self.assertTrue(
                    str(plan.metadata["build_command_source"]).startswith("deterministic:")
                )
                self.assertTrue(plan.metadata["dependency_installation_commands"])
                self.assertIn("DPRAUTO_", plan.metadata["runtime_probe_command"])
                self.assertIsNotNone(selected_test)
                self.assertNotIn("pip download", selected_test.command.display.casefold())
                self.assertNotRegex(selected_test.command.display, r"%[A-Za-z_][A-Za-z0-9_]*%")
                if case["case_id"] == "cpp-ccache":
                    self.assertEqual(
                        selected_test.command.display,
                        "ctest --test-dir build --output-on-failure",
                    )
                    self.assertIn(
                        "-DDEPS=DOWNLOAD",
                        plan.metadata["build_commands"][0],
                    )
                elif case["case_id"] == "cpp-nlohmann-json":
                    self.assertIn(
                        "-DJSON_BuildTests=ON",
                        plan.metadata["build_commands"][0],
                    )
                elif case["case_id"] == "c-distcc":
                    self.assertIn("python3", plan.metadata["system_packages"])
                elif case["case_id"] == "java-mybatis":
                    self.assertEqual(profile.runtime_constraints["java"], "17")
                    self.assertEqual(profile.metadata["java_target_version"], "11")
                    self.assertEqual(plan.metadata["java_version"], "17")


if __name__ == "__main__":
    unittest.main()

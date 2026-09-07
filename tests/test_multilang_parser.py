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
M14_MANIFEST = (
    Path(__file__).resolve().parents[1]
    / "evaluations/multilang/manifest-high-star-30-20260903.json"
)


class MultilangProjectParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = MultiLanguageProjectParser()

    def test_build_manifest_vcs_consumer_is_preserved_as_context_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "settings.gradle").write_text(
                "rootProject.name = 'fixture'\n",
                encoding="utf-8",
            )
            (root / "build.gradle").write_text(
                "import org.ajoberstar.grgit.Grgit\nplugins { id 'java' }\n",
                encoding="utf-8",
            )
            (root / "src/main/java/example").mkdir(parents=True)
            (root / "src/main/java/example/App.java").write_text(
                "package example; class App {}\n",
                encoding="utf-8",
            )

            profile = self.parser.parse(SourceReference("fixture://vcs"), root)

        self.assertTrue(profile.metadata["vcs_metadata_required"])
        self.assertEqual(profile.metadata["vcs_metadata_evidence"], ("build.gradle",))

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
            (root / ".github/workflows").mkdir(parents=True)
            (root / ".github/workflows/ci.yml").write_text(
                "run: echo 'OPTIONAL_DB_PROFILE=-Pdatabase' >> $GITHUB_ENV\n",
                encoding="utf-8",
            )
            (root / "pom.xml").write_text(
                """<project>
  <artifactId>demo-parent</artifactId>
  <properties><maven.compiler.release>17</maven.compiler.release></properties>
  <modules><module>core</module><module>services/api</module></modules>
  <build><plugins><plugin>
    <artifactId>git-build-hook-maven-plugin</artifactId>
    <executions><execution><goals><goal>install</goal></goals></execution></executions>
  </plugin></plugins></build>
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
            self.assertEqual(
                profile.metadata["maven_git_hook_install_source"],
                "pom.xml:git-build-hook-maven-plugin:install",
            )
            self.assertEqual(
                profile.metadata["optional_test_profile_variables"],
                ("OPTIONAL_DB_PROFILE",),
            )
            self.assertIn(
                "./mvnw -B -Dmaven.test.skip=true package",
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
            self.assertEqual(
                profile.metadata["working_directories"],
                (".", "core", "services/api"),
            )
            self.assertIn("./gradlew test", self._commands(profile, CommandPurpose.TEST))

    def test_gradle_wrapper_version_is_recorded_in_dependency_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src/main/java/example").mkdir(parents=True)
            (root / "src/main/java/example/App.java").write_text(
                "package example; class App {}\n",
                encoding="utf-8",
            )
            (root / "gradle/wrapper").mkdir(parents=True)
            (root / "gradle/wrapper/gradle-wrapper.properties").write_text(
                "distributionUrl=https\\://services.gradle.org/distributions/"
                "gradle-8.12.1-bin.zip\n",
                encoding="utf-8",
            )
            (root / "gradlew").write_text("#!/bin/sh\n", encoding="utf-8")
            (root / "settings.gradle").write_text(
                "rootProject.name = 'wrapper-demo'\n",
                encoding="utf-8",
            )
            (root / "build.gradle").write_text(
                "java { toolchain { languageVersion = JavaLanguageVersion.of(17) } }\n",
                encoding="utf-8",
            )

            profile = self.parser.parse(SourceReference("fixture://gradle-wrapper"), root)

            self.assertEqual(profile.metadata["build_tool_constraints"], {"gradle": "8.12.1"})
            self.assertEqual(
                profile.metadata["dependency_contract"]["build_tool_constraints"],
                (
                    {
                        "name": "gradle",
                        "specifier": "8.12.1",
                        "source": "gradle/wrapper/gradle-wrapper.properties:distributionUrl",
                        "confidence": 0.99,
                    },
                ),
            )

    def test_nested_native_build_root_beats_incidental_python_scripts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pyproject.toml").write_text(
                "[tool.black]\nline-length = 100\n",
                encoding="utf-8",
            )
            (root / "README.md").write_text(
                "## Build\n\n```sh\nmake profile-build\n```\n",
                encoding="utf-8",
            )
            (root / "scripts").mkdir()
            (root / "scripts/check.py").write_text("print('check')\n", encoding="utf-8")
            (root / "src").mkdir()
            (root / "src/Makefile").write_text(
                "all:\n\t$(CXX) -o engine main.cpp\n\ntest:\n\t./engine --test\n",
                encoding="utf-8",
            )
            (root / "src/main.cpp").write_text(
                "int main() { return 0; }\n",
                encoding="utf-8",
            )

            profile = self.parser.parse(SourceReference("fixture://nested-native"), root)

            self.assertEqual(profile.metadata["parser_registry_selection"], "native-rules-v1")
            self.assertEqual(profile.metadata["build_root"], "src")
            self.assertEqual(profile.build_files, ("src/Makefile",))
            self.assertEqual(profile.languages, ("C++",))
            build = next(
                command
                for command in profile.commands
                if command.command.purpose is CommandPurpose.BUILD
            )
            self.assertEqual(build.command.cwd, "src")
            self.assertEqual(build.command.display, "make")
            self.assertIn("README.md", profile.readme_files)
            self.assertTrue(
                any(
                    command.source == "README.md"
                    and command.command.cwd is None
                    and command.command.display == "make profile-build"
                    for command in profile.commands
                )
            )
            self.assertIn(
                {"path": "README.md", "kind": "documentation", "confidence": 0.9},
                profile.metadata["repository_evidence"]["documents"],
            )
            candidates = profile.metadata["parser_candidate_scores"]
            self.assertGreater(
                next(item["score"] for item in candidates if item["parser"] == "native-rules-v1"),
                next(item["score"] for item in candidates if item["parser"] == "python-rules-v1"),
            )

    def test_vendored_ci_commands_do_not_pollute_native_project_commands(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "CMakeLists.txt").write_text(
                "cmake_minimum_required(VERSION 3.20)\n"
                "project(server)\nadd_executable(server main.cpp)\n",
                encoding="utf-8",
            )
            (root / "main.cpp").write_text("int main() { return 0; }\n", encoding="utf-8")
            (root / ".github/workflows").mkdir(parents=True)
            (root / ".github/workflows/ci.yml").write_text(
                "steps:\n  - run: cmake --build build\n",
                encoding="utf-8",
            )
            (root / "trunk/3rdparty/library").mkdir(parents=True)
            (root / "trunk/3rdparty/library/.travis.yml").write_text(
                "script: make test\n",
                encoding="utf-8",
            )
            (root / "vendor/dependency").mkdir(parents=True)
            (root / "vendor/dependency/README.md").write_text(
                "```sh\nmake check\n```\n",
                encoding="utf-8",
            )

            profile = self.parser.parse(SourceReference("fixture://vendor-ci"), root)

            self.assertIn(".github/workflows/ci.yml", profile.ci_files)
            self.assertNotIn("trunk/3rdparty/library/.travis.yml", profile.ci_files)
            self.assertNotIn("vendor/dependency/README.md", profile.readme_files)
            sources = {command.source for command in profile.commands}
            self.assertNotIn("trunk/3rdparty/library/.travis.yml", sources)
            self.assertNotIn("vendor/dependency/README.md", sources)

    def test_root_python_package_beats_nested_native_example(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pyproject.toml").write_text(
                "[build-system]\nrequires=['setuptools']\n"
                "[project]\nname='primary-python-package'\nversion='1.0'\n",
                encoding="utf-8",
            )
            (root / "primary_package").mkdir()
            (root / "primary_package/__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
            (root / "examples/native").mkdir(parents=True)
            (root / "examples/native/CMakeLists.txt").write_text(
                "project(example)\nadd_executable(example main.cpp)\n",
                encoding="utf-8",
            )
            (root / "examples/native/main.cpp").write_text(
                "int main() { return 0; }\n",
                encoding="utf-8",
            )

            profile = self.parser.parse(SourceReference("fixture://python-primary"), root)

            self.assertEqual(profile.metadata["parser_registry_selection"], "python-rules-v1")
            self.assertEqual(profile.metadata["build_root"], ".")

    def test_repository_evidence_records_selection_constraints_and_commands(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pom.xml").write_text(
                "<project><artifactId>evidence-demo</artifactId>"
                "<properties><maven.compiler.release>17</maven.compiler.release></properties>"
                "</project>",
                encoding="utf-8",
            )
            (root / "src/main/java/example").mkdir(parents=True)
            (root / "src/main/java/example/App.java").write_text(
                "package example; class App {}\n",
                encoding="utf-8",
            )

            profile = self.parser.parse(SourceReference("fixture://evidence"), root)
            evidence = profile.metadata["repository_evidence"]

            self.assertEqual(evidence["schema_version"], 1)
            self.assertEqual(evidence["parser_selection"]["parser"], "jvm-rules-v1")
            self.assertEqual(evidence["parser_selection"]["build_root"], ".")
            self.assertGreater(evidence["parser_selection"]["confidence"], 0.5)
            self.assertIn("pom.xml", evidence["parser_selection"]["evidence"])
            self.assertTrue(
                any(
                    item["name"] == "java"
                    and item["value"] == "17"
                    and item["source"] == "pom.xml:maven.compiler.release"
                    for item in evidence["runtime_constraints"]
                )
            )
            self.assertTrue(
                all(
                    {"value", "purpose", "source", "confidence", "working_directory"}
                    <= item.keys()
                    for item in evidence["commands"]
                )
            )

    def test_gradle_default_toolchain_beats_ci_launcher_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".github/workflows").mkdir(parents=True)
            (root / ".github/workflows/ci.yml").write_text(
                "java-version: '11'\nrun: ./gradlew test\n",
                encoding="utf-8",
            )
            (root / "gradlew").write_text("#!/bin/sh\n", encoding="utf-8")
            (root / "src/test/java/example").mkdir(parents=True)
            (root / "src/test/java/example/StableTest.java").write_text(
                "class StableTest { @Test void works() {} }\n",
                encoding="utf-8",
            )
            (root / "src/test/java/example/AsyncTest.java").write_text(
                "class AsyncTest { @Test void waits() { Thread.sleep(1); } }\n",
                encoding="utf-8",
            )
            (root / "settings.gradle").write_text(
                "rootProject.name = 'toolchain-demo'\n",
                encoding="utf-8",
            )
            (root / "build.gradle").write_text(
                """java {
  toolchain {
    if (System.getenv('BUILD_WITH_11') == 'true') {
      languageVersion = JavaLanguageVersion.of(11)
    } else {
      languageVersion = JavaLanguageVersion.of(8)
    }
  }
}
tasks.withType(Test) {
  if (System.getenv('CI') == null) { maxParallelForks = 4 }
}
""",
                encoding="utf-8",
            )

            profile = self.parser.parse(SourceReference("fixture://toolchain"), root)

        self.assertEqual(profile.runtime_constraints["java"], "11")
        self.assertEqual(profile.metadata["java_target_version"], "8")
        self.assertEqual(
            profile.metadata["java_version_evidence"],
            ".github/workflows/ci.yml:java-version",
        )
        self.assertEqual(
            profile.metadata["java_target_version_evidence"],
            "build.gradle:toolchain",
        )
        self.assertEqual(profile.metadata["test_environment_variables"], {"CI": "true"})
        self.assertEqual(
            profile.metadata["test_prerequisite_evidence"],
            ("build.gradle:System.getenv(CI)",),
        )
        self.assertEqual(
            profile.metadata["safe_test_files"],
            ("src/test/java/example/StableTest.java",),
        )
        self.assertEqual(
            profile.metadata["unstable_test_files"],
            ("src/test/java/example/AsyncTest.java",),
        )

    def test_cmake_profile_discovers_languages_standard_subprojects_and_ctest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src").mkdir()
            (root / "tests").mkdir()
            (root / "src/main.c").write_text("int helper(void) { return 0; }\n", encoding="utf-8")
            (root / "src/lib.cpp").write_text("int value() { return 1; }\n", encoding="utf-8")
            (root / "tests/test.cpp").write_text("int main() { return 0; }\n", encoding="utf-8")
            (root / "tests/legacy.py").write_text(
                "#!/usr/bin/python2\nprint 'legacy'\n",
                encoding="utf-8",
            )
            (root / "tests/optional.py").write_text(
                "#!/usr/bin/python3\nprint('optional')\n",
                encoding="utf-8",
            )
            (root / "tests/docker").mkdir()
            (root / "tests/docker/Dockerfile").write_text(
                "FROM scratch\n",
                encoding="utf-8",
            )
            (root / "CMakeLists.txt").write_text(
                """cmake_minimum_required(VERSION 3.20)
project(native_demo)
set(CMAKE_C_STANDARD 11)
set(CMAKE_CXX_STANDARD 20)
option(ENABLE_TESTS "Build tests" ON)
option(NATIVE_DEVELOPER_MODE "Enable developer targets" OFF)
enable_testing()
add_custom_target(all_tests COMMAND tests/legacy.py)
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
                {"c_standard": "11", "cpp_standard": "20", "cmake": ">=3.20"},
            )
            self.assertEqual(
                profile.metadata["cmake_version_evidence"],
                "CMakeLists.txt:cmake_minimum_required",
            )
            self.assertEqual(profile.metadata["subprojects"], ("src", "tests"))
            self.assertEqual(
                profile.metadata["cmake_configuration_arguments"],
                ("-DENABLE_TESTS=OFF", "-DNATIVE_DEVELOPER_MODE=OFF"),
            )
            self.assertEqual(
                profile.metadata["cmake_test_configuration_arguments"],
                ("-DENABLE_TESTS=ON", "-DNATIVE_DEVELOPER_MODE=ON"),
            )
            self.assertEqual(profile.metadata["cmake_test_build_target"], "all_tests")
            self.assertEqual(profile.metadata["test_required_executables"], ("python2",))
            self.assertEqual(profile.dockerfiles, ())
            self.assertEqual(
                profile.metadata["build_pipeline"],
                ("cmake -S . -B build", "cmake --build build"),
            )
            self.assertIn(
                "ctest --test-dir build --output-on-failure",
                self._commands(profile, CommandPurpose.TEST),
            )

    def test_native_dependencies_require_build_file_evidence_and_emit_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "main.cpp").write_text("int value() { return 1; }\n", encoding="utf-8")
            (root / "cmake").mkdir()
            (root / "cmake/dependencies.cmake").write_text(
                """find_package(gflags REQUIRED)
find_package(Protobuf CONFIG)
pkg_check_modules(LEPTONICA lept>=1.74)
check_include_files(\"xcb/xcb.h\" HAVE_XCB)
find_program(NASM_EXECUTABLE nasm)
""",
                encoding="utf-8",
            )
            (root / "CMakeLists.txt").write_text(
                """cmake_minimum_required(VERSION 3.18)
project(dependency_demo)
include(cmake/dependencies.cmake)
# find_package(X11 REQUIRED) is documentation, not executable CMake.
add_library(dependency_demo main.cpp)
""",
                encoding="utf-8",
            )

            profile = self.parser.parse(SourceReference("fixture://native-dependencies"), root)

            self.assertEqual(
                set(profile.metadata["system_dependency_packages"]),
                {
                    "libgflags-dev",
                    "libprotobuf-dev",
                    "protobuf-compiler",
                    "libleptonica-dev",
                    "libxcb1-dev",
                    "nasm",
                },
            )
            self.assertNotIn("libx11-dev", profile.metadata["system_dependency_packages"])
            evidence = profile.metadata["system_dependency_evidence"]
            self.assertTrue(all(item["source"] for item in evidence))
            self.assertTrue(all(item["detector"] for item in evidence))
            self.assertTrue(all(item["confidence"] >= 0.9 for item in evidence))
            contract = profile.metadata["dependency_contract"]
            self.assertEqual(contract["schema_version"], 1)
            self.assertEqual(contract["build_root"], ".")
            self.assertEqual(contract["system_dependencies"], evidence)
            self.assertIn(
                {
                    "name": "cmake",
                    "specifier": ">=3.18",
                    "source": "CMakeLists.txt:cmake_minimum_required",
                    "confidence": 0.95,
                },
                contract["runtime_constraints"],
            )

    def test_dependency_contract_normalizes_python_and_jvm_facts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pyproject.toml").write_text(
                """[project]
name = "contract-demo"
requires-python = ">=3.11"
dependencies = ["requests>=2", "click"]
[project.optional-dependencies]
test = ["pytest"]
""",
                encoding="utf-8",
            )
            (root / "contract_demo.py").write_text("import requests\n", encoding="utf-8")

            python_profile = self.parser.parse(SourceReference("fixture://python-contract"), root)
            python_contract = python_profile.metadata["dependency_contract"]
            self.assertEqual(python_contract["parser"], "python-rules-v1")
            self.assertEqual(
                {item["name"] for item in python_contract["language_dependencies"]},
                {"click", "requests"},
            )
            self.assertIn(
                {
                    "name": "test",
                    "kind": "extra",
                    "sources": ("pyproject.toml",),
                    "confidence": 0.9,
                },
                python_contract["test_dependencies"],
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pom.xml").write_text(
                """<project><artifactId>contract-jvm</artifactId><properties>
<maven.compiler.release>21</maven.compiler.release>
</properties></project>""",
                encoding="utf-8",
            )
            jvm_profile = self.parser.parse(SourceReference("fixture://jvm-contract"), root)
            jvm_contract = jvm_profile.metadata["dependency_contract"]
            self.assertEqual(jvm_contract["parser"], "jvm-rules-v1")
            self.assertEqual(jvm_contract["package_managers"], ("maven",))
            self.assertEqual(jvm_contract["manifests"][0]["path"], "pom.xml")
            self.assertEqual(jvm_contract["runtime_constraints"][0]["specifier"], "21")

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

    def test_native_autotools_bin_program_is_a_cli(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "configure.ac").write_text(
                "AC_INIT([demo],[1.0])\nPKG_CHECK_MODULES(POPT, [popt >= 1.7])\n",
                encoding="utf-8",
            )
            (root / "Makefile.in").write_text(
                "bin_PROGRAMS = demo@EXEEXT@ helper@EXEEXT@\n",
                encoding="utf-8",
            )
            (root / "src").mkdir()
            (root / "src" / "demo").mkdir()
            (root / "src" / "demo" / "core").mkdir()
            (root / "src" / "demo" / "core" / "options.c").write_text(
                'int main(void) { return 0; } /* --version */\n',
                encoding="utf-8",
            )

            profile = self.parser.parse(SourceReference("fixture://autotools-cli"), root)

            self.assertEqual(profile.project_type, profile.project_type.CLI)
            self.assertIn("./demo --version", self._commands(profile, CommandPurpose.RUN))
            self.assertIn("libpopt-dev", profile.metadata["system_dependency_packages"])

    def test_native_root_cli_gets_runtime_probe_but_library_examples_do_not(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "README.md").write_text(
                "## Quickstart\n\n```sh\n./demo_example\n```\n",
                encoding="utf-8",
            )
            (root / "main.cpp").write_text(
                'int main() { return 0; } // accepts --version\n',
                encoding="utf-8",
            )
            (root / "CMakeLists.txt").write_text(
                "project(demo)\nadd_executable(demo main.cpp)\n",
                encoding="utf-8",
            )

            cli = self.parser.parse(SourceReference("fixture://cli"), root)
            self.assertEqual(cli.project_type, cli.project_type.CLI)
            self.assertIn(
                "build/demo --version",
                self._commands(cli, CommandPurpose.RUN),
            )

            (root / "CMakeLists.txt").write_text(
                "project(demo)\nadd_library(demo main.cpp)\n",
                encoding="utf-8",
            )
            library = self.parser.parse(SourceReference("fixture://library"), root)
            self.assertEqual(library.project_type, library.project_type.LIBRARY)
            self.assertFalse(self._commands(library, CommandPurpose.RUN))

    def test_nested_make_executable_uses_bounded_repository_runtime_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src").mkdir()
            (root / ".github/workflows").mkdir(parents=True)
            (root / "README.md").write_text("# Native CLI\n", encoding="utf-8")
            (root / "src/main.cpp").write_text(
                "int main(int argc, char **argv) { return argc > 1 ? 0 : 1; }\n",
                encoding="utf-8",
            )
            (root / "src/Makefile").write_text(
                "ifeq ($(OS),Windows_NT)\n"
                "EXE = demo.exe\n"
                "else\n"
                "EXE = demo\n"
                "endif\n"
                "all: $(EXE)\n$(EXE): main.cpp\n\t$(CXX) main.cpp -o $(EXE)\n",
                encoding="utf-8",
            )
            (root / ".github/workflows/ci.yml").write_text(
                "jobs:\n  test:\n    steps:\n      - run: ./demo bench 1 2\n",
                encoding="utf-8",
            )

            parsed = self.parser.parse(SourceReference("fixture://make-cli"), root)

            self.assertEqual(parsed.metadata["build_root"], "src")
            self.assertEqual(parsed.metadata["project_name"], "demo")
            self.assertEqual(parsed.metadata["make_build_target"], "all")
            self.assertEqual(parsed.project_type, parsed.project_type.CLI)
            runtime = next(
                command
                for command in parsed.commands
                if command.command.purpose is CommandPurpose.RUN
            )
            self.assertEqual(runtime.command.display, "./demo bench 1 2")
            self.assertEqual(runtime.command.cwd, "src")
            self.assertEqual(runtime.source, "runtime-evidence:.github/workflows/ci.yml")

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

        self.assertEqual(len(ready_cases), 21)
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

        self.assertEqual(len(cases), 13)
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
                        "-DJSON_BuildTests=OFF",
                        plan.metadata["build_commands"][0],
                    )
                elif case["case_id"] == "c-distcc":
                    self.assertIn("python3", plan.metadata["system_packages"])
                elif case["case_id"] == "java-mybatis":
                    self.assertEqual(profile.runtime_constraints["java"], "17")
                    self.assertEqual(profile.metadata["java_target_version"], "11")
                    self.assertEqual(plan.metadata["java_version"], "17")

    def test_all_high_star_30_sources_have_dependency_contracts(self) -> None:
        manifest = json.loads(M14_MANIFEST.read_text(encoding="utf-8"))
        expected_parser = {
            "python": "python-rules-v1",
            "java": "jvm-rules-v1",
            "c": "native-rules-v1",
            "cpp": "native-rules-v1",
        }
        observed_failure_dependencies = {
            "cpp-tesseract": "libleptonica-dev",
            "c-ffmpeg": "nasm",
            "cpp-aseprite": "libxcb1-dev",
            "c-raylib": "libx11-dev",
            "cpp-rocksdb": "libgflags-dev",
        }
        ready_cases = [
            case for case in manifest["cases"] if case["source"]["state"] == "ready"
        ]

        self.assertEqual(len(ready_cases), 30)
        for case in ready_cases:
            with self.subTest(case_id=case["case_id"]):
                source_path = Path(case["source"]["path"])
                profile = self.parser.parse(
                    SourceReference(str(source_path), case["source"]["revision"]),
                    source_path,
                )
                contract = profile.metadata["dependency_contract"]

                self.assertEqual(
                    profile.metadata["parser_registry_selection"],
                    expected_parser[case["primary_language"]],
                )
                self.assertEqual(contract["schema_version"], 1)
                self.assertTrue(contract["manifests"])
                expected_package = observed_failure_dependencies.get(case["case_id"])
                if expected_package:
                    evidence_packages = {
                        item["package"] for item in contract["system_dependencies"]
                    }
                    self.assertIn(expected_package, evidence_packages)
                    strategy = NativeTemplateStrategy(None)  # type: ignore[arg-type]
                    plan = strategy.create_plan(profile)
                    self.assertIn(expected_package, plan.metadata["system_packages"])


if __name__ == "__main__":
    unittest.main()

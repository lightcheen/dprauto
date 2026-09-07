import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from dprauto.adapters.storage import LocalArtifactStorage
from dprauto.application.verification import LayeredVerificationService
from dprauto.config import VerificationConfig
from dprauto.domain.enums import (
    BuildStage,
    BuildStatus,
    CommandPurpose,
    ProjectType,
    VerificationLevel,
    VerificationStatus,
)
from dprauto.domain.models import (
    ArtifactRef,
    BuildPlan,
    BuildResult,
    BuildStep,
    CommandResult,
    CommandSpec,
    GeneratedFile,
    ProjectCommand,
    ProjectProfile,
    SourceReference,
    VerificationResult,
)
from dprauto.ports.runtime import ContainerExecution, ImageInspection, WebProbe
from dprauto.ports.verification import VerificationContext
from dprauto.verification import (
    InstallabilityVerifier,
    RunnabilityVerifier,
    TestabilityVerifier,
    TestCommandSelector,
)

NOW = datetime.now(timezone.utc)
BUILD_COMMAND = CommandSpec(("docker", "build", "."), purpose=CommandPurpose.BUILD)


class FakeRuntime:
    def __init__(self) -> None:
        self.output = "command-output"
        self.exit_code = 0
        self.filesystem_changes = ()
        self.library_api_count = 1
        self.web = WebProbe(True, True, True, 8000, 43210, 200)
        self.commands = []
        self.output_by_command = {}
        self.exit_code_by_command = {}
        self.timed_out_by_command = {}
        self.environments = []
        self.default_command = ("python", "app.py")

    def inspect_image(self, image_reference: str) -> ImageInspection:
        return ImageInspection(
            bool(image_reference),
            "sha256:image" if image_reference else "",
            "/workspace",
            self.default_command,
            (8000,),
        )

    def run_image(self, image_reference, command, *, timeout_seconds):
        actual = command or CommandSpec(("image-default",), purpose=CommandPurpose.RUN)
        self.commands.append(actual)
        output = self.output_by_command.get(actual.display, self.output)
        if "DPRAUTO_IMPORT_OK" in actual.display:
            output = f"DPRAUTO_IMPORT_OK\nDPRAUTO_API_COUNT={self.library_api_count}\n"
        if (
            "DPRAUTO_INSTALL_HEALTH_OK" in actual.display
            and actual.display not in self.output_by_command
        ):
            if "command -v ldd" in actual.display:
                output = (
                    "DPRAUTO_NATIVE_DYNAMIC_COUNT=1\n"
                    "DPRAUTO_INSTALL_HEALTH_MODE=dynamic-link-check\n"
                    "DPRAUTO_INSTALL_HEALTH_OK\n"
                )
            elif "java -version" in actual.display:
                output = (
                    "DPRAUTO_INSTALL_HEALTH_MODE=runtime-load\n"
                    "DPRAUTO_INSTALL_HEALTH_OK\n"
                )
            else:
                output = (
                    "DPRAUTO_INSTALL_HEALTH_MODE=pip-check\n"
                    "DPRAUTO_INSTALL_HEALTH_OK\n"
                )
        result = CommandResult(
            actual,
            self.exit_code_by_command.get(actual.display, self.exit_code),
            stdout=ArtifactRef("fake/run.log"),
            timed_out=self.timed_out_by_command.get(actual.display, False),
        )
        return ContainerExecution(result, output, self.filesystem_changes)

    def run_environment(self, image_reference, command, environment, *, timeout_seconds):
        self.environments.append(environment)
        return self.run_image(
            image_reference,
            command,
            timeout_seconds=timeout_seconds,
        )

    def probe_web(self, image_reference, command, *, container_port, timeout_seconds, path="/"):
        return self.web


def profile(
    project_type: ProjectType,
    *commands: ProjectCommand,
    dependency_files=(),
    import_modules=(),
    dependency_names=(),
    package_managers=(),
    metadata=None,
) -> ProjectProfile:
    profile_metadata = {
        "project_name": "sample",
        "import_modules": import_modules,
        "dependency_names": dependency_names,
    }
    profile_metadata.update(metadata or {})
    return ProjectProfile(
        f"{project_type.value}-project",
        SourceReference("fixture://project"),
        languages=("Python",),
        project_type=project_type,
        package_managers=package_managers,
        dependency_files=dependency_files,
        commands=commands,
        metadata=profile_metadata,
    )


def project_command(text, purpose, source, confidence=0.9):
    return ProjectCommand(
        text,
        CommandSpec((text,), purpose=purpose, shell=True),
        source,
        confidence,
    )


def build_context(
    project,
    workspace: Path,
    *,
    setup="python --version",
    runtime_base_image="",
    strategy="template",
    plan_metadata=None,
):
    metadata = {"image_reference": "example:latest"}
    if runtime_base_image:
        metadata["runtime_base_image"] = runtime_base_image
    metadata.update(plan_metadata or {})
    plan = BuildPlan(
        "plan",
        project.project_id,
        strategy,
        (BuildStep("docker-build", BuildStage.BUILD, BUILD_COMMAND),),
        metadata=metadata,
        generated_files=(GeneratedFile("setup.sh", setup),),
    )
    command_result = CommandResult(BUILD_COMMAND, 0, stdout=ArtifactRef("fake/build.log"))
    result = BuildResult(
        "attempt",
        plan.plan_id,
        BuildStatus.SUCCEEDED,
        NOW,
        NOW,
        exit_code=0,
        image_reference="example:latest",
        command_results=(command_result,),
    )
    return VerificationContext(project, result, workspace, plan)


class CommandSelectionTests(unittest.TestCase):
    def test_selector_keeps_tests_owned_by_primary_manifest_root(self) -> None:
        selected = TestCommandSelector(max_test_files=2).select_with_details(
            ProjectProfile(
                "python-project",
                SourceReference("fixture://python"),
                languages=("Python",),
                commands=(
                    project_command("pytest", CommandPurpose.TEST, "pyproject.toml"),
                    ProjectCommand(
                        "example-tests",
                        CommandSpec(
                            ("pytest",),
                            purpose=CommandPurpose.TEST,
                            cwd="examples/tutorial",
                        ),
                        "examples/tutorial/pyproject.toml",
                        0.99,
                    ),
                ),
                metadata={
                    "build_root": ".",
                    "project_roots": (".", "examples/tutorial"),
                    "test_files": (
                        "tests/test_app.py",
                        "examples/tutorial/tests/test_app.py",
                    ),
                    "safe_test_files": (
                        "tests/test_app.py",
                        "examples/tutorial/tests/test_app.py",
                    ),
                    "optional_dependency_groups": {"test": ("pytest",)},
                },
            )
        )

        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.targets, ("tests/test_app.py",))
        self.assertEqual(selected.command.command.cwd, None)

    def test_native_standard_runner_beats_specialized_ci_pipeline(self) -> None:
        selected = TestCommandSelector().select_with_details(
            ProjectProfile(
                "native-project",
                SourceReference("fixture://native"),
                languages=("C++",),
                commands=(
                    project_command(
                        "bash ops/pipeline/build-test-sycl.sh pytest",
                        CommandPurpose.TEST,
                        ".github/workflows/sycl_tests.yml",
                        0.98,
                    ),
                    project_command(
                        "pytest -s tests/python",
                        CommandPurpose.TEST,
                        ".github/workflows/main.yml",
                        0.98,
                    ),
                    project_command(
                        "ctest --test-dir build --output-on-failure",
                        CommandPurpose.TEST,
                        "inferred:CMakeLists.txt:test-layout",
                        0.85,
                    ),
                ),
                metadata={"primary_build_system": "cmake"},
            )
        )

        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(
            selected.command.command.display,
            "ctest --test-dir build --output-on-failure",
        )

    def test_make_test_beats_ci_script_with_parent_relative_path(self) -> None:
        selected = TestCommandSelector().select_with_details(
            ProjectProfile(
                "make-project",
                SourceReference("fixture://make"),
                languages=("C++",),
                commands=(
                    ProjectCommand(
                        "make-test",
                        CommandSpec(
                            ("make", "test"),
                            purpose=CommandPurpose.TEST,
                            cwd="src",
                        ),
                        "inferred:Makefile",
                        0.7,
                    ),
                    project_command(
                        "python3 ../tests/instrumented.py --none ./binary",
                        CommandPurpose.TEST,
                        ".github/workflows/matetrack.yml",
                        0.98,
                    ),
                ),
                metadata={"primary_build_system": "make"},
            )
        )

        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.command.command.display, "make test")
        self.assertEqual(selected.command.command.cwd, "src")

    def test_ctest_parallelism_is_bounded_by_verification_policy(self) -> None:
        selected = TestCommandSelector(max_parallel_workers=4).select_with_details(
            profile(
                ProjectType.LIBRARY,
                project_command(
                    "ctest --test-dir build --output-on-failure",
                    CommandPurpose.TEST,
                    "inferred:CMakeLists.txt:test-layout",
                ),
            )
        )

        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(
            selected.command.command.display,
            "ctest --test-dir build --output-on-failure --parallel 4",
        )
        self.assertIn("ctest-bounded-parallel", selected.kind)

    def test_standard_jvm_test_beats_special_ci_test_target(self) -> None:
        selected = TestCommandSelector().select(
            ProjectProfile(
                "jvm-project",
                SourceReference("fixture://jvm"),
                languages=("Java",),
                commands=(
                    project_command(
                        "./mvnw -B -Pslow verify",
                        CommandPurpose.TEST,
                        ".github/workflows/full.yml",
                        1.0,
                    ),
                    project_command(
                        "./mvnw -B test",
                        CommandPurpose.TEST,
                        "inferred:mvnw",
                        0.9,
                    ),
                ),
            )
        )

        self.assertIsNotNone(selected)
        self.assertEqual(selected.command.display, "./mvnw -B test")

    def test_jvm_ci_test_drops_only_evidenced_optional_profile_variable(self) -> None:
        selected = TestCommandSelector().select(
            ProjectProfile(
                "jvm-project",
                SourceReference("fixture://jvm"),
                languages=("Java",),
                ci_files=(".github/workflows/ci.yml",),
                commands=(
                    project_command(
                        "./mvnw test -B -Dlicense.skip=true $TEST_CONTAINERS_PROFILE",
                        CommandPurpose.TEST,
                        ".github/workflows/ci.yml",
                        0.98,
                    ),
                    project_command(
                        "./mvnw -B test",
                        CommandPurpose.TEST,
                        "inferred:mvnw",
                        0.9,
                    ),
                ),
                metadata={
                    "optional_test_profile_variables": ("TEST_CONTAINERS_PROFILE",),
                    "maven_git_hook_install_source": (
                        "pom.xml:git-build-hook-maven-plugin:install"
                    ),
                },
            )
        )

        self.assertIsNotNone(selected)
        self.assertEqual(
            selected.command.display,
            "./mvnw test -B -Dlicense.skip=true -Dgitbuildhook.install.skip=true",
        )

    def test_maven_version_output_flag_does_not_turn_test_into_smoke(self) -> None:
        selected = TestCommandSelector().select(
            ProjectProfile(
                "jvm-project",
                SourceReference("fixture://jvm"),
                languages=("Java",),
                ci_files=(".github/workflows/ci.yml",),
                commands=(
                    project_command(
                        "./mvnw test -B -V --no-transfer-progress",
                        CommandPurpose.TEST,
                        ".github/workflows/ci.yml",
                        0.98,
                    ),
                    project_command(
                        "./mvnw -B test",
                        CommandPurpose.TEST,
                        "inferred:mvnw",
                        0.9,
                    ),
                ),
            )
        )

        self.assertIsNotNone(selected)
        self.assertEqual(
            selected.command.display,
            "./mvnw test -B -V --no-transfer-progress",
        )

    def test_gradle_test_uses_image_proxy_launcher(self) -> None:
        selected = TestCommandSelector().select(
            ProjectProfile(
                "jvm-project",
                SourceReference("fixture://jvm"),
                languages=("Java",),
                commands=(
                    project_command(
                        "./gradlew test",
                        CommandPurpose.TEST,
                        "inferred:gradlew",
                        0.9,
                    ),
                ),
                metadata={"primary_build_system": "gradle"},
            )
        )

        self.assertIsNotNone(selected)
        self.assertEqual(
            selected.command.display,
            "/usr/local/bin/dprauto-gradle-proxy ./gradlew test",
        )

    def test_windows_ci_variable_does_not_override_portable_ctest(self) -> None:
        selected = TestCommandSelector().select(
            ProjectProfile(
                "native-project",
                SourceReference("fixture://native"),
                languages=("C++",),
                commands=(
                    project_command(
                        'pytest test/msvc --ccache "%GITHUB_WORKSPACE%/build/ccache.exe"',
                        CommandPurpose.TEST,
                        ".github/workflows/build.yml",
                        0.99,
                    ),
                    project_command(
                        "ctest --test-dir build --output-on-failure",
                        CommandPurpose.TEST,
                        "inferred:CMakeLists.txt:test-layout",
                        0.85,
                    ),
                ),
            )
        )

        self.assertIsNotNone(selected)
        self.assertEqual(
            selected.command.display,
            "ctest --test-dir build --output-on-failure",
        )

    def test_lowercase_pytest_verbose_flag_is_not_a_version_smoke_probe(self) -> None:
        selected = TestCommandSelector().select(
            ProjectProfile(
                "project",
                SourceReference("fixture://project"),
                commands=(
                    project_command(
                        "python -m pytest tests -v",
                        CommandPurpose.TEST,
                        ".github/workflows/test.yml",
                    ),
                ),
            )
        )

        self.assertIsNotNone(selected)
        self.assertEqual(selected.command.display, "python -m pytest tests -v")

    def test_ci_test_command_wins_and_smoke_is_never_a_project_test(self) -> None:
        selected = TestCommandSelector().select(
            ProjectProfile(
                "project",
                SourceReference("fixture://project"),
                ci_files=(".github/workflows/test.yml",),
                readme_files=("README.md",),
                commands=(
                    project_command(
                        "python -m sample --help",
                        CommandPurpose.TEST,
                        ".github/workflows/test.yml",
                        1.0,
                    ),
                    project_command("python -m pytest -q", CommandPurpose.TEST, "README.md", 0.9),
                    project_command(
                        "python -m unittest discover",
                        CommandPurpose.TEST,
                        ".github/workflows/test.yml",
                        0.8,
                    ),
                ),
            )
        )
        self.assertEqual(selected.command.display, "python -m unittest discover")

    def test_real_test_beats_unresolved_matrix_lint_and_docs_commands(self) -> None:
        selected = TestCommandSelector().select(
            ProjectProfile(
                "project",
                SourceReference("fixture://project"),
                ci_files=(".github/workflows/test.yml",),
                commands=(
                    project_command(
                        "tox -e ${{ matrix.env }}",
                        CommandPurpose.TEST,
                        ".github/workflows/test.yml",
                        1.0,
                    ),
                    project_command(
                        "tox -e docs", CommandPurpose.TEST, ".github/workflows/test.yml", 0.99
                    ),
                    project_command(
                        "ruff check .", CommandPurpose.TEST, ".github/workflows/test.yml", 0.98
                    ),
                    project_command(
                        "python -m pytest -q", CommandPurpose.TEST, "inferred:test-layout", 0.7
                    ),
                ),
            )
        )

        self.assertEqual(selected.command.display, "python -m pytest -q")

    def test_bare_tox_is_narrowed_to_default_environment(self) -> None:
        selected = TestCommandSelector().select(
            ProjectProfile(
                "project",
                SourceReference("fixture://project"),
                commands=(project_command("tox", CommandPurpose.TEST, "tox.ini", 0.9),),
                metadata={"default_tox_env": "py311"},
            )
        )

        self.assertEqual(selected.command.display, "tox -e py311")

    def test_bare_nox_is_narrowed_to_default_session(self) -> None:
        selected = TestCommandSelector().select(
            ProjectProfile(
                "project",
                SourceReference("fixture://project"),
                commands=(project_command("nox", CommandPurpose.TEST, "noxfile.py", 0.9),),
                metadata={"default_nox_session": "tests"},
            )
        )

        self.assertEqual(selected.command.display, "nox -s tests")

    def test_tox_matrix_matches_the_built_python_and_rejects_fuzz(self) -> None:
        selected = TestCommandSelector().select(
            ProjectProfile(
                "project",
                SourceReference("fixture://project"),
                commands=(
                    project_command(
                        "tox -e fuzz",
                        CommandPurpose.TEST,
                        ".github/workflows/fuzz.yml",
                        1.0,
                    ),
                    project_command(
                        "tox -e py38",
                        CommandPurpose.TEST,
                        "tox.ini",
                        0.8,
                    ),
                ),
                metadata={
                    "tox_environments": (
                        {
                            "name": "py38",
                            "kind": "unit",
                            "safe": True,
                            "python_version": "3.8",
                        },
                        {
                            "name": "py311",
                            "kind": "unit",
                            "safe": True,
                            "python_version": "3.11",
                        },
                        {
                            "name": "fuzz",
                            "kind": "expensive",
                            "safe": False,
                            "python_version": "",
                        },
                    )
                },
            ),
            python_version="3.11",
        )

        self.assertEqual(selected.command.display, "tox -e py311")

    def test_nox_python_matrix_is_narrowed_to_one_interpreter(self) -> None:
        selected = TestCommandSelector().select(
            ProjectProfile(
                "project",
                SourceReference("fixture://project"),
                commands=(project_command("nox", CommandPurpose.TEST, "noxfile.py"),),
                metadata={
                    "nox_sessions": (
                        {
                            "name": "tests",
                            "kind": "unit",
                            "safe": True,
                            "python_versions": ("3.10", "3.11"),
                            "parameterized": False,
                        },
                    )
                },
            ),
            python_version="3.11",
        )

        self.assertEqual(selected.command.display, "nox -s tests-3.11")

    def test_only_external_service_or_quality_commands_are_skipped(self) -> None:
        selected = TestCommandSelector().select(
            ProjectProfile(
                "project",
                SourceReference("fixture://project"),
                commands=(
                    project_command(
                        "pytest tests/integration",
                        CommandPurpose.TEST,
                        ".github/workflows/integration.yml",
                    ),
                    project_command(
                        "tox -e lint",
                        CommandPurpose.TEST,
                        "tox.ini",
                    ),
                ),
            )
        )

        self.assertIsNone(selected)

    def test_large_pytest_suite_is_bounded_across_local_unit_directories(self) -> None:
        project = ProjectProfile(
            "project",
            SourceReference("fixture://project"),
            commands=(
                project_command(
                    "pytest tests/unit",
                    CommandPurpose.TEST,
                    "README.rst",
                ),
            ),
            metadata={
                "test_files": (
                    "tests/unit/test_b.py",
                    "tests/unit/test_a.py",
                    "tests/unit/docs/test_b.py",
                    "tests/unit/docs/test_a.py",
                    "tests/unit/s3/test_api.py",
                ),
                "safe_test_files": (
                    "tests/unit/test_b.py",
                    "tests/unit/test_a.py",
                    "tests/unit/docs/test_b.py",
                    "tests/unit/docs/test_a.py",
                    "tests/unit/s3/test_api.py",
                ),
            },
        )

        selection = TestCommandSelector(max_test_files=3).select_with_details(project)

        self.assertIsNotNone(selection)
        self.assertEqual(selection.kind, "bounded-file-slice")
        self.assertEqual(
            selection.targets,
            (
                "tests/unit/test_a.py",
                "tests/unit/docs/test_a.py",
                "tests/unit/s3/test_api.py",
            ),
        )
        self.assertEqual(
            selection.command.command.argv,
            (
                "python",
                "-m",
                "pytest",
                "tests/unit/test_a.py",
                "tests/unit/docs/test_a.py",
                "tests/unit/s3/test_api.py",
            ),
        )
        self.assertEqual(selection.original_command.display, "pytest tests/unit")

    def test_external_test_files_are_excluded_even_for_a_small_suite(self) -> None:
        selection = TestCommandSelector().select_with_details(
            ProjectProfile(
                "project",
                SourceReference("fixture://project"),
                commands=(
                    project_command(
                        "python -m pytest",
                        CommandPurpose.TEST,
                        "inferred:test-layout",
                    ),
                ),
                metadata={
                    "test_files": (
                        "tests/test_remote.py",
                        "tests/test_unit.py",
                    ),
                    "safe_test_files": ("tests/test_unit.py",),
                    "external_test_files": ("tests/test_remote.py",),
                },
            )
        )

        self.assertEqual(selection.targets, ("tests/test_unit.py",))
        self.assertEqual(
            selection.command.command.display,
            "python -m pytest tests/test_unit.py",
        )

    def test_optional_dependencies_make_a_small_pytest_suite_explicit(self) -> None:
        selection = TestCommandSelector().select_with_details(
            ProjectProfile(
                "project",
                SourceReference("fixture://project"),
                commands=(
                    project_command(
                        "python -m pytest",
                        CommandPurpose.TEST,
                        "inferred:test-layout",
                    ),
                ),
                metadata={
                    "safe_test_files": ("tests/test_cache.py",),
                    "optional_dependency_groups": {"cache": ("requests-cache",)},
                },
            )
        )

        self.assertEqual(selection.targets, ("tests/test_cache.py",))
        self.assertEqual(
            selection.command.command.display,
            "python -m pytest tests/test_cache.py",
        )

    def test_coverage_only_arguments_are_removed_from_testability(self) -> None:
        selection = TestCommandSelector().select_with_details(
            ProjectProfile(
                "project",
                SourceReference("fixture://project"),
                commands=(
                    project_command(
                        "pytest --cov=sample --cov-report xml --tb=short",
                        CommandPurpose.TEST,
                        ".github/workflows/tests.yml",
                    ),
                ),
            )
        )

        self.assertEqual(selection.kind, "coverage-normalized")
        self.assertEqual(selection.command.command.display, "pytest --tb=short")
        self.assertEqual(
            selection.original_command.display,
            "pytest --cov=sample --cov-report xml --tb=short",
        )

    def test_gradle_large_suite_uses_stable_repository_test_classes(self) -> None:
        selection = TestCommandSelector(max_test_files=2).select_with_details(
            ProjectProfile(
                "jvm-project",
                SourceReference("fixture://jvm"),
                languages=("Java",),
                commands=(
                    project_command(
                        "./gradlew test",
                        CommandPurpose.TEST,
                        "inferred:gradlew",
                    ),
                ),
                metadata={
                    "primary_build_system": "gradle",
                    "gradle_projects_by_directory": {},
                    "test_files": (
                        "src/test/java/example/AlphaTest.java",
                        "src/test/java/example/BetaTest.java",
                        "src/test/java/example/AsyncTest.java",
                    ),
                    "safe_test_files": (
                        "src/test/java/example/AlphaTest.java",
                        "src/test/java/example/BetaTest.java",
                    ),
                    "unstable_test_files": (
                        "src/test/java/example/AsyncTest.java",
                    ),
                },
            )
        )

        self.assertIsNotNone(selection)
        self.assertEqual(selection.kind, "bounded-file-slice")
        self.assertEqual(
            selection.targets,
            (
                "src/test/java/example/AlphaTest.java",
                "src/test/java/example/BetaTest.java",
            ),
        )
        self.assertEqual(
            selection.command.command.display,
            "/usr/local/bin/dprauto-gradle-proxy ./gradlew test "
            "--tests example.AlphaTest --tests example.BetaTest",
        )

    def test_gradle_test_slice_uses_repository_project_mapping(self) -> None:
        selection = TestCommandSelector(max_test_files=1).select_with_details(
            ProjectProfile(
                "jvm-project",
                SourceReference("fixture://jvm"),
                languages=("Java",),
                commands=(
                    project_command("./gradlew test", CommandPurpose.TEST, "inferred:gradlew"),
                ),
                metadata={
                    "primary_build_system": "gradle",
                    "gradle_projects_by_directory": {"access": "spring-security-access"},
                    "test_files": (
                        "access/src/test/java/example/AccessTests.java",
                        "access/src/test/java/example/AsyncTests.java",
                    ),
                    "safe_test_files": (
                        "access/src/test/java/example/AccessTests.java",
                    ),
                    "unstable_test_files": (
                        "access/src/test/java/example/AsyncTests.java",
                    ),
                },
            )
        )

        self.assertEqual(
            selection.command.command.display,
            "/usr/local/bin/dprauto-gradle-proxy ./gradlew "
            ":spring-security-access:test --tests example.AccessTests",
        )

    def test_pytest_capture_is_disabled_for_import_time_stdio_rewrapping(self) -> None:
        selection = TestCommandSelector().select_with_details(
            ProjectProfile(
                "project",
                SourceReference("fixture://project"),
                commands=(
                    project_command(
                        "python -m pytest tests/test_common.py",
                        CommandPurpose.TEST,
                        "README.md",
                    ),
                ),
                metadata={
                    "pytest_capture_incompatible_files": ("src/sample/common.py",),
                },
            )
        )

        self.assertEqual(selection.kind, "pytest-capture-disabled")
        self.assertEqual(
            selection.command.command.display,
            "python -m pytest tests/test_common.py -s",
        )
        self.assertIn("rewraps sys.stdout/sys.stderr", selection.reason)

    def test_required_secret_or_external_only_tests_are_not_executed(self) -> None:
        secret = ProjectProfile(
            "secret-project",
            SourceReference("fixture://secret"),
            commands=(project_command("pytest", CommandPurpose.TEST, "pyproject.toml"),),
            metadata={"test_required_environment_variables": ("SERVICE_TOKEN",)},
        )
        external = ProjectProfile(
            "external-project",
            SourceReference("fixture://external"),
            commands=(project_command("pytest", CommandPurpose.TEST, "pyproject.toml"),),
            metadata={
                "test_files": ("tests/integration/test_api.py",),
                "safe_test_files": (),
                "external_test_files": ("tests/integration/test_api.py",),
            },
        )

        self.assertIsNone(TestCommandSelector().select(secret))
        self.assertIsNone(TestCommandSelector().select(external))


class VerificationPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name)
        self.runtime = FakeRuntime()

    def test_build_exit_zero_is_not_enough_for_installability(self) -> None:
        project = profile(
            ProjectType.SCRIPT,
            dependency_files=("requirements.txt",),
            dependency_names=("requests",),
        )
        result = InstallabilityVerifier(self.runtime).verify(
            build_context(project, self.workspace, setup="python --version")
        )
        self.assertEqual(result.status, VerificationStatus.FAILED)
        self.assertEqual(
            next(
                check for check in result.checks if check.name == "dependency-installation"
            ).status,
            VerificationStatus.FAILED,
        )

    def test_installability_uses_language_neutral_plan_dependency_contract(self) -> None:
        project = replace(
            profile(ProjectType.LIBRARY),
            languages=("Java",),
            package_managers=("maven",),
            dependency_files=("pom.xml",),
        )
        command = "./mvnw -B -DskipTests package"
        context = build_context(
            project,
            self.workspace,
            setup=f"FROM fixed\nRUN {command}\n",
            strategy="jvm-template",
            plan_metadata={"dependency_installation_commands": (command,)},
        )

        result = InstallabilityVerifier(self.runtime).verify(context)
        dependency = next(
            check for check in result.checks if check.name == "dependency-installation"
        )

        self.assertEqual(result.status, VerificationStatus.PASSED)
        self.assertEqual(dependency.metadata["contract_source"], "build-plan")
        self.assertEqual(dependency.metadata["contract_commands"], (command,))

    def test_jvm_testability_runs_standard_runner_without_python_bootstrap(self) -> None:
        project = replace(
            profile(
                ProjectType.LIBRARY,
                project_command("./gradlew test", CommandPurpose.TEST, "inferred:gradlew"),
            ),
            languages=("Java",),
            package_managers=("gradle",),
            dependency_files=("build.gradle",),
        )

        result = TestabilityVerifier(self.runtime).verify(
            build_context(project, self.workspace, strategy="jvm-template")
        )

        self.assertEqual(result.status, VerificationStatus.PASSED)
        self.assertEqual(self.runtime.commands[0].display, "./gradlew test")
        self.assertEqual(result.metadata["timeout_policy"], "test-only")

    def test_testability_uses_build_runtime_to_bound_nox_python_matrix(self) -> None:
        project = profile(
            ProjectType.LIBRARY,
            project_command("nox", CommandPurpose.TEST, "noxfile.py"),
            dependency_files=("requirements-test.txt", "pyproject.toml"),
            package_managers=("poetry",),
            metadata={
                "test_dependency_extras": ("testing",),
                "test_dependency_manager_groups": ("test", "dev"),
                "nox_sessions": (
                    {
                        "name": "tests",
                        "kind": "unit",
                        "safe": True,
                        "python_versions": ("3.10", "3.11"),
                        "parameterized": False,
                    },
                ),
            },
        )
        context = build_context(
            project,
            self.workspace,
            runtime_base_image="python:3.11-slim",
        )

        result = TestabilityVerifier(self.runtime).verify(context)

        self.assertEqual(result.status, VerificationStatus.PASSED)
        self.assertEqual(result.metadata["original_command"], "nox -s tests-3.11")
        self.assertIn("python -m pip install nox==2024.10.9", result.metadata["command"])
        self.assertNotIn("requirements-test.txt", result.metadata["command"])
        self.assertNotIn("poetry install", result.metadata["command"])

    def test_testability_installs_selected_test_tool_ephemerally(self) -> None:
        project = profile(
            ProjectType.LIBRARY,
            project_command("python -m pytest -q", CommandPurpose.TEST, "README.md"),
        )

        result = TestabilityVerifier(self.runtime).verify(build_context(project, self.workspace))

        self.assertEqual(result.status, VerificationStatus.PASSED)
        self.assertIn(
            "python -m pip install pytest==8.3.5 && "
            "env -u HTTP_PROXY -u HTTPS_PROXY -u NO_PROXY "
            "-u http_proxy -u https_proxy -u no_proxy python -m pytest -q",
            self.runtime.commands[0].display,
        )
        self.assertEqual(result.metadata["original_command"], "python -m pytest -q")
        self.assertEqual(result.metadata["timeout_policy"], "dependency-and-test")
        self.assertEqual(result.metadata["requested_timeout_seconds"], 180)

    def test_dependency_setup_keeps_proxy_for_install_but_clears_it_for_tests(self) -> None:
        project = profile(
            ProjectType.LIBRARY,
            project_command(
                "python -m pytest",
                CommandPurpose.TEST,
                "inferred:test-layout",
            ),
        )

        result = TestabilityVerifier(self.runtime).verify(
            build_context(project, self.workspace)
        )

        self.assertTrue(result.passed)
        command = self.runtime.commands[0].display
        self.assertTrue(command.startswith("python -m pip install pytest==8.3.5 && env "))
        self.assertIn("-u HTTP_PROXY", command)
        self.assertTrue(command.endswith("python -m pytest"))

    def test_testability_keeps_short_timeout_when_no_dependency_setup_is_needed(self) -> None:
        project = profile(
            ProjectType.LIBRARY,
            project_command("python -m unittest", CommandPurpose.TEST, "README.md"),
        )
        verifier = TestabilityVerifier(
            self.runtime,
            config=VerificationConfig(
                command_timeout_seconds=41,
                dependency_command_timeout_seconds=173,
            ),
        )

        result = verifier.verify(build_context(project, self.workspace))

        self.assertEqual(result.metadata["timeout_policy"], "test-only")
        self.assertEqual(result.metadata["requested_timeout_seconds"], 41)

    def test_testability_uses_jvm_timeout_and_ci_environment_contract(self) -> None:
        project = ProjectProfile(
            "jvm-project",
            SourceReference("fixture://jvm"),
            languages=("Java",),
            project_type=ProjectType.LIBRARY,
            package_managers=("gradle",),
            commands=(
                project_command("./gradlew test", CommandPurpose.TEST, "inferred:gradlew"),
            ),
            metadata={
                "primary_build_system": "gradle",
                "test_environment_variables": {"CI": "true"},
                "test_prerequisite_evidence": ("build.gradle:System.getenv(CI)",),
            },
        )
        verifier = TestabilityVerifier(
            self.runtime,
            config=VerificationConfig(
                command_timeout_seconds=41,
                jvm_command_timeout_seconds=701,
            ),
        )

        result = verifier.verify(build_context(project, self.workspace))

        self.assertEqual(result.metadata["requested_timeout_seconds"], 701)
        self.assertEqual(result.metadata["test_environment_variable_names"], ("CI",))
        self.assertEqual(self.runtime.environments[0].command_environment, {"CI": "true"})

    def test_testability_records_bounded_slice_and_original_command(self) -> None:
        (self.workspace / "tests/unit").mkdir(parents=True)
        for index in range(5):
            (self.workspace / f"tests/unit/test_{index}.py").write_text(
                "def test_ok(): assert True\n",
                encoding="utf-8",
            )
        project = profile(
            ProjectType.LIBRARY,
            project_command("pytest tests/unit", CommandPurpose.TEST, "README.rst"),
            metadata={
                "test_files": tuple(f"tests/unit/test_{index}.py" for index in range(5)),
                "safe_test_files": tuple(f"tests/unit/test_{index}.py" for index in range(5)),
            },
        )
        verifier = TestabilityVerifier(
            self.runtime,
            config=VerificationConfig(max_test_files_per_slice=2),
        )

        result = verifier.verify(build_context(project, self.workspace))

        self.assertEqual(result.metadata["selection_kind"], "bounded-file-slice")
        self.assertEqual(result.metadata["selection_target_count"], 2)
        self.assertEqual(
            result.metadata["selection_targets"],
            ("tests/unit/test_0.py", "tests/unit/test_1.py"),
        )
        self.assertEqual(result.metadata["original_command"], "pytest tests/unit")
        self.assertTrue(
            self.runtime.commands[0].display.endswith(
                "python -m pytest tests/unit/test_0.py tests/unit/test_1.py"
            )
        )

    def test_testability_rejects_missing_make_target_and_uses_next_candidate(self) -> None:
        (self.workspace / "Makefile").write_text(
            "check:\n\t@echo ok\n",
            encoding="utf-8",
        )
        project = profile(
            ProjectType.LIBRARY,
            project_command("make test", CommandPurpose.TEST, "README.md", 0.99),
            project_command("make check", CommandPurpose.TEST, "README.md", 0.90),
            metadata={"primary_build_system": "make"},
        )

        result = TestabilityVerifier(self.runtime).verify(
            build_context(project, self.workspace)
        )

        self.assertTrue(result.passed)
        self.assertEqual(result.metadata["attempted_candidate_count"], 1)
        self.assertEqual(
            tuple(item["status"] for item in result.metadata["command_attempts"]),
            ("preflight-rejected", "passed"),
        )
        self.assertEqual(self.runtime.commands[0].display, "make check")

    def test_testability_dry_runs_target_from_generated_makefile(self) -> None:
        project = profile(
            ProjectType.LIBRARY,
            project_command("make check", CommandPurpose.TEST, "inferred:configure.ac"),
            metadata={"primary_build_system": "autotools"},
        )
        preflight = "make --dry-run --no-builtin-rules check"
        self.runtime.exit_code_by_command[preflight] = 2
        self.runtime.output_by_command[preflight] = (
            "make: *** No rule to make target 'devprogs', needed by 'check'. Stop."
        )

        result = TestabilityVerifier(self.runtime).verify(
            build_context(project, self.workspace)
        )

        self.assertEqual(result.status, VerificationStatus.SKIPPED)
        self.assertEqual(result.metadata["skip_reason"], "generated-make-target-dry-run-failed")
        self.assertEqual(result.metadata["make_target"], "check")
        self.assertEqual(self.runtime.commands[0].display, preflight)

    def test_testability_does_not_hide_real_test_failure_with_fallback(self) -> None:
        project = profile(
            ProjectType.LIBRARY,
            project_command("python -m unittest", CommandPurpose.TEST, "README.md", 0.99),
            project_command("tox", CommandPurpose.TEST, "tox.ini", 0.80),
        )
        self.runtime.exit_code = 1
        self.runtime.output = "FAILED: expected 2 but got 1"

        result = TestabilityVerifier(self.runtime).verify(
            build_context(project, self.workspace)
        )

        self.assertEqual(result.status, VerificationStatus.FAILED)
        self.assertEqual(result.metadata["attempted_candidate_count"], 1)
        self.assertEqual(len(self.runtime.commands), 1)

    def test_testability_retries_invalid_candidate_and_keeps_consistent_checks(self) -> None:
        project = profile(
            ProjectType.LIBRARY,
            project_command(
                "python -m unittest",
                CommandPurpose.TEST,
                "inferred:test-layout",
                0.99,
            ),
            project_command("tox", CommandPurpose.TEST, "tox.ini", 0.80),
        )
        self.runtime.output_by_command["python -m unittest"] = "Ran 0 tests\nNO TESTS RAN"

        result = TestabilityVerifier(self.runtime).verify(
            build_context(project, self.workspace)
        )

        self.assertTrue(result.passed)
        self.assertEqual(result.metadata["attempted_candidate_count"], 2)
        self.assertEqual(result.metadata["selected_candidate"], 2)
        self.assertTrue(result.metadata["candidate_fallback_used"])
        self.assertEqual(result.metadata["candidate_stop_reason"], "tests-passed")
        self.assertEqual(
            tuple(item["outcome_category"] for item in result.metadata["command_attempts"]),
            ("no-tests-collected", "tests-passed"),
        )
        self.assertEqual(
            tuple(check.status for check in result.checks),
            (VerificationStatus.SKIPPED, VerificationStatus.PASSED),
        )
        self.assertTrue(result.checks[0].metadata["superseded"])
        self.assertEqual(result.checks[0].metadata["original_status"], "failed")

    def test_testability_records_observed_test_count_and_evidence_strength(self) -> None:
        project = profile(
            ProjectType.LIBRARY,
            project_command(
                "python -m unittest",
                CommandPurpose.TEST,
                ".github/workflows/tests.yml",
            ),
        )
        self.runtime.output_by_command["python -m unittest"] = "Ran 12 tests in 0.25s\nOK"

        result = TestabilityVerifier(self.runtime).verify(
            build_context(project, self.workspace)
        )

        self.assertTrue(result.passed)
        self.assertEqual(result.metadata["observed_test_count"], 12)
        self.assertEqual(result.metadata["test_evidence_strength"], "strong")
        self.assertEqual(result.metadata["test_evidence_source_kind"], "ci")
        self.assertEqual(result.metadata["test_evidence_scope"], "project-command")

    def test_bounded_gradle_tests_require_and_count_xml_results(self) -> None:
        project = replace(
            profile(
                ProjectType.LIBRARY,
                project_command(
                    "./gradlew test",
                    CommandPurpose.TEST,
                    "inferred:gradlew",
                ),
                package_managers=("gradle",),
                metadata={
                    "primary_build_system": "gradle",
                    "safe_test_files": tuple(
                        f"src/test/java/example/Sample{index}Test.java"
                        for index in range(9)
                    ),
                },
            ),
            languages=("Java",),
        )
        selection = TestCommandSelector().select_with_details(project)
        self.assertIsNotNone(selection)
        assert selection is not None

        wrapped = TestabilityVerifier._with_gradle_result_probe(
            project,
            selection.command.command,
            selection,
        )
        evidence = TestabilityVerifier._test_evidence(
            selection,
            "BUILD SUCCESSFUL\nDPRAUTO_TEST_COUNT=8\n",
            passed=True,
            outcome_category="tests-passed",
        )

        self.assertIn("build/test-results", wrapped.display)
        self.assertIn("DPRAUTO_NO_TESTS_COLLECTED", wrapped.display)
        self.assertEqual(evidence["observed_test_count"], 8)
        self.runtime.exit_code = 1
        self.runtime.output = "BUILD SUCCESSFUL\nDPRAUTO_NO_TESTS_COLLECTED\n"
        execution = self.runtime.run_image("image", wrapped, timeout_seconds=30)
        self.assertEqual(
            TestabilityVerifier._execution_outcome(execution),
            ("no-tests-collected", True),
        )

    def test_testability_classifies_timeout_without_trying_another_candidate(self) -> None:
        project = profile(
            ProjectType.LIBRARY,
            project_command(
                "python -m unittest",
                CommandPurpose.TEST,
                "inferred:test-layout",
                0.99,
            ),
            project_command("tox", CommandPurpose.TEST, "tox.ini", 0.80),
        )
        self.runtime.exit_code_by_command["python -m unittest"] = 1
        self.runtime.timed_out_by_command["python -m unittest"] = True

        result = TestabilityVerifier(self.runtime).verify(
            build_context(project, self.workspace)
        )

        self.assertEqual(result.status, VerificationStatus.FAILED)
        self.assertEqual(result.metadata["outcome_category"], "test-timeout")
        self.assertFalse(result.metadata["candidate_retryable"])
        self.assertEqual(result.metadata["attempted_candidate_count"], 1)
        self.assertEqual(
            result.metadata["candidate_stop_reason"],
            "non-retryable-test-timeout",
        )

    def test_ctest_enables_tests_only_during_testability(self) -> None:
        project = ProjectProfile(
            "native-project",
            SourceReference("fixture://native"),
            languages=("C++",),
            project_type=ProjectType.LIBRARY,
            commands=(
                project_command(
                    "ctest --test-dir build --output-on-failure",
                    CommandPurpose.TEST,
                    "inferred:CMakeLists.txt:test-layout",
                ),
            ),
            metadata={
                "primary_build_system": "cmake",
                "cmake_test_configuration_arguments": ("-DBUILD_TESTING=ON",),
                "cmake_test_build_target": "tests",
            },
        )

        result = TestabilityVerifier(self.runtime).verify(
            build_context(project, self.workspace, strategy="native-template")
        )

        self.assertTrue(result.passed)
        command = self.runtime.commands[0].display
        self.assertIn("timeout --signal=TERM --kill-after=5s 900s", command)
        self.assertIn("cmake -S . -B build -DBUILD_TESTING=ON", command)
        self.assertIn("cmake --build build --target tests", command)
        self.assertIn("&& ctest --test-dir build --output-on-failure", command)
        self.assertEqual(result.metadata["timeout_policy"], "native-test-preparation-and-test")
        self.assertEqual(result.metadata["requested_timeout_seconds"], 1200)
        self.assertEqual(result.metadata["native_test_preparation_timeout_seconds"], 900)

    def test_testability_records_capture_compatibility_command_and_reason(self) -> None:
        project = profile(
            ProjectType.LIBRARY,
            project_command("pytest tests/unit", CommandPurpose.TEST, "README.rst"),
            metadata={
                "test_files": (
                    "tests/unit/test_common.py",
                    "tests/unit/test_util.py",
                ),
                "safe_test_files": (
                    "tests/unit/test_common.py",
                    "tests/unit/test_util.py",
                ),
                "pytest_capture_incompatible_files": ("src/sample/common.py",),
            },
        )

        result = TestabilityVerifier(self.runtime).verify(build_context(project, self.workspace))

        self.assertEqual(result.metadata["original_command"], "pytest tests/unit")
        self.assertEqual(
            result.metadata["selection_kind"],
            "pytest-capture-disabled",
        )
        self.assertEqual(result.metadata["selection_targets"], ())
        self.assertIn("rewraps sys.stdout/sys.stderr", result.metadata["selection_reason"])
        self.assertTrue(result.metadata["command"].endswith("pytest tests/unit -s"))
        self.assertTrue(self.runtime.commands[0].display.endswith("pytest tests/unit -s"))

    def test_testability_skips_required_secret_without_running_a_container(self) -> None:
        project = profile(
            ProjectType.LIBRARY,
            project_command("pytest", CommandPurpose.TEST, "pyproject.toml"),
            metadata={"test_required_environment_variables": ("SERVICE_TOKEN",)},
        )

        result = TestabilityVerifier(self.runtime).verify(build_context(project, self.workspace))

        self.assertEqual(result.status, VerificationStatus.SKIPPED)
        self.assertEqual(result.metadata["skip_reason"], "required-secret-environment")
        self.assertEqual(
            result.metadata["required_environment_variables"],
            ("SERVICE_TOKEN",),
        )
        self.assertIn("SERVICE_TOKEN", result.summary)
        self.assertFalse(self.runtime.commands)

    def test_testability_orchestrates_only_explicit_selected_test_services(self) -> None:
        project = profile(
            ProjectType.LIBRARY,
            project_command("python -m unittest", CommandPurpose.TEST, "README.md"),
            metadata={
                "available_test_services": ("postgresql", "redis"),
                "test_service_requirements": ("postgresql",),
                "test_environment_variables": {"PG_DATABASE": "fixture"},
                "test_prerequisite_evidence": ("ci.yml:services.postgres",),
            },
        )

        result = TestabilityVerifier(self.runtime).verify(build_context(project, self.workspace))

        self.assertEqual(result.status, VerificationStatus.PASSED)
        self.assertEqual(result.metadata["required_services"], ("postgresql",))
        self.assertEqual(result.metadata["service_images"], ("postgres:16-alpine",))
        self.assertNotIn("redis", result.metadata["required_services"])
        self.assertEqual(len(self.runtime.environments), 1)
        self.assertEqual(self.runtime.environments[0].command_environment["PG_DATABASE"], "fixture")

    def test_testability_skips_service_when_orchestration_is_disabled(self) -> None:
        project = profile(
            ProjectType.LIBRARY,
            project_command("python -m unittest", CommandPurpose.TEST, "README.md"),
            metadata={"test_service_requirements": ("redis",)},
        )

        result = TestabilityVerifier(
            self.runtime,
            config=VerificationConfig(service_orchestration_enabled=False),
        ).verify(build_context(project, self.workspace))

        self.assertEqual(result.status, VerificationStatus.SKIPPED)
        self.assertEqual(result.metadata["skip_reason"], "service-orchestration-disabled")
        self.assertFalse(self.runtime.commands)

    def test_testability_routes_required_executable_through_environment_runtime(self) -> None:
        project = profile(
            ProjectType.LIBRARY,
            project_command("python -m unittest", CommandPurpose.TEST, "README.md"),
            metadata={"test_required_executables": ("tmux",)},
        )

        result = TestabilityVerifier(self.runtime).verify(build_context(project, self.workspace))

        self.assertEqual(result.status, VerificationStatus.PASSED)
        self.assertEqual(result.metadata["required_executables"], ("tmux",))
        self.assertEqual(len(self.runtime.environments), 1)
        self.assertEqual(self.runtime.environments[0].required_executables, ("tmux",))

    def test_testability_installs_agent_overlay_only_in_temporary_container(self) -> None:
        project = profile(
            ProjectType.LIBRARY,
            project_command("python -m pytest -q", CommandPurpose.TEST, "README.md"),
        )
        overlay = self.workspace / ".dprauto" / "requirements-verification.txt"
        overlay.parent.mkdir(parents=True)
        overlay.write_text(
            "# DPRAuto Testability-only dependency overlay\npytest-mock==3.14.0\n",
            encoding="utf-8",
        )

        result = TestabilityVerifier(self.runtime).verify(build_context(project, self.workspace))

        executed = self.runtime.commands[0].display
        self.assertIn(
            "python -m pip install pytest==8.3.5 pytest-mock==3.14.0",
            executed,
        )
        self.assertTrue(executed.endswith("python -m pytest -q"))
        self.assertEqual(
            result.metadata["verification_overlay_packages"],
            ("pytest-mock==3.14.0",),
        )

    def test_testability_uses_configured_fixed_runner_version(self) -> None:
        project = profile(
            ProjectType.LIBRARY,
            project_command("pytest -q", CommandPurpose.TEST, "README.md"),
        )
        verifier = TestabilityVerifier(
            self.runtime,
            config=VerificationConfig(pytest_version="8.4.2"),
        )

        verifier.verify(build_context(project, self.workspace))

        self.assertIn("pip install pytest==8.4.2", self.runtime.commands[0].display)

    def test_testability_preserves_project_owned_pytest_version(self) -> None:
        project = profile(
            ProjectType.LIBRARY,
            project_command("pytest -q", CommandPurpose.TEST, "README.md"),
            dependency_files=("requirements-tests.txt",),
            package_managers=("pip",),
        )

        TestabilityVerifier(self.runtime).verify(build_context(project, self.workspace))

        executed = self.runtime.commands[0].display
        self.assertIn("python -m pip install -r requirements-tests.txt", executed)
        self.assertNotIn("pytest==8.3.5", executed)

    def test_testability_does_not_append_unhashed_runner_to_lock_requirements(self) -> None:
        project = profile(
            ProjectType.LIBRARY,
            project_command("python -m pytest", CommandPurpose.TEST, "tox.ini"),
            dependency_files=("requirements-dev-lock.txt",),
            package_managers=("pip",),
        )

        TestabilityVerifier(self.runtime).verify(build_context(project, self.workspace))

        executed = self.runtime.commands[0].display
        self.assertIn("pip install -r requirements-dev-lock.txt", executed)
        self.assertNotIn("requirements-dev-lock.txt pytest", executed)

    def test_testability_uses_ci_confirmed_tox_parallelism_with_fixed_cap(self) -> None:
        project = profile(
            ProjectType.LIBRARY,
            project_command("pytest", CommandPurpose.TEST, "README.rst"),
            dependency_files=("pyproject.toml",),
            package_managers=("pip",),
            metadata={
                "test_dependency_extras": ("tests",),
                "pytest_parallel": {
                    "runner": "pytest",
                    "factor": "parallel",
                    "dependency": "pytest-xdist",
                    "argument": "--numprocesses",
                    "requested_workers": "auto",
                    "source": "tox.ini",
                    "ci_confirmed": True,
                },
            },
        )

        result = TestabilityVerifier(self.runtime).verify(build_context(project, self.workspace))

        executed = self.runtime.commands[0].display
        self.assertIn(
            "python -m pip install '.[tests]' pytest-xdist==3.6.1",
            executed,
        )
        self.assertTrue(executed.endswith("pytest --numprocesses 4"))
        self.assertEqual(result.metadata["parallel_workers"], 4)
        self.assertEqual(result.metadata["parallel_source"], "tox.ini+ci")

    def test_testability_does_not_parallelize_without_ci_confirmation(self) -> None:
        project = profile(
            ProjectType.LIBRARY,
            project_command("pytest", CommandPurpose.TEST, "README.rst"),
            metadata={
                "pytest_parallel": {
                    "runner": "pytest",
                    "dependency": "pytest-xdist",
                    "argument": "--numprocesses",
                    "requested_workers": "auto",
                    "ci_confirmed": False,
                }
            },
        )

        result = TestabilityVerifier(self.runtime).verify(build_context(project, self.workspace))

        self.assertNotIn("pytest-xdist", self.runtime.commands[0].display)
        self.assertNotIn("--numprocesses", self.runtime.commands[0].display)
        self.assertEqual(result.metadata["parallel_workers"], 0)

    def test_testability_selects_one_native_dependency_source_after_runtime_build(self) -> None:
        command = project_command("python -m pytest -q", CommandPurpose.TEST, "pyproject.toml")
        project = profile(
            ProjectType.LIBRARY,
            command,
            dependency_files=("requirements-test.txt", "pyproject.toml"),
            package_managers=("poetry",),
            metadata={
                "test_dependency_extras": ("testing",),
                "test_dependency_manager_groups": ("test", "dev"),
            },
        )

        result = TestabilityVerifier(self.runtime).verify(build_context(project, self.workspace))

        executed = self.runtime.commands[0].display
        self.assertTrue(result.passed)
        self.assertNotIn("requirements-test.txt", executed)
        self.assertIn("poetry install --only main,test,dev", executed)
        self.assertIn("--extras testing", executed)
        self.assertNotIn("pytest==8.3.5", executed)

    def test_testability_bootstraps_runner_when_declared_group_lacks_pytest(self) -> None:
        project = profile(
            ProjectType.LIBRARY,
            project_command("python -m pytest", CommandPurpose.TEST, "pyproject.toml"),
            dependency_files=("pyproject.toml",),
            package_managers=("poetry",),
            metadata={
                "test_dependency_manager_groups": ("dev",),
                "manager_dependency_groups": {"dev": ("jsonschema",)},
            },
        )

        TestabilityVerifier(self.runtime).verify(build_context(project, self.workspace))

        executed = self.runtime.commands[0].display
        self.assertIn("poetry install --only main,dev", executed)
        self.assertIn("python -m pip install pytest==8.3.5", executed)

    def test_testability_prefers_pip_extra_over_broad_dev_requirements(self) -> None:
        project = profile(
            ProjectType.LIBRARY,
            project_command("pytest", CommandPurpose.TEST, "README.rst"),
            dependency_files=("requirements-dev.txt", "pyproject.toml"),
            package_managers=("pip",),
            metadata={"test_dependency_extras": ("tests",)},
        )

        result = TestabilityVerifier(self.runtime).verify(build_context(project, self.workspace))

        self.assertTrue(result.passed)
        executed = self.runtime.commands[0].display
        self.assertIn("python -m pip install '.[tests]'", executed)
        self.assertNotIn("pytest==8.3.5", executed)
        self.assertNotIn("requirements-dev.txt", executed)

    def test_testability_selects_narrowest_single_requirements_file(self) -> None:
        project = profile(
            ProjectType.LIBRARY,
            project_command("pytest", CommandPurpose.TEST, "README.md"),
            dependency_files=(
                "requirements-dev.txt",
                "requirements-testing.in",
                "requirements-tox.txt",
            ),
            package_managers=("pip",),
        )

        TestabilityVerifier(self.runtime).verify(build_context(project, self.workspace))

        executed = self.runtime.commands[0].display
        self.assertIn(
            "pip install -r requirements-testing.in",
            executed,
        )
        self.assertNotIn("pytest==8.3.5", executed)
        self.assertNotIn("requirements-dev.txt", executed)
        self.assertNotIn("requirements-tox.txt", executed)

    def test_testability_prefers_dedicated_tests_over_docs_tests_requirements(self) -> None:
        project = profile(
            ProjectType.LIBRARY,
            project_command("pytest", CommandPurpose.TEST, "README.md"),
            dependency_files=(
                "requirements-docs-tests.txt",
                "requirements-tests.txt",
                "requirements.txt",
            ),
            package_managers=("pip",),
        )

        TestabilityVerifier(self.runtime).verify(build_context(project, self.workspace))

        executed = self.runtime.commands[0].display
        self.assertIn("pip install -r requirements-tests.txt", executed)
        self.assertNotIn("requirements-docs-tests.txt", executed)

    def test_testability_installs_minimal_import_closure_from_broad_dev_file(self) -> None:
        (self.workspace / "tests").mkdir()
        (self.workspace / "tests" / "test_a.py").write_text(
            "import aiohttp\ndef test_a(): pass\n", encoding="utf-8"
        )
        (self.workspace / "tests" / "test_b.py").write_text(
            "import requests_cache\ndef test_b(): pass\n", encoding="utf-8"
        )
        (self.workspace / "tests" / "test_c.py").write_text(
            "def test_c(): pass\n", encoding="utf-8"
        )
        (self.workspace / "requirements-dev.txt").write_text(
            """pytest==8.2.1
aiohttp==3.10.0
requests-cache==1.2.1
fiftyone==0.23.8
""",
            encoding="utf-8",
        )
        files = (
            "tests/test_a.py",
            "tests/test_b.py",
            "tests/test_c.py",
        )
        project = profile(
            ProjectType.LIBRARY,
            project_command("pytest", CommandPurpose.TEST, "README.md"),
            dependency_files=("requirements-dev.txt",),
            package_managers=("pip",),
            metadata={"test_files": files, "safe_test_files": files},
        )

        result = TestabilityVerifier(
            self.runtime,
            config=VerificationConfig(max_test_files_per_slice=2),
        ).verify(build_context(project, self.workspace))

        executed = self.runtime.commands[0].display
        self.assertTrue(result.passed)
        self.assertTrue(result.metadata["dependency_plan_applied"])
        self.assertEqual(result.metadata["dependency_plan_mode"], "minimal-slice")
        self.assertEqual(
            result.metadata["dependency_selected_requirements"],
            ("pytest==8.2.1", "aiohttp==3.10.0", "requests-cache==1.2.1"),
        )
        self.assertNotIn("requirements-dev.txt", executed)
        self.assertNotIn("fiftyone", executed)
        self.assertIn("aiohttp==3.10.0", executed)
        self.assertIn("requests-cache==1.2.1", executed)

    def test_testability_uses_native_uv_and_pdm_group_selection(self) -> None:
        command = project_command("pytest -q", CommandPurpose.TEST, "pyproject.toml")
        uv_project = profile(
            ProjectType.LIBRARY,
            command,
            package_managers=("uv",),
            metadata={
                "test_dependency_extras": ("testing",),
                "test_dependency_manager_groups": ("test",),
            },
        )
        TestabilityVerifier(self.runtime).verify(build_context(uv_project, self.workspace))
        uv_command = self.runtime.commands[-1].display
        self.assertIn("uv sync --frozen --inexact --no-default-groups", uv_command)
        self.assertIn("--group test", uv_command)
        self.assertIn("--extra testing", uv_command)

        pdm_project = profile(
            ProjectType.LIBRARY,
            command,
            dependency_files=("pdm.lock", "pyproject.toml"),
            package_managers=("pdm",),
            metadata={
                "test_dependency_extras": ("testing",),
                "test_dependency_manager_groups": ("test",),
            },
        )
        TestabilityVerifier(self.runtime).verify(build_context(pdm_project, self.workspace))
        pdm_command = self.runtime.commands[-1].display
        self.assertIn("pdm sync --no-editable -G test -G testing", pdm_command)

        unlocked_pdm_project = profile(
            ProjectType.LIBRARY,
            command,
            dependency_files=("pyproject.toml",),
            package_managers=("pdm",),
            metadata={"test_dependency_manager_groups": ("test",)},
        )
        TestabilityVerifier(self.runtime).verify(
            build_context(unlocked_pdm_project, self.workspace)
        )
        unlocked_pdm_command = self.runtime.commands[-1].display
        self.assertIn("pdm install --no-editable -G test", unlocked_pdm_command)
        self.assertNotIn("pdm sync", unlocked_pdm_command)

    def test_installability_records_all_five_checks(self) -> None:
        project = profile(
            ProjectType.SCRIPT,
            dependency_files=("requirements.txt",),
            dependency_names=("requests",),
        )
        result = InstallabilityVerifier(self.runtime).verify(
            build_context(
                project, self.workspace, setup="python -m pip install -r requirements.txt"
            )
        )
        self.assertTrue(result.passed)
        self.assertEqual(
            [check.name for check in result.checks],
            [
                "build",
                "dependency-installation",
                "target-artifact",
                "image",
                "installation-health",
            ],
        )

    def test_python_installability_runs_offline_dependency_health_probe(self) -> None:
        project = profile(
            ProjectType.LIBRARY,
            dependency_files=("pyproject.toml",),
            package_managers=("pip",),
        )

        result = InstallabilityVerifier(self.runtime).verify(
            build_context(project, self.workspace, setup="python -m pip install .")
        )

        self.assertTrue(result.passed)
        self.assertIn("-m pip check", self.runtime.commands[-1].display)
        health = next(check for check in result.checks if check.name == "installation-health")
        self.assertEqual(health.metadata["probe_type"], "python-pip-check")
        self.assertEqual(health.metadata["evidence_mode"], "pip-check")

    def test_installability_fails_when_dependency_health_probe_fails(self) -> None:
        project = profile(
            ProjectType.LIBRARY,
            dependency_files=("pyproject.toml",),
            package_managers=("pip",),
        )
        probe = InstallabilityVerifier._installation_health_probe(
            build_context(project, self.workspace)
        )[0]
        self.runtime.exit_code_by_command[probe] = 1
        self.runtime.output_by_command[probe] = "sample requires missing-package"

        result = InstallabilityVerifier(self.runtime).verify(
            build_context(project, self.workspace, setup="python -m pip install .")
        )

        self.assertEqual(result.status, VerificationStatus.FAILED)
        health = next(check for check in result.checks if check.name == "installation-health")
        self.assertEqual(health.status, VerificationStatus.FAILED)

    def test_python_installability_scopes_retained_tool_conflicts(self) -> None:
        project = profile(
            ProjectType.LIBRARY,
            dependency_files=("poetry.lock", "pyproject.toml"),
            package_managers=("poetry",),
        )
        context = build_context(
            project,
            self.workspace,
            setup="python -m pip install poetry && poetry install --only main",
        )
        probe = InstallabilityVerifier._installation_health_probe(context)[0]
        self.assertIn("grep -Eiv '^(poetry)", probe)
        self.runtime.output_by_command[probe] = (
            "poetry 1.8.5 requires pexpect, which is not installed.\n"
            "DPRAUTO_INSTALL_HEALTH_MODE=pip-check-project-clean-tool-conflicts\n"
            "DPRAUTO_INSTALL_HEALTH_OK\n"
        )

        result = InstallabilityVerifier(self.runtime).verify(context)

        self.assertTrue(result.passed)
        health = next(check for check in result.checks if check.name == "installation-health")
        self.assertEqual(health.status, VerificationStatus.SKIPPED)
        self.assertEqual(
            health.metadata["evidence_mode"],
            "pip-check-project-clean-tool-conflicts",
        )

    def test_python_project_does_not_ignore_its_own_tool_named_distribution(self) -> None:
        project = profile(
            ProjectType.LIBRARY,
            dependency_files=("poetry.lock", "pyproject.toml"),
            package_managers=("poetry",),
            metadata={"project_name": "poetry"},
        )

        probe = InstallabilityVerifier._installation_health_probe(
            build_context(project, self.workspace)
        )[0]

        self.assertNotIn("grep -Eiv", probe)

    def test_native_installability_checks_dynamic_links_without_python(self) -> None:
        project = replace(
            profile(ProjectType.LIBRARY),
            languages=("C++",),
            package_managers=("cmake",),
        )
        command = "apt-get update && apt-get install -y cmake g++"

        result = InstallabilityVerifier(self.runtime).verify(
            build_context(project, self.workspace, setup=command)
        )

        self.assertTrue(result.passed)
        self.assertIn("command -v ldd", self.runtime.commands[-1].display)
        self.assertIn("find build", self.runtime.commands[-1].display)
        self.assertNotIn("python", self.runtime.commands[-1].display)
        self.assertEqual(
            result.metadata["installation_health_probe"],
            "native-dynamic-link-check",
        )
        self.assertEqual(result.metadata["checked_dynamic_artifacts"], 1)

    def test_jvm_installability_checks_runtime_load(self) -> None:
        project = replace(
            profile(ProjectType.LIBRARY),
            languages=("Java",),
            package_managers=("maven",),
            dependency_files=("pom.xml",),
        )
        command = "mvn -B -DskipTests package"

        result = InstallabilityVerifier(self.runtime).verify(
            build_context(
                project,
                self.workspace,
                setup=command,
                plan_metadata={"dependency_installation_commands": (command,)},
            )
        )

        self.assertTrue(result.passed)
        self.assertIn("java -version", self.runtime.commands[-1].display)
        self.assertEqual(
            result.metadata["installation_health_probe"],
            "jvm-runtime-load",
        )

    def test_native_installability_marks_no_dynamic_artifacts_as_limited_evidence(self) -> None:
        project = replace(
            profile(ProjectType.LIBRARY),
            languages=("C++",),
            package_managers=("cmake",),
        )
        context = build_context(
            project,
            self.workspace,
            setup="apt-get update && apt-get install -y cmake g++",
        )
        probe = InstallabilityVerifier._installation_health_probe(context)[0]
        self.runtime.output_by_command[probe] = (
            "DPRAUTO_NATIVE_DYNAMIC_COUNT=0\n"
            "DPRAUTO_INSTALL_HEALTH_MODE=no-dynamic-artifacts\n"
            "DPRAUTO_INSTALL_HEALTH_OK\n"
        )

        result = InstallabilityVerifier(self.runtime).verify(context)

        self.assertTrue(result.passed)
        health = next(check for check in result.checks if check.name == "installation-health")
        self.assertEqual(health.status, VerificationStatus.SKIPPED)
        self.assertEqual(health.metadata["checked_dynamic_artifacts"], 0)

    def test_testability_executes_project_tests_but_skips_smoke_only(self) -> None:
        smoke = profile(
            ProjectType.CLI,
            project_command("sample --help", CommandPurpose.TEST, "README.md"),
        )
        skipped = TestabilityVerifier(self.runtime).verify(build_context(smoke, self.workspace))
        self.assertEqual(skipped.status, VerificationStatus.SKIPPED)
        self.assertFalse(self.runtime.commands)

        tested = profile(
            ProjectType.CLI,
            project_command("python -m pytest -q", CommandPurpose.TEST, ".github/workflows/ci.yml"),
        )
        passed = TestabilityVerifier(self.runtime).verify(build_context(tested, self.workspace))
        self.assertTrue(passed.passed)
        self.assertEqual(passed.metadata["command_kind"], "project-test")

    def test_web_requires_process_port_and_http(self) -> None:
        project = profile(
            ProjectType.WEB,
            project_command("python app.py --port 8000", CommandPurpose.RUN, "README.md"),
        )
        verifier = RunnabilityVerifier(self.runtime)
        passed = verifier.verify(build_context(project, self.workspace))
        self.assertTrue(passed.passed)
        self.assertEqual(
            [check.name for check in passed.checks], ["web-process", "web-port", "web-http"]
        )
        self.assertEqual(passed.metadata["runtime_contract"], "service-health")
        self.assertEqual(passed.metadata["runtime_evidence_strength"], "strong")
        self.assertTrue(passed.metadata["runtime_semantically_proven"])

        self.runtime.web = WebProbe(True, True, False, 8000, 43210, None)
        failed = verifier.verify(build_context(project, self.workspace))
        self.assertEqual(failed.status, VerificationStatus.FAILED)

        self.runtime.web = WebProbe(True, True, True, 8000, 43210, 500)
        server_error = verifier.verify(build_context(project, self.workspace))
        self.assertEqual(server_error.status, VerificationStatus.FAILED)
        self.assertIn("server-error", server_error.checks[-1].summary)

    def test_cli_and_script_require_observable_behavior(self) -> None:
        verifier = RunnabilityVerifier(self.runtime)
        cli = profile(ProjectType.CLI, project_command("sample", CommandPurpose.RUN, "setup.py"))
        self.assertTrue(verifier.verify(build_context(cli, self.workspace)).passed)
        script = profile(
            ProjectType.SCRIPT, project_command("python app.py", CommandPurpose.RUN, "README.md")
        )
        self.runtime.output = ""
        self.assertEqual(
            verifier.verify(build_context(script, self.workspace)).status,
            VerificationStatus.FAILED,
        )
        self.runtime.filesystem_changes = ("A /workspace/result.txt",)
        self.assertTrue(verifier.verify(build_context(script, self.workspace)).passed)

    def test_cli_empty_output_is_retried_with_help(self) -> None:
        verifier = RunnabilityVerifier(self.runtime)
        cli = profile(ProjectType.CLI, project_command("sample", CommandPurpose.RUN, "setup.py"))
        self.runtime.output = ""
        self.runtime.output_by_command["sample --help"] = "usage: sample [options]"

        result = verifier.verify(build_context(cli, self.workspace))

        self.assertTrue(result.passed)
        self.assertEqual(
            [command.display for command in self.runtime.commands], ["sample", "sample --help"]
        )
        self.assertTrue(result.metadata["empty_output_help_fallback"])
        self.assertEqual(result.metadata["runtime_outcome_category"], "cli-invocable")
        self.assertEqual(result.metadata["runtime_evidence_strength"], "moderate")
        self.assertTrue(result.metadata["runtime_semantically_proven"])

    def test_cli_nonzero_usage_exit_is_retried_with_help(self) -> None:
        verifier = RunnabilityVerifier(self.runtime)
        cli = profile(ProjectType.CLI, project_command("sample", CommandPurpose.RUN, "setup.py"))
        self.runtime.exit_code_by_command["sample"] = 2
        self.runtime.output_by_command["sample"] = "Usage: sample [OPTIONS] COMMAND"
        self.runtime.output_by_command["sample --help"] = "Usage: sample [OPTIONS] COMMAND"

        result = verifier.verify(build_context(cli, self.workspace))

        self.assertTrue(result.passed)
        self.assertEqual(
            [command.display for command in self.runtime.commands],
            ["sample", "sample --help"],
        )
        self.assertTrue(result.metadata["help_fallback_used"])
        self.assertEqual(result.metadata["help_fallback_reason"], "initial-command-failed")

    def test_inferred_bare_cli_uses_help_before_it_can_block(self) -> None:
        verifier = RunnabilityVerifier(self.runtime)
        cli = profile(
            ProjectType.CLI,
            project_command(
                "python -m sample",
                CommandPurpose.RUN,
                "inferred:__main__.py",
            ),
        )

        result = verifier.verify(build_context(cli, self.workspace))

        self.assertTrue(result.passed)
        self.assertEqual([item.display for item in self.runtime.commands], ["python -m sample --help"])
        self.assertTrue(result.metadata["safe_probe_normalized"])
        self.assertEqual(result.metadata["selected_command"], "python -m sample")

    def test_unavailable_development_wrapper_is_not_selected_for_runtime(self) -> None:
        verifier = RunnabilityVerifier(self.runtime)
        cli = profile(
            ProjectType.CLI,
            project_command(
                "pipenv run sample --version",
                CommandPurpose.RUN,
                "README.md",
                0.95,
            ),
            project_command(
                "python -m sample",
                CommandPurpose.RUN,
                "inferred:__main__.py",
                0.85,
            ),
            package_managers=("pip",),
        )

        result = verifier.verify(build_context(cli, self.workspace))

        self.assertTrue(result.passed)
        self.assertEqual(self.runtime.commands[0].display, "python -m sample --help")

    def test_jvm_library_uses_strategy_owned_artifact_probe(self) -> None:
        project = replace(
            profile(ProjectType.LIBRARY),
            languages=("Java",),
            package_managers=("maven",),
        )
        probe = "jar tf /workspace/target/sample.jar"
        self.runtime.output_by_command[probe] = "DPRAUTO_JVM_ARTIFACT_OK\nDPRAUTO_API_COUNT=14\n"
        context = build_context(
            project,
            self.workspace,
            strategy="jvm-template",
            plan_metadata={
                "runtime_probe_command": probe,
                "runtime_probe_marker": "DPRAUTO_JVM_ARTIFACT_OK",
                "runtime_probe_type": "jvm-jar-classes",
            },
        )

        result = RunnabilityVerifier(self.runtime).verify(context)

        self.assertTrue(result.passed)
        self.assertEqual(result.metadata["artifact_count"], 14)
        self.assertEqual(self.runtime.commands[0].display, probe)
        self.assertEqual(result.metadata["runtime_outcome_category"], "compiled-artifact-present")
        self.assertEqual(result.metadata["runtime_evidence_strength"], "limited")
        self.assertFalse(result.metadata["runtime_semantically_proven"])

    def test_native_library_requires_artifact_or_passing_project_tests(self) -> None:
        project = replace(
            profile(ProjectType.LIBRARY),
            languages=("C++",),
            package_managers=("cmake",),
        )
        probe = "test -d build"
        self.runtime.output_by_command[probe] = (
            "DPRAUTO_NATIVE_BUILD_OK\nDPRAUTO_ARTIFACT_COUNT=0\n"
        )
        context = build_context(
            project,
            self.workspace,
            strategy="native-template",
            plan_metadata={
                "runtime_probe_command": probe,
                "runtime_probe_marker": "DPRAUTO_NATIVE_BUILD_OK",
                "runtime_probe_type": "native-build-artifacts",
            },
        )

        failed = RunnabilityVerifier(self.runtime).verify(context)
        self.assertEqual(failed.status, VerificationStatus.FAILED)

        passed_tests = VerificationResult(
            "native-tests",
            VerificationLevel.TESTABILITY,
            VerificationStatus.PASSED,
            summary="native project tests passed",
        )
        passed = RunnabilityVerifier(self.runtime).verify(
            replace(context, prior_results=(passed_tests,))
        )
        self.assertTrue(passed.passed)
        self.assertTrue(passed.metadata["project_tests_passed"])
        self.assertEqual(passed.metadata["runtime_evidence_strength"], "moderate")
        self.assertTrue(passed.metadata["runtime_semantically_proven"])

        limited_tests = replace(
            passed_tests,
            metadata={
                "outcome_category": "tests-passed",
                "test_evidence_strength": "limited",
            },
        )
        limited = RunnabilityVerifier(self.runtime).verify(
            replace(context, prior_results=(limited_tests,))
        )
        self.assertTrue(limited.passed)
        self.assertEqual(limited.metadata["runtime_evidence_strength"], "limited")
        self.assertFalse(limited.metadata["runtime_semantically_proven"])

    def test_compiled_library_rejects_zero_test_false_success_as_runtime_evidence(self) -> None:
        project = replace(
            profile(ProjectType.LIBRARY),
            languages=("C++",),
            package_managers=("cmake",),
        )
        probe = "test -d build"
        self.runtime.output_by_command[probe] = (
            "DPRAUTO_NATIVE_BUILD_OK\nDPRAUTO_ARTIFACT_COUNT=2\n"
        )
        false_success = VerificationResult(
            "zero-tests",
            VerificationLevel.TESTABILITY,
            VerificationStatus.PASSED,
            summary="ctest returned zero",
            metadata={
                "outcome_category": "no-tests-collected",
                "test_evidence_strength": "limited",
            },
        )
        context = build_context(
            project,
            self.workspace,
            strategy="native-template",
            plan_metadata={
                "runtime_probe_command": probe,
                "runtime_probe_marker": "DPRAUTO_NATIVE_BUILD_OK",
                "runtime_probe_type": "native-build-artifacts",
            },
        )

        result = RunnabilityVerifier(self.runtime).verify(
            replace(context, prior_results=(false_success,))
        )

        self.assertTrue(result.passed)
        self.assertFalse(result.metadata["project_tests_passed"])
        self.assertEqual(result.metadata["runtime_evidence_strength"], "limited")
        self.assertFalse(result.metadata["runtime_semantically_proven"])

    def test_repaired_native_docker_plan_recovers_image_artifact_probe(self) -> None:
        project = replace(
            profile(ProjectType.LIBRARY),
            languages=("C++",),
            package_managers=("cmake",),
        )
        probe = (
            "test -d build && count=$(find build -type f -name '*.so' | wc -l) "
            '&& test "$count" -gt 0 && echo DPRAUTO_NATIVE_BUILD_OK '
            "&& echo DPRAUTO_ARTIFACT_COUNT=$count"
        )
        self.runtime.default_command = ("/bin/sh", "-lc", probe)
        self.runtime.output_by_command[probe] = (
            "DPRAUTO_NATIVE_BUILD_OK\nDPRAUTO_ARTIFACT_COUNT=3\n"
        )
        context = build_context(project, self.workspace, strategy="docker")

        result = RunnabilityVerifier(self.runtime).verify(context)

        self.assertTrue(result.passed)
        self.assertEqual(result.metadata["artifact_count"], 3)
        self.assertEqual(
            result.metadata["runtime_probe_type"],
            "native-image-default-artifacts",
        )
        self.assertEqual([command.display for command in self.runtime.commands], [probe])
        self.assertEqual(result.metadata["runtime_evidence_strength"], "limited")
        self.assertFalse(result.metadata["runtime_semantically_proven"])

    def test_repaired_jvm_docker_plan_recovers_image_artifact_probe(self) -> None:
        project = replace(
            profile(ProjectType.LIBRARY),
            languages=("Java",),
            package_managers=("maven",),
        )
        probe = (
            "count=$(find . -path '*/target/*.jar' | wc -l) "
            '&& test "$count" -gt 0 && echo DPRAUTO_JVM_ARTIFACT_OK '
            "&& echo DPRAUTO_API_COUNT=$count"
        )
        self.runtime.default_command = ("/bin/sh", "-lc", probe)
        self.runtime.output_by_command[probe] = (
            "DPRAUTO_JVM_ARTIFACT_OK\nDPRAUTO_API_COUNT=2\n"
        )
        context = build_context(project, self.workspace, strategy="docker")

        result = RunnabilityVerifier(self.runtime).verify(context)

        self.assertTrue(result.passed)
        self.assertEqual(result.metadata["artifact_count"], 2)
        self.assertEqual(
            result.metadata["runtime_probe_type"],
            "jvm-image-default-artifacts",
        )
        self.assertEqual([command.display for command in self.runtime.commands], [probe])
        self.assertEqual(result.metadata["runtime_evidence_strength"], "limited")
        self.assertFalse(result.metadata["runtime_semantically_proven"])

    def test_non_python_library_never_falls_back_to_python_import(self) -> None:
        project = replace(
            profile(ProjectType.LIBRARY),
            languages=("C++",),
            package_managers=("cmake",),
        )

        result = RunnabilityVerifier(self.runtime).verify(
            build_context(project, self.workspace, strategy="docker")
        )

        self.assertEqual(result.status, VerificationStatus.ERROR)
        self.assertIn("compiled-library runtime probe", result.summary)
        self.assertFalse(self.runtime.commands)

    def test_library_import_alone_is_not_enough_without_api_or_tests(self) -> None:
        project = profile(ProjectType.LIBRARY, import_modules=("sample",))
        verifier = RunnabilityVerifier(self.runtime)
        self.assertTrue(verifier.verify(build_context(project, self.workspace)).passed)
        self.runtime.library_api_count = 0
        result = verifier.verify(build_context(project, self.workspace))
        self.assertEqual(result.status, VerificationStatus.FAILED)
        self.assertTrue(
            next(check for check in result.checks if check.name == "library-import").passed
        )
        self.assertEqual(
            next(check for check in result.checks if check.name == "library-api-or-tests").status,
            VerificationStatus.FAILED,
        )

    def test_library_import_is_accepted_when_tests_are_explicitly_unavailable(self) -> None:
        project = profile(ProjectType.LIBRARY, import_modules=("sample",))
        self.runtime.library_api_count = 0
        skipped_tests = VerificationResult(
            "testability-policy-skip",
            VerificationLevel.TESTABILITY,
            VerificationStatus.SKIPPED,
            summary="tests require a secret",
            metadata={"skip_reason": "required-secret-environment"},
        )
        context = replace(
            build_context(project, self.workspace),
            prior_results=(skipped_tests,),
        )

        result = RunnabilityVerifier(self.runtime).verify(context)

        self.assertTrue(result.passed)
        policy_check = next(
            check for check in result.checks if check.name == "library-api-or-tests"
        )
        self.assertEqual(policy_check.status, VerificationStatus.SKIPPED)
        self.assertEqual(
            policy_check.metadata["tests_unavailable_reason"],
            "required-secret-environment",
        )
        self.assertEqual(result.metadata["runtime_evidence_strength"], "limited")
        self.assertFalse(result.metadata["runtime_semantically_proven"])

    def test_service_persists_each_layer_and_aggregate(self) -> None:
        storage = LocalArtifactStorage(self.workspace / "artifacts")
        project = profile(
            ProjectType.SCRIPT,
            project_command("python app.py", CommandPurpose.RUN, "README.md"),
        )
        context = build_context(project, self.workspace)
        service = LayeredVerificationService(
            (
                InstallabilityVerifier(self.runtime),
                TestabilityVerifier(self.runtime),
                RunnabilityVerifier(self.runtime),
            ),
            storage,
        )
        report = service.verify(
            project,
            context.build_result,
            self.workspace,
            build_plan=context.build_plan,
        )
        self.assertTrue(report.succeeded)
        self.assertEqual(len(report.results), 3)
        self.assertEqual(len(report.artifacts), 4)
        self.assertEqual(
            tuple(result.level for result in report.results),
            (
                VerificationLevel.INSTALLABILITY,
                VerificationLevel.TESTABILITY,
                VerificationLevel.RUNNABILITY,
            ),
        )
        for artifact in report.artifacts:
            self.assertTrue(storage.exists(artifact))


if __name__ == "__main__":
    unittest.main()

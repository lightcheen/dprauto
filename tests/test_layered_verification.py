import tempfile
import unittest
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

    def inspect_image(self, image_reference: str) -> ImageInspection:
        return ImageInspection(
            bool(image_reference),
            "sha256:image" if image_reference else "",
            "/workspace",
            ("python", "app.py"),
            (8000,),
        )

    def run_image(self, image_reference, command, *, timeout_seconds):
        actual = command or CommandSpec(("image-default",), purpose=CommandPurpose.RUN)
        self.commands.append(actual)
        output = self.output_by_command.get(actual.display, self.output)
        if "DPRAUTO_IMPORT_OK" in actual.display:
            output = f"DPRAUTO_IMPORT_OK\nDPRAUTO_API_COUNT={self.library_api_count}\n"
        result = CommandResult(
            actual,
            self.exit_code,
            stdout=ArtifactRef("fake/run.log"),
            timed_out=False,
        )
        return ContainerExecution(result, output, self.filesystem_changes)

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
):
    metadata = {"image_reference": "example:latest"}
    if runtime_base_image:
        metadata["runtime_base_image"] = runtime_base_image
    plan = BuildPlan(
        "plan",
        project.project_id,
        "template",
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
                    project_command("python -m sample --help", CommandPurpose.TEST, ".github/workflows/test.yml", 1.0),
                    project_command("python -m pytest -q", CommandPurpose.TEST, "README.md", 0.9),
                    project_command("python -m unittest discover", CommandPurpose.TEST, ".github/workflows/test.yml", 0.8),
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
                    project_command("tox -e ${{ matrix.env }}", CommandPurpose.TEST, ".github/workflows/test.yml", 1.0),
                    project_command("tox -e docs", CommandPurpose.TEST, ".github/workflows/test.yml", 0.99),
                    project_command("ruff check .", CommandPurpose.TEST, ".github/workflows/test.yml", 0.98),
                    project_command("python -m pytest -q", CommandPurpose.TEST, "inferred:test-layout", 0.7),
                ),
            )
        )

        self.assertEqual(selected.command.display, "python -m pytest -q")

    def test_bare_tox_is_narrowed_to_default_environment(self) -> None:
        selected = TestCommandSelector().select(
            ProjectProfile(
                "project",
                SourceReference("fixture://project"),
                commands=(
                    project_command("tox", CommandPurpose.TEST, "tox.ini", 0.9),
                ),
                metadata={"default_tox_env": "py311"},
            )
        )

        self.assertEqual(selected.command.display, "tox -e py311")

    def test_bare_nox_is_narrowed_to_default_session(self) -> None:
        selected = TestCommandSelector().select(
            ProjectProfile(
                "project",
                SourceReference("fixture://project"),
                commands=(
                    project_command("nox", CommandPurpose.TEST, "noxfile.py", 0.9),
                ),
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
                commands=(
                    project_command("nox", CommandPurpose.TEST, "noxfile.py"),
                ),
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
                check
                for check in result.checks
                if check.name == "dependency-installation"
            ).status,
            VerificationStatus.FAILED,
        )

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
                )
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

        result = TestabilityVerifier(self.runtime).verify(
            build_context(project, self.workspace)
        )

        self.assertEqual(result.status, VerificationStatus.PASSED)
        self.assertIn(
            "python -m pip install pytest==8.3.5 && python -m pytest -q",
            self.runtime.commands[0].display,
        )
        self.assertEqual(result.metadata["original_command"], "python -m pytest -q")

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

        result = TestabilityVerifier(self.runtime).verify(
            build_context(project, self.workspace)
        )

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

        result = TestabilityVerifier(self.runtime).verify(
            build_context(project, self.workspace)
        )

        executed = self.runtime.commands[0].display
        self.assertIn(
            "python -m pip install '.[tests]' pytest==8.3.5 pytest-xdist==3.6.1",
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

        result = TestabilityVerifier(self.runtime).verify(
            build_context(project, self.workspace)
        )

        self.assertNotIn("pytest-xdist", self.runtime.commands[0].display)
        self.assertNotIn("--numprocesses", self.runtime.commands[0].display)
        self.assertEqual(result.metadata["parallel_workers"], 0)

    def test_testability_selects_one_native_dependency_source_after_runtime_build(self) -> None:
        command = project_command(
            "python -m pytest -q", CommandPurpose.TEST, "pyproject.toml"
        )
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

        result = TestabilityVerifier(self.runtime).verify(
            build_context(project, self.workspace)
        )

        executed = self.runtime.commands[0].display
        self.assertTrue(result.passed)
        self.assertNotIn("requirements-test.txt", executed)
        self.assertIn("poetry install --only main,test,dev", executed)
        self.assertIn("--extras testing", executed)
        self.assertIn("pip install pytest==8.3.5", executed)
        self.assertLess(executed.index("poetry install"), executed.index("pip install pytest"))

    def test_testability_prefers_pip_extra_over_broad_dev_requirements(self) -> None:
        project = profile(
            ProjectType.LIBRARY,
            project_command("pytest", CommandPurpose.TEST, "README.rst"),
            dependency_files=("requirements-dev.txt", "pyproject.toml"),
            package_managers=("pip",),
            metadata={"test_dependency_extras": ("tests",)},
        )

        result = TestabilityVerifier(self.runtime).verify(
            build_context(project, self.workspace)
        )

        self.assertTrue(result.passed)
        executed = self.runtime.commands[0].display
        self.assertIn("python -m pip install '.[tests]' pytest==8.3.5", executed)
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
            "pip install -r requirements-testing.in pytest==8.3.5",
            executed,
        )
        self.assertNotIn("requirements-dev.txt", executed)
        self.assertNotIn("requirements-tox.txt", executed)

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
            package_managers=("pdm",),
            metadata={
                "test_dependency_extras": ("testing",),
                "test_dependency_manager_groups": ("test",),
            },
        )
        TestabilityVerifier(self.runtime).verify(build_context(pdm_project, self.workspace))
        pdm_command = self.runtime.commands[-1].display
        self.assertIn("pdm sync --no-editable -G test -G testing", pdm_command)

    def test_installability_records_all_four_checks(self) -> None:
        project = profile(
            ProjectType.SCRIPT,
            dependency_files=("requirements.txt",),
            dependency_names=("requests",),
        )
        result = InstallabilityVerifier(self.runtime).verify(
            build_context(project, self.workspace, setup="python -m pip install -r requirements.txt")
        )
        self.assertTrue(result.passed)
        self.assertEqual(
            [check.name for check in result.checks],
            ["build", "dependency-installation", "target-artifact", "image"],
        )

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
        self.assertEqual([check.name for check in passed.checks], ["web-process", "web-port", "web-http"])

        self.runtime.web = WebProbe(True, True, False, 8000, 43210, None)
        failed = verifier.verify(build_context(project, self.workspace))
        self.assertEqual(failed.status, VerificationStatus.FAILED)

    def test_cli_and_script_require_observable_behavior(self) -> None:
        verifier = RunnabilityVerifier(self.runtime)
        cli = profile(ProjectType.CLI, project_command("sample", CommandPurpose.RUN, "setup.py"))
        self.assertTrue(verifier.verify(build_context(cli, self.workspace)).passed)
        script = profile(ProjectType.SCRIPT, project_command("python app.py", CommandPurpose.RUN, "README.md"))
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
        self.assertEqual([command.display for command in self.runtime.commands], ["sample", "sample --help"])
        self.assertTrue(result.metadata["empty_output_help_fallback"])

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

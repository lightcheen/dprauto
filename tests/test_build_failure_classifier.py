import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from dprauto.adapters.failure import FailureClassifierChain, RuleBasedBuildFailureClassifier
from dprauto.adapters.storage import LocalArtifactStorage
from dprauto.domain.enums import (
    BuildFailureKind,
    BuildStage,
    BuildStatus,
    CommandPurpose,
    FailureCategory,
)
from dprauto.domain.models import (
    BuildPlan,
    BuildResult,
    BuildStep,
    CommandResult,
    CommandSpec,
    ProjectProfile,
    SourceReference,
)


FIXTURES = Path(__file__).parent / "fixtures" / "failure_logs"


class RecordingFallback:
    def __init__(self) -> None:
        self.calls = []

    def refine(self, profile, plan, preliminary):
        self.calls.append(preliminary)
        return replace(
            preliminary,
            category=FailureCategory.BUILD_COMMAND,
            message="LLM fallback classification",
            possible_cause="fallback result",
            confidence=0.5,
        )


class BuildFailureClassifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.storage = LocalArtifactStorage(Path(self.temporary.name))
        self.profile = ProjectProfile(
            "project",
            SourceReference("source"),
            languages=("Python",),
            runtime_constraints={"python": ">=3.10"},
            package_managers=("pip",),
        )
        self.classifier = RuleBasedBuildFailureClassifier(self.storage)
        self.sequence = 0

    def classify_fixture(
        self,
        fixture: str,
        stage: BuildStage,
        purpose: CommandPurpose,
        command_text: str,
    ):
        self.sequence += 1
        command = CommandSpec((command_text,), purpose=purpose, shell=True)
        plan = BuildPlan(
            f"plan-{self.sequence}",
            "project",
            "template",
            (BuildStep("failed-step", stage, command),),
            metadata={"base_image": "python:3.11-slim"},
        )
        log = self.storage.save(
            f"logs/{self.sequence}.log",
            (FIXTURES / fixture).read_bytes(),
            media_type="text/plain",
        )
        now = datetime.now(timezone.utc)
        command_result = CommandResult(command, 1, stdout=log, duration_seconds=0.1)
        result = BuildResult(
            f"attempt-{self.sequence}",
            plan.plan_id,
            BuildStatus.FAILED,
            now,
            now,
            exit_code=1,
            failed_stage=stage,
            logs=(log,),
            command_results=(command_result,),
        )
        return plan, result, self.classifier.classify(self.profile, plan, result)

    def classify_text(
        self,
        text: str,
        *,
        stage: BuildStage = BuildStage.BUILD,
        purpose: CommandPurpose = CommandPurpose.BUILD,
        timed_out: bool = False,
    ):
        self.sequence += 1
        command = CommandSpec(("docker build .",), purpose=purpose, shell=True)
        plan = BuildPlan(
            f"text-plan-{self.sequence}",
            "project",
            "template",
            (BuildStep("failed-step", stage, command),),
        )
        log = self.storage.save(f"logs/text-{self.sequence}.log", text.encode())
        now = datetime.now(timezone.utc)
        result = BuildResult(
            f"text-attempt-{self.sequence}",
            plan.plan_id,
            BuildStatus.TIMED_OUT if timed_out else BuildStatus.FAILED,
            now,
            now,
            exit_code=1,
            failed_stage=stage,
            logs=(log,),
            command_results=(
                CommandResult(
                    command,
                    None if timed_out else 1,
                    stdout=log,
                    timed_out=timed_out,
                ),
            ),
        )
        return self.classifier.classify(self.profile, plan, result)

    def test_real_log_fixtures_cover_required_categories(self) -> None:
        cases = (
            (
                "network_ursina.log",
                BuildStage.DEPENDENCY_INSTALLATION,
                CommandPurpose.INSTALL,
                "poetry install",
                FailureCategory.NETWORK,
            ),
            (
                "docker_lavague.log",
                BuildStage.BUILD,
                CommandPurpose.BUILD,
                "pack build app",
                FailureCategory.DOCKER,
            ),
            (
                "system_dependency_yubikey.log",
                BuildStage.DEPENDENCY_INSTALLATION,
                CommandPurpose.INSTALL,
                "poetry install",
                FailureCategory.SYSTEM_DEPENDENCY,
            ),
            (
                "python_version_oadoi.log",
                BuildStage.DEPENDENCY_INSTALLATION,
                CommandPurpose.INSTALL,
                "pip install -r requirements.txt",
                FailureCategory.RUNTIME_VERSION,
            ),
            (
                "python_dependency.log",
                BuildStage.DEPENDENCY_INSTALLATION,
                CommandPurpose.INSTALL,
                "python import_check.py",
                FailureCategory.PYTHON_DEPENDENCY,
            ),
            (
                "dependency_conflict_pennylane.log",
                BuildStage.DEPENDENCY_INSTALLATION,
                CommandPurpose.INSTALL,
                "pip install -r requirements.txt",
                FailureCategory.DEPENDENCY_CONFLICT,
            ),
            (
                "build_tool_cpython.log",
                BuildStage.DETECTION,
                CommandPurpose.BUILD,
                "pack build cpython",
                FailureCategory.BUILD_TOOL,
            ),
            (
                "compilation_openinterpreter.log",
                BuildStage.BUILD,
                CommandPurpose.BUILD,
                "poetry install",
                FailureCategory.COMPILATION,
            ),
            (
                "test_tool_openlane.log",
                BuildStage.TEST,
                CommandPurpose.TEST,
                "python -m pyright .",
                FailureCategory.BUILD_TOOL,
            ),
            (
                "test_failure.log",
                BuildStage.TEST,
                CommandPurpose.TEST,
                "python -m unittest",
                FailureCategory.TEST,
            ),
            (
                "run_failure.log",
                BuildStage.STARTUP,
                CommandPurpose.RUN,
                "python app.py",
                FailureCategory.RUN,
            ),
            (
                "external_service.log",
                BuildStage.STARTUP,
                CommandPurpose.RUN,
                "python connect_to_local_service.py",
                FailureCategory.EXTERNAL_SERVICE,
            ),
            (
                "unknown.log",
                BuildStage.BUILD,
                CommandPurpose.BUILD,
                "custom-build-tool assemble",
                FailureCategory.UNKNOWN,
            ),
        )
        for fixture, stage, purpose, command, category in cases:
            with self.subTest(fixture=fixture):
                _, _, failure = self.classify_fixture(fixture, stage, purpose, command)
                self.assertEqual(failure.category, category)
                self.assertEqual(failure.failure_stage, stage)
                self.assertEqual(failure.failed_command.display, command)
                self.assertTrue(failure.key_log)
                self.assertLessEqual(len(failure.key_log), 3000)
                self.assertEqual(failure.environment["runtime.python"], ">=3.10")
                self.assertEqual(failure.environment["base_image"], "python:3.11-slim")
                self.assertTrue(failure.possible_cause)

    def test_docker_and_network_are_never_project_build_failures(self) -> None:
        cases = (
            ("docker_lavague.log", FailureCategory.DOCKER, BuildFailureKind.DOCKER_INFRASTRUCTURE),
            ("network_ursina.log", FailureCategory.NETWORK, BuildFailureKind.NETWORK),
        )
        for fixture, category, kind in cases:
            with self.subTest(fixture=fixture):
                _, _, failure = self.classify_fixture(
                    fixture,
                    BuildStage.BUILD,
                    CommandPurpose.BUILD,
                    "docker build .",
                )
                self.assertEqual(failure.category, category)
                self.assertEqual(failure.kind, kind)
                self.assertTrue(failure.infrastructure_related)
                self.assertNotEqual(failure.kind, BuildFailureKind.PROJECT_BUILD)

    def test_jvm_environment_failures_have_deterministic_categories(self) -> None:
        cases = (
            (
                "Files with unapproved licenses:\n  /.cnb-benchmark-source-ready",
                FailureCategory.POLICY,
                BuildFailureKind.PROJECT_BUILD,
                False,
            ),
            (
                'Unknown lifecycle phase "/root/.m2". You must specify a valid lifecycle phase.',
                FailureCategory.BUILD_TOOL,
                BuildFailureKind.PROJECT_BUILD,
                False,
            ),
            (
                "Failed to execute goal com.mycila:license-maven-plugin:5.0.0:format: "
                "One of setGitDir or setWorkTree must be called.",
                FailureCategory.POLICY,
                BuildFailureKind.PROJECT_BUILD,
                False,
            ),
            (
                "Failed to execute goal com.rudikershaw.gitbuildhook:"
                "git-build-hook-maven-plugin:3.6.0:install: Could not find or initialise "
                "a local git repository.",
                FailureCategory.POLICY,
                BuildFailureKind.PROJECT_BUILD,
                False,
            ),
            (
                "Downloading https://services.gradle.org/distributions/gradle-8.14-bin.zip\n"
                "Downloading from https://services.gradle.org/distributions/gradle-8.14-bin.zip "
                "failed: timeout (10000ms)",
                FailureCategory.NETWORK,
                BuildFailureKind.NETWORK,
                True,
            ),
            (
                "Cannot find a Java installation on your machine matching: "
                "{languageVersion=8, vendor=Eclipse Temurin, implementation=vendor-specific}",
                FailureCategory.TOOLCHAIN,
                BuildFailureKind.PROJECT_BUILD,
                False,
            ),
            (
                "Dependency requires at least JVM runtime version 11. "
                "This build uses a Java 8 JVM. Run this build using a Java 11 or newer JVM.",
                FailureCategory.RUNTIME_VERSION,
                BuildFailureKind.PROJECT_BUILD,
                False,
            ),
        )
        for text, category, kind, infrastructure_related in cases:
            with self.subTest(category=category):
                failure = self.classify_text(text)
                self.assertEqual(failure.category, category)
                self.assertEqual(failure.kind, kind)
                self.assertEqual(failure.infrastructure_related, infrastructure_related)
                self.assertEqual(failure.confidence, 0.99)

    def test_key_log_is_bounded_around_match_not_full_log(self) -> None:
        noise = "\n".join(f"unrelated line {index}" for index in range(200))
        text = f"{noise}\nCould not resolve host: pypi.org\n{noise}"
        self.sequence += 1
        log = self.storage.save(f"logs/{self.sequence}.log", text.encode())
        command = CommandSpec(("pip", "install", "."), purpose=CommandPurpose.INSTALL)
        plan = BuildPlan(
            "bounded-plan",
            "project",
            "template",
            (BuildStep("install", BuildStage.DEPENDENCY_INSTALLATION, command),),
        )
        now = datetime.now(timezone.utc)
        result = BuildResult(
            "bounded",
            plan.plan_id,
            BuildStatus.FAILED,
            now,
            now,
            exit_code=1,
            failed_stage=BuildStage.DEPENDENCY_INSTALLATION,
            logs=(log,),
        )

        failure = self.classifier.classify(self.profile, plan, result)
        self.assertIn("Could not resolve host", failure.key_log)
        self.assertLess(len(failure.key_log), len(text) // 4)

    def test_last_missing_dependency_beats_earlier_optional_cmake_probes(self) -> None:
        failure = self.classify_text(
            "-- Could NOT find c-ares (missing: C-ARES_LIBRARIES)\n"
            "-- Could NOT find Doxygen (missing: DOXYGEN_EXECUTABLE)\n"
            "CMake Error at FindPackageHandleStandardArgs.cmake:230 (message):\n"
            "  Could NOT find Jsoncpp (missing: JSONCPP_LIBRARIES)\n"
            "-- Configuring incomplete, errors occurred!\n"
        )

        self.assertEqual(failure.category, FailureCategory.SYSTEM_DEPENDENCY)
        self.assertEqual(
            failure.evidence,
            ("Could NOT find Jsoncpp (missing: JSONCPP_LIBRARIES)",),
        )
        self.assertIn("Jsoncpp", failure.key_log)
        self.assertNotIn("c-ares", failure.key_log)

    def test_prompt12_build_failures_are_classified_by_causal_error(self) -> None:
        cases = (
            (
                "ERROR: no match for platform in manifest: not found",
                FailureCategory.DOCKER,
                True,
            ),
            (
                "W: Failed to fetch http://deb.debian.org/debian/InRelease "
                "Temporary failure resolving 'deb.debian.org'",
                FailureCategory.NETWORK,
                True,
            ),
            (
                "ERROR: Cannot find command 'git' - do you have 'git' installed?",
                FailureCategory.SYSTEM_DEPENDENCY,
                False,
            ),
            (
                "pyproject.toml changed significantly since poetry.lock was last generated",
                FailureCategory.DEPENDENCY_CONFLICT,
                False,
            ),
            (
                "LookupError: setuptools-scm was unable to detect version for /workspace",
                FailureCategory.BUILD_TOOL,
                False,
            ),
            (
                'failed to calculate checksum of ref abc: "/app": not found',
                FailureCategory.BUILD_COMMAND,
                False,
            ),
        )
        for text, category, infrastructure in cases:
            with self.subTest(text=text):
                failure = self.classify_text(text)
                self.assertEqual(failure.category, category)
                self.assertEqual(failure.infrastructure_related, infrastructure)

    def test_high_star_build_failures_have_specific_general_categories(self) -> None:
        cases = json.loads((FIXTURES / "high_star30_failure_classes.json").read_text())
        for case in cases:
            with self.subTest(case=case["case"]):
                failure = self.classify_text(case["text"])
                self.assertEqual(failure.category.value, case["category"])

        unpack_failure = self.classify_text(
            "Failed to execute goal maven-dependency-plugin:unpack-dependencies: "
            "Could not find artifact com.example:demo:jar:1.0"
        )
        self.assertNotEqual(unpack_failure.category, FailureCategory.DOCKER)

    def test_download_progress_number_is_not_an_external_service_failure(self) -> None:
        failure = self.classify_text(
            "Downloading pyproject_hooks-1.2.0-py3-none-any.whl (10 kB) at 7.503 MB/s"
        )

        self.assertNotEqual(failure.category, FailureCategory.EXTERNAL_SERVICE)

    def test_unknown_build_log_uses_causal_line_not_buildkit_stack_tail(self) -> None:
        failure = self.classify_text(
            "error: project metadata is invalid\n"
            "ERROR: failed to solve: process exited with code 1\n"
            "github.com/moby/buildkit/solver/edge.go:966\n"
            "runtime.goexit\n"
        )

        self.assertIn("project metadata is invalid", failure.key_log)
        self.assertNotIn("runtime.goexit", failure.key_log)

    def test_fallback_receives_only_unknown_structured_failure(self) -> None:
        fallback = RecordingFallback()
        chain = FailureClassifierChain(self.classifier, fallback)
        plan, result, preliminary = self.classify_fixture(
            "unknown.log",
            BuildStage.BUILD,
            CommandPurpose.BUILD,
            "custom-build-tool assemble",
        )

        refined = chain.classify(self.profile, plan, result)
        self.assertEqual(preliminary.category, FailureCategory.UNKNOWN)
        self.assertEqual(refined.category, FailureCategory.BUILD_COMMAND)
        self.assertEqual(fallback.calls, [preliminary])
        self.assertFalse(hasattr(fallback.calls[0], "logs"))

        network_plan, network_result, _ = self.classify_fixture(
            "network_ursina.log",
            BuildStage.BUILD,
            CommandPurpose.BUILD,
            "docker build .",
        )
        chain.classify(self.profile, network_plan, network_result)
        self.assertEqual(len(fallback.calls), 1)

    def test_success_has_no_failure(self) -> None:
        command = CommandSpec(("docker", "build", "."), purpose=CommandPurpose.BUILD)
        plan = BuildPlan(
            "success-plan",
            "project",
            "template",
            (BuildStep("build", BuildStage.BUILD, command),),
        )
        now = datetime.now(timezone.utc)
        result = BuildResult("ok", plan.plan_id, BuildStatus.SUCCEEDED, now, now, exit_code=0)
        self.assertIsNone(self.classifier.classify(self.profile, plan, result))

    def test_timed_out_build_carries_timeout_profile_and_repair_guidance(self) -> None:
        command = CommandSpec(
            ("python", "-m", "pip", "install", "-r", "requirements.txt"),
            purpose=CommandPurpose.INSTALL,
        )
        plan = BuildPlan(
            "timeout-plan",
            "project",
            "template",
            (BuildStep("install", BuildStage.DEPENDENCY_INSTALLATION, command),),
        )
        log = self.storage.save(
            "logs/timeout.log",
            b"Collecting pandas\nDownloading pandas-2.0.whl\ncommand timed out after 60 seconds\n",
        )
        now = datetime.now(timezone.utc)
        result = BuildResult(
            "timeout-attempt",
            plan.plan_id,
            BuildStatus.TIMED_OUT,
            now,
            now,
            failed_stage=BuildStage.DEPENDENCY_INSTALLATION,
            logs=(log,),
            command_results=(CommandResult(command, None, stdout=log, timed_out=True),),
        )

        failure = self.classifier.classify(self.profile, plan, result)

        self.assertEqual(failure.message, "Build command timed out")
        self.assertIn("timeout_profile=python-package-install", failure.evidence)
        self.assertIn("timeout_activity=dependency-download", failure.evidence)
        self.assertIn(
            "last_active_line=Downloading pandas-2.0.whl",
            failure.evidence,
        )
        self.assertTrue(any("Do not add dependencies" in item for item in failure.suggestions))

    def test_timed_out_system_package_install_is_infrastructure_related(self) -> None:
        command = CommandSpec(
            ("docker", "build", "."),
            purpose=CommandPurpose.BUILD,
            timeout_seconds=60,
        )
        plan = BuildPlan(
            "apt-timeout-plan",
            "project",
            "docker",
            (BuildStep("build", BuildStage.BUILD, command),),
        )
        log = self.storage.save(
            "logs/apt-timeout.log",
            (
                b"RUN apt-get update && apt-get install -y git\n"
                b"command timed out after 60 seconds\n"
            ),
        )
        now = datetime.now(timezone.utc)
        result = BuildResult(
            "apt-timeout-attempt",
            plan.plan_id,
            BuildStatus.TIMED_OUT,
            now,
            now,
            failed_stage=BuildStage.BUILD,
            logs=(log,),
            command_results=(CommandResult(command, None, stdout=log, timed_out=True),),
        )

        failure = self.classifier.classify(self.profile, plan, result)

        self.assertEqual(failure.category, FailureCategory.NETWORK)
        self.assertEqual(failure.kind, BuildFailureKind.NETWORK)
        self.assertTrue(failure.infrastructure_related)
        self.assertIn("timeout_profile=system-package-install", failure.evidence)
        self.assertTrue(
            any(item.startswith("network_inference=") for item in failure.evidence)
        )

    def test_timed_out_git_clone_is_active_dependency_download(self) -> None:
        failure = self.classify_text(
            "\n".join(
                (
                    "Collecting botocore from git+https://github.com/boto/botocore.git",
                    "Running command git clone --filter=blob:none --quiet "
                    "https://github.com/boto/botocore.git /tmp/pip-build",
                    "command timed out after 300 seconds",
                )
            ),
            stage=BuildStage.DEPENDENCY_INSTALLATION,
            purpose=CommandPurpose.INSTALL,
            timed_out=True,
        )

        self.assertIn("timeout_profile=python-package-install", failure.evidence)
        self.assertIn("timeout_activity=dependency-download", failure.evidence)
        self.assertIn(
            "last_active_line=Running command git clone --filter=blob:none --quiet "
            "https://github.com/boto/botocore.git /tmp/pip-build",
            failure.evidence,
        )

    def test_active_apt_download_wins_over_poetry_image_and_gcc_names(self) -> None:
        failure = self.classify_text(
            "\n".join(
                (
                    "FROM dprauto-tools/python-poetry:python-3.11-poetry-1.8.5",
                    "RUN apt-get update && apt-get install -y gcc libpcsclite-dev swig",
                    "#10 102.7 Get:13 http://deb.debian.org/debian gcc [11.0 MB]",
                    "command timed out after 300 seconds",
                )
            ),
            timed_out=True,
        )

        self.assertEqual(failure.category, FailureCategory.NETWORK)
        self.assertEqual(failure.kind, BuildFailureKind.NETWORK)
        self.assertIn("timeout_profile=system-package-install", failure.evidence)
        self.assertIn("timeout_activity=dependency-download", failure.evidence)


if __name__ == "__main__":
    unittest.main()

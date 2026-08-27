import hashlib
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from dprauto.adapters.python import PythonEnvironmentDiffer
from dprauto.adapters.storage import LocalArtifactStorage
from dprauto.agent.full_workflow import EnvironmentBuildWorkflow
from dprauto.agent.models import FixPlan, ToolCall, ToolContext, ToolResult
from dprauto.agent.tools import ModifyBuildScriptTool, PatchSystemPackagesTool, ToolRegistry
from dprauto.agent.workflow import AgentWorkflow
from dprauto.config import SecurityConfig
from dprauto.domain.enums import (
    AgentPhase,
    BuildStage,
    BuildStatus,
    ChangeKind,
    CommandPurpose,
    FailureCategory,
    ProjectType,
    RiskLevel,
)
from dprauto.domain.models import (
    BuildPlan,
    BuildResult,
    BuildStep,
    CommandSpec,
    EnvironmentSnapshot,
    FailureInfo,
    GeneratedFile,
    ProjectProfile,
    SourceReference,
)
from dprauto.environment import compare_environment_snapshots
from dprauto.errors import PolicyViolationError


def source_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "absent"


class EnvironmentSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.profile = ProjectProfile(
            "environment-project",
            SourceReference("fixture://environment"),
            languages=("Python",),
            project_type=ProjectType.SCRIPT,
            runtime_constraints={"python": ">=3.9"},
        )

    def test_snapshot_extracts_every_requested_environment_dimension(self) -> None:
        (self.root / "Dockerfile").write_text(
            "FROM python:3.10-slim\n"
            "ENV APP_MODE=prod API_TOKEN=do-not-persist\n"
            "RUN apt-get update && apt-get install -y curl=7.0 gcc\n"
            "RUN python -m pip install requests==2.31.0\n"
            'ENTRYPOINT ["python", "app.py"]\n'
            'CMD ["--port", "8000"]\n',
            encoding="utf-8",
        )
        (self.root / "setup.sh").write_text(
            "#!/bin/sh\nexport DEBUG=1\nexec python app.py --port 8000\n",
            encoding="utf-8",
        )
        (self.root / "requirements.txt").write_text("urllib3>=2\n", encoding="utf-8")
        (self.root / "app.py").write_text("print('ok')\n", encoding="utf-8")

        snapshot = PythonEnvironmentDiffer().snapshot(self.profile, self.root)

        self.assertEqual(snapshot.base_image, "python:3.10-slim")
        self.assertEqual(snapshot.python_version, "3.10")
        self.assertEqual(snapshot.system_packages, {"curl": "7.0", "gcc": None})
        self.assertEqual(snapshot.python_dependencies["requests"], "==2.31.0")
        self.assertEqual(snapshot.python_dependencies["urllib3"], ">=2")
        self.assertEqual(snapshot.environment_variables["APP_MODE"], "prod")
        self.assertTrue(snapshot.environment_variables["API_TOKEN"].startswith("<redacted:"))
        self.assertEqual(snapshot.environment_variables["DEBUG"], "1")
        self.assertIn("Dockerfile:ENTRYPOINT", snapshot.startup_arguments)
        self.assertIn("Dockerfile:CMD", snapshot.startup_arguments)
        self.assertIn("setup.sh:EXEC", snapshot.startup_arguments)
        self.assertEqual(set(snapshot.build_scripts), {"Dockerfile", "setup.sh"})
        self.assertEqual(set(snapshot.business_source), {"app.py"})

    def test_snapshot_detects_apt_packages_when_options_precede_install(self) -> None:
        (self.root / "Dockerfile").write_text(
            "FROM python:3.11-slim\n"
            "RUN apt-get -o Acquire::Retries=0 -o Acquire::http::Timeout=15 "
            "install -y git curl\n",
            encoding="utf-8",
        )

        snapshot = PythonEnvironmentDiffer().snapshot(self.profile, self.root)

        self.assertEqual(snapshot.system_packages, {"curl": None, "git": None})

    def test_verification_overlay_is_environment_not_business_source(self) -> None:
        overlay = self.root / ".dprauto" / "requirements-verification.txt"
        overlay.parent.mkdir(parents=True)
        overlay.write_text("pytest-mock==3.14.0\n", encoding="utf-8")

        snapshot = PythonEnvironmentDiffer().snapshot(self.profile, self.root)

        self.assertIn(
            ".dprauto/requirements-verification.txt",
            snapshot.build_scripts,
        )
        self.assertEqual(snapshot.python_dependencies["pytest-mock"], "==3.14.0")
        self.assertFalse(snapshot.business_source)


class StructuredEnvironmentDiffTests(unittest.TestCase):
    def test_build_script_only_change_is_allowed_and_low_risk(self) -> None:
        before = EnvironmentSnapshot(build_scripts={"Dockerfile": "old"})
        after = EnvironmentSnapshot(build_scripts={"Dockerfile": "new"})
        diff = compare_environment_snapshots(before, after)
        self.assertEqual(diff.risk_level, RiskLevel.LOW)
        self.assertFalse(diff.requires_manual_review)
        self.assertEqual(diff.build_scripts[0].kind, ChangeKind.MODIFIED)

    def test_added_environment_components_are_recorded(self) -> None:
        before = EnvironmentSnapshot()
        after = EnvironmentSnapshot(
            system_packages={"libpq-dev": None},
            python_dependencies={"psycopg": ">=3"},
        )
        diff = compare_environment_snapshots(before, after)
        self.assertEqual(diff.risk_level, RiskLevel.MEDIUM)
        self.assertEqual(diff.system_packages[0].name, "libpq-dev")
        self.assertEqual(diff.python_dependencies[0].name, "psycopg")
        self.assertTrue(all(item.kind is ChangeKind.ADDED for item in diff.dependencies))

    def test_dependency_version_change_is_high_risk(self) -> None:
        before = EnvironmentSnapshot(
            system_packages={"curl": "7.0"},
            python_dependencies={"requests": "==2.31.0"},
        )
        after = EnvironmentSnapshot(
            system_packages={"curl": "8.0"},
            python_dependencies={"requests": "==2.32.0"},
        )
        diff = compare_environment_snapshots(before, after)
        self.assertEqual(diff.risk_level, RiskLevel.HIGH)
        self.assertEqual(diff.system_packages[0].before, "7.0")
        self.assertEqual(diff.python_dependencies[0].after, "==2.32.0")

    def test_base_python_environment_and_startup_changes_are_structured(self) -> None:
        before = EnvironmentSnapshot(
            base_image="python:3.10-slim",
            python_version="3.10",
            environment_variables={"MODE": "dev"},
            startup_arguments={"Dockerfile:CMD": '["python", "app.py"]'},
        )
        after = EnvironmentSnapshot(
            base_image="python:3.11-slim",
            python_version="3.11",
            environment_variables={"MODE": "prod"},
            startup_arguments={"Dockerfile:CMD": '["python", "app.py", "--safe"]'},
        )
        diff = compare_environment_snapshots(before, after)
        self.assertEqual(diff.base_image.after, "python:3.11-slim")
        self.assertEqual(diff.python_version.after, "3.11")
        self.assertEqual(diff.environment_variables[0].name, "MODE")
        self.assertEqual(diff.startup_arguments[0].name, "Dockerfile:CMD")
        self.assertEqual(diff.risk_level, RiskLevel.HIGH)

    def test_business_source_change_is_critical_and_requires_review(self) -> None:
        before = EnvironmentSnapshot(business_source={"src/app.py": "old"})
        after = EnvironmentSnapshot(business_source={"src/app.py": "new"})
        diff = compare_environment_snapshots(before, after)
        self.assertTrue(diff.source_changed)
        self.assertTrue(diff.requires_manual_review)
        self.assertEqual(diff.risk_level, RiskLevel.CRITICAL)
        self.assertEqual(diff.business_source[0].path, "src/app.py")
        self.assertIn("business source changed", diff.policy_violations[0])


class StaticTool:
    def __init__(self, name, *, succeeded=True):
        self.name = name
        self.description = name
        self.succeeded = succeeded

    def invoke(self, arguments, context):
        return ToolResult(self.name, self.succeeded, "unused")


class AgentEnvironmentPolicyTests(unittest.TestCase):
    def test_structured_patch_uses_materialized_template_dockerfile_as_diff_baseline(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            storage = LocalArtifactStorage(root / "artifacts")
            tools = ToolRegistry(
                (
                    StaticTool("read_file"),
                    StaticTool("get_build_log"),
                    StaticTool("build_image"),
                    PatchSystemPackagesTool(storage),
                )
            )
            workflow = AgentWorkflow(
                object(), tools, storage, environment_differ=PythonEnvironmentDiffer()
            )
            self.addCleanup(workflow.close)
            profile = ProjectProfile(
                "generated-template",
                SourceReference("fixture://generated-template"),
                languages=("Python",),
                project_type=ProjectType.SCRIPT,
            )
            command = CommandSpec(("docker", "build", "."), purpose=CommandPurpose.BUILD)
            build_plan = BuildPlan(
                "generated-template-plan",
                profile.project_id,
                "template",
                (BuildStep("docker-build", BuildStage.BUILD, command),),
                metadata={"dockerfile": "Dockerfile"},
                generated_files=(
                    GeneratedFile("Dockerfile", "FROM python:3.11-slim\n"),
                ),
            )
            state = {
                "run_id": "generated-template-run",
                "workspace": str(root),
                "attempt_number": 0,
                "project_profile": profile,
                "build_plan": build_plan,
                "fix_plan": FixPlan(
                    "git is required by a declared VCS dependency",
                    (
                        ToolCall(
                            "patch_system_packages",
                            {
                                "path": "Dockerfile",
                                "package_manager": "apt",
                                "packages": ["git"],
                            },
                        ),
                    ),
                ),
                "environment_diffs": (),
                "artifacts": (),
                "summaries": (),
            }

            update = workflow.apply_fix(state)
            self.addCleanup(
                workflow.candidates.reject,
                update["repair_candidate"],
                "test cleanup",
            )

            self.assertEqual(update["phase"], AgentPhase.APPLYING)
            self.assertIsNone(update["environment_diff"].base_image)
            self.assertEqual(
                tuple(item.name for item in update["environment_diff"].system_packages),
                ("git",),
            )
            self.assertEqual(
                update["environment_diff"].build_scripts[0].kind,
                ChangeKind.MODIFIED,
            )
            self.assertEqual(
                update["repair_candidate"].materialized_generated_files,
                ("Dockerfile",),
            )
            self.assertFalse((root / "Dockerfile").exists())

    def test_policy_rejected_mutation_restores_original_build_script(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = "FROM python:3.11-slim\n"
            (root / "Dockerfile").write_text(original, encoding="utf-8")
            storage = LocalArtifactStorage(root / "artifacts")
            tools = ToolRegistry(
                (
                    StaticTool("read_file"),
                    StaticTool("get_build_log"),
                    StaticTool("build_image"),
                    StaticTool("inspect_project"),
                    ModifyBuildScriptTool(storage),
                )
            )
            workflow = AgentWorkflow(
                object(), tools, storage, environment_differ=PythonEnvironmentDiffer()
            )
            profile = ProjectProfile(
                "rollback-policy",
                SourceReference("fixture://rollback-policy"),
                languages=("Python",),
                project_type=ProjectType.SCRIPT,
            )
            command = CommandSpec(("docker", "build", "."), purpose=CommandPurpose.BUILD)
            build_plan = BuildPlan(
                "rollback-plan",
                profile.project_id,
                "docker",
                (BuildStep("docker-build", BuildStage.BUILD, command),),
                metadata={"dockerfile": "Dockerfile"},
            )
            now = datetime.now(timezone.utc)
            build_result = BuildResult(
                "accepted-attempt",
                build_plan.plan_id,
                BuildStatus.SUCCEEDED,
                now,
                now,
                exit_code=0,
                image_reference="fixture:accepted",
            )

            state = {
                "run_id": "rollback-policy-run",
                "workspace": str(root),
                "attempt_number": 0,
                "project_profile": profile,
                "build_plan": build_plan,
                "build_result": build_result,
                "failure": FailureInfo(
                    FailureCategory.BUILD_COMMAND,
                    BuildStage.BUILD,
                    "Build command timed out",
                    "timeout:rollback",
                ),
                "fix_plan": FixPlan(
                    "dependency expansion that policy rejects",
                    (
                        ToolCall(
                            "modify_build_script",
                            {
                                "path": "Dockerfile",
                                "content": original + "RUN python -m pip install pytest\n",
                                "source_sha256": source_sha(root / "Dockerfile"),
                            },
                        ),
                    ),
                ),
                "environment_diffs": (),
                "artifacts": (),
                "summaries": (),
            }
            update = workflow.apply_fix(state)
            final_result = EnvironmentBuildWorkflow(workflow, storage).finalize(
                state | update
            )["final_result"]
            dockerfile = next(
                script for script in final_result.final_build_scripts if script.path == "Dockerfile"
            )

            self.assertEqual(update["phase"], AgentPhase.STOPPED)
            self.assertIn("dependency expansion", update["stop_reason"])
            self.assertEqual(dockerfile.content, original)
            self.assertEqual((root / "Dockerfile").read_text(encoding="utf-8"), original)

    def test_source_is_denied_by_default_and_allowed_mode_stops_for_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
            storage = LocalArtifactStorage(root / "artifacts")
            context = ToolContext("environment-policy", 1, str(root))
            with self.assertRaises(PolicyViolationError):
                ModifyBuildScriptTool(storage).invoke(
                    {
                        "path": "app.py",
                        "content": "VALUE = 2\n",
                        "source_sha256": source_sha(root / "app.py"),
                    },
                    context,
                )

            mutation = ModifyBuildScriptTool(
                storage, SecurityConfig(allow_source_changes=True)
            )
            tools = ToolRegistry(
                (
                    StaticTool("read_file"),
                    StaticTool("get_build_log"),
                    StaticTool("build_image"),
                    mutation,
                )
            )
            workflow = AgentWorkflow(
                object(),
                tools,
                storage,
                environment_differ=PythonEnvironmentDiffer(),
            )
            profile = ProjectProfile(
                "source-review",
                SourceReference("fixture://source-review"),
                languages=("Python",),
                project_type=ProjectType.SCRIPT,
            )
            update = workflow.apply_fix(
                {
                    "run_id": "source-review-run",
                    "workspace": str(root),
                    "attempt_number": 0,
                    "project_profile": profile,
                    "fix_plan": FixPlan(
                        "change source",
                        (
                            ToolCall(
                                "modify_build_script",
                                {
                                    "path": "app.py",
                                    "content": "VALUE = 2\n",
                                    "source_sha256": source_sha(root / "app.py"),
                                },
                            ),
                        ),
                    ),
                    "environment_diffs": (),
                    "artifacts": (),
                    "summaries": (),
                }
            )
            self.assertEqual(update["phase"], AgentPhase.STOPPED)
            self.assertTrue(update["manual_review_required"])
            self.assertTrue(update["environment_diff"].requires_manual_review)
            self.assertEqual(update["environment_diff"].risk_level, RiskLevel.CRITICAL)

    def test_partial_change_is_recorded_even_if_a_later_tool_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Dockerfile").write_text("FROM python:3.11-slim\n", encoding="utf-8")
            storage = LocalArtifactStorage(root / "artifacts")
            tools = ToolRegistry(
                (
                    StaticTool("read_file"),
                    StaticTool("get_build_log"),
                    StaticTool("build_image"),
                    StaticTool("later_failure", succeeded=False),
                    ModifyBuildScriptTool(storage),
                )
            )
            workflow = AgentWorkflow(
                object(), tools, storage, environment_differ=PythonEnvironmentDiffer()
            )
            profile = ProjectProfile(
                "partial-change",
                SourceReference("fixture://partial-change"),
                languages=("Python",),
                project_type=ProjectType.SCRIPT,
            )
            update = workflow.apply_fix(
                {
                    "run_id": "partial-change-run",
                    "workspace": str(root),
                    "attempt_number": 0,
                    "project_profile": profile,
                    "fix_plan": FixPlan(
                        "partial change",
                        (
                            ToolCall(
                                "modify_build_script",
                                {
                                    "path": "Dockerfile",
                                    "content": "FROM python:3.11-slim\nRUN python --version\n",
                                    "source_sha256": source_sha(root / "Dockerfile"),
                                },
                            ),
                            ToolCall("later_failure", {}),
                        ),
                    ),
                    "environment_diffs": (),
                    "artifacts": (),
                    "summaries": (),
                }
            )
            self.assertEqual(update["phase"], AgentPhase.STOPPED)
            self.assertIn("later_failure", update["stop_reason"])
            self.assertEqual(update["environment_diff"].build_scripts[0].path, "Dockerfile")
            self.assertTrue(
                any(item.key.endswith("environment-diff.json") for item in update["artifacts"])
            )

    def test_timeout_repair_rejects_dependency_expansion_before_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Dockerfile").write_text("FROM python:3.11-slim\n", encoding="utf-8")
            storage = LocalArtifactStorage(root / "artifacts")
            tools = ToolRegistry(
                (
                    StaticTool("read_file"),
                    StaticTool("get_build_log"),
                    StaticTool("build_image"),
                    ModifyBuildScriptTool(storage),
                )
            )
            workflow = AgentWorkflow(
                object(), tools, storage, environment_differ=PythonEnvironmentDiffer()
            )
            profile = ProjectProfile(
                "timeout-policy",
                SourceReference("fixture://timeout-policy"),
                languages=("Python",),
                project_type=ProjectType.SCRIPT,
            )

            update = workflow.apply_fix(
                {
                    "run_id": "timeout-policy-run",
                    "workspace": str(root),
                    "attempt_number": 0,
                    "project_profile": profile,
                    "failure": FailureInfo(
                        FailureCategory.BUILD_COMMAND,
                        BuildStage.BUILD,
                        "Build command timed out",
                        "timeout:fingerprint",
                    ),
                    "fix_plan": FixPlan(
                        "add more packages after timeout",
                        (
                            ToolCall(
                                "modify_build_script",
                                {
                                    "path": "Dockerfile",
                                    "content": (
                                        "FROM python:3.11-slim\n"
                                        "RUN python -m pip install pytest\n"
                                    ),
                                    "source_sha256": source_sha(root / "Dockerfile"),
                                },
                            ),
                        ),
                    ),
                    "environment_diffs": (),
                    "artifacts": (),
                    "summaries": (),
                }
            )

            self.assertEqual(update["phase"], AgentPhase.STOPPED)
            self.assertIn("dependency expansion", update["stop_reason"])
            self.assertEqual(update["environment_diff"].python_dependencies[0].name, "pytest")

    def test_successful_build_test_repair_rejects_build_dependency_expansion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Dockerfile").write_text("FROM python:3.11-slim\n", encoding="utf-8")
            storage = LocalArtifactStorage(root / "artifacts")
            tools = ToolRegistry(
                (
                    StaticTool("read_file"),
                    StaticTool("get_build_log"),
                    StaticTool("build_image"),
                    ModifyBuildScriptTool(storage),
                )
            )
            workflow = AgentWorkflow(
                object(), tools, storage, environment_differ=PythonEnvironmentDiffer()
            )
            profile = ProjectProfile(
                "test-repair-policy",
                SourceReference("fixture://test-repair-policy"),
                languages=("Python",),
                project_type=ProjectType.SCRIPT,
            )

            update = workflow.apply_fix(
                {
                    "run_id": "test-repair-policy-run",
                    "workspace": str(root),
                    "attempt_number": 0,
                    "project_profile": profile,
                    "build_result": BuildResult(
                        "attempt-0",
                        "plan-0",
                        BuildStatus.SUCCEEDED,
                        datetime.now(timezone.utc),
                        datetime.now(timezone.utc),
                        exit_code=0,
                        image_reference="fixture:test-repair-policy",
                    ),
                    "failure": FailureInfo(
                        FailureCategory.TEST,
                        BuildStage.TEST,
                        "testability verification did not pass",
                        "test:fingerprint",
                    ),
                    "fix_plan": FixPlan(
                        "add test dependency to build image",
                        (
                            ToolCall(
                                "modify_build_script",
                                {
                                    "path": "Dockerfile",
                                    "content": (
                                        "FROM python:3.11-slim\n"
                                        "RUN python -m pip install pytest\n"
                                    ),
                                    "source_sha256": source_sha(root / "Dockerfile"),
                                },
                            ),
                        ),
                    ),
                    "environment_diffs": (),
                    "artifacts": (),
                    "summaries": (),
                }
            )

            self.assertEqual(update["phase"], AgentPhase.STOPPED)
            self.assertIn("verification repair rejected", update["stop_reason"])
            self.assertIn("pytest", update["stop_reason"])

    def test_whole_file_dependency_change_requires_structured_patch_tool(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = "FROM python:3.11-slim\n"
            (root / "Dockerfile").write_text(original, encoding="utf-8")
            storage = LocalArtifactStorage(root / "artifacts")
            tools = ToolRegistry(
                (
                    StaticTool("read_file"),
                    StaticTool("get_build_log"),
                    StaticTool("build_image"),
                    ModifyBuildScriptTool(storage),
                )
            )
            workflow = AgentWorkflow(
                object(), tools, storage, environment_differ=PythonEnvironmentDiffer()
            )
            profile = ProjectProfile(
                "structured-policy",
                SourceReference("fixture://structured-policy"),
                languages=("Python",),
                project_type=ProjectType.SCRIPT,
            )

            update = workflow.apply_fix(
                {
                    "run_id": "structured-policy-run",
                    "workspace": str(root),
                    "attempt_number": 0,
                    "project_profile": profile,
                    "fix_plan": FixPlan(
                        "add an OS dependency by replacing the whole file",
                        (
                            ToolCall(
                                "modify_build_script",
                                {
                                    "path": "Dockerfile",
                                    "content": original + "RUN apt-get install -y git\n",
                                    "source_sha256": source_sha(root / "Dockerfile"),
                                },
                            ),
                        ),
                    ),
                    "environment_diffs": (),
                    "artifacts": (),
                    "summaries": (),
                }
            )

            self.assertEqual(update["phase"], AgentPhase.STOPPED)
            self.assertIn("use patch_system_packages", update["stop_reason"])
            self.assertEqual(update["repair_candidate"].status, "rejected")

    def test_diff_rejects_multiple_high_risk_dimensions_in_one_round(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Dockerfile").write_text(
                "FROM python:3.10-slim\n", encoding="utf-8"
            )
            storage = LocalArtifactStorage(root / "artifacts")
            tools = ToolRegistry(
                (
                    StaticTool("read_file"),
                    StaticTool("get_build_log"),
                    StaticTool("build_image"),
                    ModifyBuildScriptTool(storage),
                )
            )
            workflow = AgentWorkflow(
                object(), tools, storage, environment_differ=PythonEnvironmentDiffer()
            )
            profile = ProjectProfile(
                "multi-dimension-policy",
                SourceReference("fixture://multi-dimension-policy"),
                languages=("Python",),
                project_type=ProjectType.SCRIPT,
            )

            update = workflow.apply_fix(
                {
                    "run_id": "multi-dimension-policy-run",
                    "workspace": str(root),
                    "attempt_number": 0,
                    "project_profile": profile,
                    "fix_plan": FixPlan(
                        "change runtime and system packages together",
                        (
                            ToolCall(
                                "modify_build_script",
                                {
                                    "path": "Dockerfile",
                                    "content": (
                                        "FROM python:3.11-slim\n"
                                        "RUN apt-get install -y git\n"
                                    ),
                                    "source_sha256": source_sha(root / "Dockerfile"),
                                },
                            ),
                        ),
                    ),
                    "environment_diffs": (),
                    "artifacts": (),
                    "summaries": (),
                }
            )

            self.assertEqual(update["phase"], AgentPhase.STOPPED)
            self.assertIn("multiple high-risk", update["stop_reason"])
            self.assertIn("runtime", update["stop_reason"])
            self.assertIn("system-packages", update["stop_reason"])


if __name__ == "__main__":
    unittest.main()

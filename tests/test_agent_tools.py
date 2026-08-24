import sys
import tempfile
import unittest
from pathlib import Path

from dprauto.adapters.execution import SubprocessCommandExecutor
from dprauto.adapters.storage import LocalArtifactStorage
from dprauto.agent.models import ToolContext
from dprauto.agent.tools import (
    ListProjectFilesTool,
    ModifyBuildScriptTool,
    PatchBaseImageTool,
    PatchPythonDependenciesTool,
    PatchSystemPackagesTool,
    PatchVerificationDependenciesTool,
    ReadFileTool,
    RunCommandTool,
    ToolRegistry,
)
from dprauto.domain.models import (
    CommandSpec,
    ProjectCommand,
    ProjectProfile,
    SourceReference,
)
from dprauto.domain.enums import RiskLevel
from dprauto.errors import PolicyViolationError, ToolExecutionError


class AgentToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.storage = LocalArtifactStorage(self.root / "artifacts")
        self.context = ToolContext("tool-run", 1, str(self.root))

    def test_build_script_change_is_atomic_and_persists_diff(self) -> None:
        dockerfile = self.root / "Dockerfile"
        dockerfile.write_text("FROM python:3.10-slim\n", encoding="utf-8")
        result = ModifyBuildScriptTool(self.storage).invoke(
            {"path": "Dockerfile", "content": "FROM python:3.11-slim\nRUN python --version\n"},
            self.context,
        )

        self.assertTrue(result.succeeded)
        self.assertEqual(dockerfile.read_text(), "FROM python:3.11-slim\nRUN python --version\n")
        self.assertFalse(result.environment_diff.source_changed)
        self.assertEqual(result.environment_diff.base_image.before, "python:3.10-slim")
        self.assertEqual(result.environment_diff.base_image.after, "python:3.11-slim")
        patch = self.storage.load(result.artifacts[0]).decode()
        self.assertIn("-FROM python:3.10-slim", patch)
        self.assertIn("+FROM python:3.11-slim", patch)

    def test_business_source_and_path_escape_are_rejected(self) -> None:
        (self.root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        tool = ModifyBuildScriptTool(self.storage)
        with self.assertRaises(PolicyViolationError):
            tool.invoke(
                {"path": "app.py", "content": "VALUE = 2\n"},
                self.context,
            )
        with self.assertRaises(ToolExecutionError):
            tool.invoke(
                {"path": "../Dockerfile", "content": "FROM scratch\n"},
                self.context,
            )
        (self.root / "real-build-file").write_text("FROM scratch\n", encoding="utf-8")
        (self.root / "Dockerfile").symlink_to(self.root / "real-build-file")
        with self.assertRaises(ToolExecutionError):
            tool.invoke(
                {"path": "Dockerfile", "content": "FROM python:3.11-slim\n"},
                self.context,
            )
        self.assertEqual((self.root / "app.py").read_text(), "VALUE = 1\n")

    def test_setup_script_is_an_allowed_build_script_not_business_source(self) -> None:
        result = ModifyBuildScriptTool(self.storage).invoke(
            {"path": "setup.sh", "content": "#!/bin/sh\npython --version\n"},
            self.context,
        )
        self.assertFalse(result.environment_diff.source_changed)
        self.assertFalse(result.environment_diff.requires_manual_review)

    def test_dockerfile_apt_network_retries_are_bounded(self) -> None:
        result = ModifyBuildScriptTool(self.storage).invoke(
            {
                "path": "Dockerfile",
                "content": (
                    "FROM python:3.11-slim\n"
                    "RUN apt-get update && apt-get install -y git\n"
                ),
            },
            self.context,
        )

        content = (self.root / "Dockerfile").read_text(encoding="utf-8")
        self.assertEqual(content.count('RES_OPTIONS="attempts:1 timeout:2"'), 2)
        self.assertIn("Acquire::Retries=0", content)
        self.assertIn("Acquire::http::Timeout=15", content)
        self.assertIn("Acquire::https::Timeout=15", content)
        self.assertIn('timeout:2" apt-get', content)
        self.assertIn("install -y git", content)
        self.assertTrue(result.data["bounded_apt_network_retries"])

        repeated = ModifyBuildScriptTool(self.storage).invoke(
            {"path": "Dockerfile", "content": content},
            self.context,
        )
        self.assertFalse(repeated.data["changed"])
        self.assertFalse(repeated.data["bounded_apt_network_retries"])

    def test_run_command_accepts_only_detected_non_shell_command(self) -> None:
        command = CommandSpec((sys.executable, "-c", "print('allowed')"))
        profile = ProjectProfile(
            "command-project",
            SourceReference("fixture"),
            commands=(ProjectCommand("check", command, "fixture"),),
        )
        context = ToolContext(
            "command-run",
            1,
            str(self.root),
            project_profile=profile,
        )
        tool = RunCommandTool(SubprocessCommandExecutor(self.storage))

        result = tool.invoke({"argv": list(command.argv)}, context)
        self.assertTrue(result.succeeded)
        with self.assertRaises(PolicyViolationError):
            tool.invoke({"argv": [sys.executable, "-c", "print('not detected')"]}, context)
        with self.assertRaises(ToolExecutionError):
            tool.invoke({"argv": list(command.argv), "shell": True}, context)

    def test_structured_system_packages_are_bounded_and_merge_idempotently(self) -> None:
        dockerfile = self.root / "Dockerfile"
        dockerfile.write_text(
            "FROM python:3.11-slim\nWORKDIR /workspace\n",
            encoding="utf-8",
        )
        tool = PatchSystemPackagesTool(self.storage)

        first = tool.invoke(
            {
                "path": "Dockerfile",
                "package_manager": "apt",
                "packages": ["git", "libpq-dev"],
            },
            self.context,
        )
        second = tool.invoke(
            {
                "path": "Dockerfile",
                "package_manager": "apt",
                "packages": ["git", "curl"],
            },
            self.context,
        )

        content = dockerfile.read_text(encoding="utf-8")
        self.assertEqual(content.count("structured-system-packages"), 1)
        self.assertEqual(content.count("apt-get"), 2)
        self.assertIn('packages=["git","libpq-dev","curl"]', content)
        self.assertIn("Acquire::Retries=0", content)
        self.assertEqual(first.environment_diff.system_packages[0].name, "git")
        self.assertEqual(
            [item.name for item in second.environment_diff.system_packages],
            ["curl"],
        )
        self.assertEqual(first.data["structured_dimension"], "system_packages")
        with self.assertRaises(ToolExecutionError):
            tool.invoke(
                {
                    "path": "Dockerfile",
                    "package_manager": "apt",
                    "packages": ["git;rm -rf /tmp/value"],
                },
                self.context,
            )

    def test_structured_python_dependencies_reject_urls_and_shell_fragments(self) -> None:
        dockerfile = self.root / "Dockerfile"
        dockerfile.write_text("FROM python:3.11-slim\n", encoding="utf-8")
        tool = PatchPythonDependenciesTool(self.storage)

        result = tool.invoke(
            {"path": "Dockerfile", "packages": ["requests>=2.31,<3", "psycopg[binary]==3.2.1"]},
            self.context,
        )

        content = dockerfile.read_text(encoding="utf-8")
        self.assertIn("structured-python-dependencies", content)
        self.assertIn("'requests>=2.31,<3'", content)
        self.assertEqual(
            [item.name for item in result.environment_diff.python_dependencies],
            ["requests", "psycopg"],
        )
        for invalid in (
            "demo @ https://example.invalid/demo.whl",
            "pytest; python_version > '3.10'",
            "requests && touch /tmp/value",
            "--extra-index-url",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ToolExecutionError):
                tool.invoke(
                    {"path": "Dockerfile", "packages": [invalid]},
                    self.context,
                )

    def test_verification_dependencies_are_isolated_from_runtime_image(self) -> None:
        tool = PatchVerificationDependenciesTool(self.storage)

        first = tool.invoke(
            {"packages": ["pytest-mock==3.14.0"]},
            self.context,
        )
        second = tool.invoke(
            {"packages": ["freezegun>=1.5,<2", "pytest-mock==3.14.0"]},
            self.context,
        )

        overlay = self.root / ".dprauto" / "requirements-verification.txt"
        self.assertEqual(
            overlay.read_text(encoding="utf-8"),
            "# DPRAuto Testability-only dependency overlay\n"
            "pytest-mock==3.14.0\n"
            "freezegun>=1.5,<2\n",
        )
        self.assertFalse(first.environment_diff.source_changed)
        self.assertEqual(first.environment_diff.risk_level, RiskLevel.LOW)
        self.assertEqual(
            tuple(item.ecosystem for item in first.environment_diff.python_dependencies),
            ("verification-python",),
        )
        self.assertEqual(
            second.data["packages"],
            ("pytest-mock==3.14.0", "freezegun>=1.5,<2"),
        )
        self.assertFalse((self.root / "Dockerfile").exists())

        with self.assertRaises(ToolExecutionError):
            tool.invoke(
                {"packages": ["https://example.invalid/pkg.whl"]},
                self.context,
            )

    def test_verification_dependencies_cannot_override_controlled_runners(self) -> None:
        tool = PatchVerificationDependenciesTool(self.storage)
        overlay = self.root / ".dprauto" / "requirements-verification.txt"

        for requirement in (
            "pytest<8.3.5",
            "pytest_xdist==3.6.1",
            "tox>=4",
            "nox==2025.5.1",
        ):
            with self.subTest(requirement=requirement), self.assertRaisesRegex(
                ToolExecutionError,
                "DPRAuto-managed test runner",
            ):
                tool.invoke({"packages": [requirement]}, self.context)

        self.assertFalse(overlay.exists())

    def test_structured_base_image_requires_exact_non_latest_precondition(self) -> None:
        dockerfile = self.root / "Dockerfile"
        dockerfile.write_text(
            "FROM python:3.10-slim AS runtime\nRUN python --version\n",
            encoding="utf-8",
        )
        tool = PatchBaseImageTool(self.storage)

        result = tool.invoke(
            {
                "path": "Dockerfile",
                "current_image": "python:3.10-slim",
                "replacement_image": "python:3.11-slim",
            },
            self.context,
        )

        self.assertIn("FROM python:3.11-slim AS runtime", dockerfile.read_text())
        self.assertEqual(result.environment_diff.base_image.before, "python:3.10-slim")
        self.assertEqual(result.environment_diff.base_image.after, "python:3.11-slim")
        self.assertEqual(result.data["structured_dimension"], "runtime")
        with self.assertRaises(ToolExecutionError):
            tool.invoke(
                {
                    "path": "Dockerfile",
                    "current_image": "python:3.11-slim",
                    "replacement_image": "python:latest",
                },
                self.context,
            )

    def test_registry_enforces_exact_tool_argument_schema_before_execution(self) -> None:
        (self.root / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
        registry = ToolRegistry((ReadFileTool(),))

        with self.assertRaises(ToolExecutionError) as raised:
            registry.invoke(
                "read_file",
                {"path": "Dockerfile", "size_bound": 8192},
                self.context,
            )

        self.assertIn("unexpected argument 'size_bound'", str(raised.exception))
        schema = registry.specifications["read_file"]["argument_schema"]
        self.assertEqual(schema["required"], ["path"])
        self.assertEqual(registry.specifications["read_file"]["effect"], "observe")

    def test_read_and_search_hide_sensitive_project_files(self) -> None:
        from dprauto.agent.tools import SearchProjectTool

        (self.root / ".env").write_text("API_TOKEN=secret-value\n", encoding="utf-8")
        (self.root / ".env.example").write_text("API_TOKEN=example\n", encoding="utf-8")
        (self.root / "README.md").write_text("API_TOKEN is configured here\n", encoding="utf-8")

        with self.assertRaises(PolicyViolationError):
            ReadFileTool().invoke({"path": ".env"}, self.context)
        example = ReadFileTool().invoke({"path": ".env.example"}, self.context)
        matches = SearchProjectTool().invoke({"query": "API_TOKEN"}, self.context)

        self.assertIn("example", example.data["content"])
        self.assertNotIn("secret-value", str(matches.data["matches"]))
        self.assertEqual(
            {item["path"] for item in matches.data["matches"]},
            {".env.example", "README.md"},
        )

    def test_read_file_pages_large_files_by_bounded_line_range(self) -> None:
        content = "".join(f"line-{number}\n" for number in range(1, 1_001))
        (self.root / "large.py").write_text(content, encoding="utf-8")
        tool = ReadFileTool(max_lines=100)

        first = tool.invoke({"path": "large.py"}, self.context)
        middle = tool.invoke(
            {"path": "large.py", "start_line": 650, "end_line": 749},
            self.context,
        )

        self.assertEqual(first.data["start_line"], 1)
        self.assertEqual(first.data["end_line"], 100)
        self.assertEqual(first.data["total_lines"], 1_000)
        self.assertEqual(first.data["next_start_line"], 101)
        self.assertTrue(first.data["truncated"])
        self.assertIn("line-650", middle.data["content"])
        self.assertIn("line-749", middle.data["content"])
        with self.assertRaises(ToolExecutionError):
            tool.invoke(
                {"path": "large.py", "start_line": 1, "end_line": 101},
                self.context,
            )
        with self.assertRaises(ToolExecutionError):
            tool.invoke(
                {"path": "large.py", "start_line": 20, "end_line": 10},
                self.context,
            )

    def test_list_project_files_is_bounded_and_read_only(self) -> None:
        (self.root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        (self.root / "README.md").write_text("project\n", encoding="utf-8")
        result = ListProjectFilesTool(max_files=1).invoke({}, self.context)

        self.assertEqual(len(result.data["files"]), 1)
        self.assertTrue(result.data["truncated"])
        self.assertEqual(ListProjectFilesTool.effect, "observe")

    def test_registry_exposes_mutation_effect_for_planning(self) -> None:
        registry = ToolRegistry(
            (
                ReadFileTool(),
                ModifyBuildScriptTool(self.storage),
                PatchSystemPackagesTool(self.storage),
            )
        )

        self.assertEqual(registry.specifications["read_file"]["effect"], "observe")
        self.assertEqual(
            registry.specifications["modify_build_script"]["effect"],
            "mutate",
        )
        self.assertEqual(
            registry.specifications["patch_system_packages"]["effect"],
            "mutate",
        )



if __name__ == "__main__":
    unittest.main()

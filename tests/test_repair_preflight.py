import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from dprauto.adapters.execution import SubprocessCommandExecutor
from dprauto.adapters.preflight import DockerRepairPreflight
from dprauto.adapters.storage import LocalArtifactStorage
from dprauto.agent.models import ToolContext
from dprauto.agent.tools import PatchSystemPackagesTool
from dprauto.domain.enums import ChangeKind, VerificationStatus
from dprauto.domain.models import CommandResult, EnvironmentDiff, FileChange


class RecordingExecutor:
    def __init__(self, storage, responses):
        self.storage = storage
        self.responses = list(responses)
        self.commands = []

    def execute(self, command, workspace):
        self.commands.append(command)
        exit_code, output, timed_out = self.responses.pop(0)
        artifact = self.storage.save(
            f"preflight/{len(self.commands)}.log",
            output.encode(),
            media_type="text/plain",
        )
        return CommandResult(
            command,
            exit_code,
            stdout=artifact,
            timed_out=timed_out,
            duration_seconds=0.01,
        )


def changed(path):
    change = FileChange(path, ChangeKind.MODIFIED, "before", "after")
    return EnvironmentDiff(files=(change,), build_scripts=(change,), summary=f"updated {path}")


class RepairPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.storage = LocalArtifactStorage(self.root / "artifacts")

    def test_invalid_setup_shell_is_an_explicit_preflight_failure(self):
        (self.root / "setup.sh").write_text("if true; then\n", encoding="utf-8")
        executor = RecordingExecutor(
            self.storage,
            ((2, "setup.sh: syntax error: unexpected end of file", False),),
        )

        result = DockerRepairPreflight(executor, self.storage).run(
            self.root, changed("setup.sh")
        )

        self.assertEqual(result.status, VerificationStatus.FAILED)
        self.assertFalse(result.accepted)
        self.assertEqual(executor.commands[0].argv, ("sh", "-n", "setup.sh"))
        self.assertIn("syntax error", result.checks[0].summary)
        self.assertTrue(result.artifacts)

    def test_bash_shebang_selects_bash_syntax_parser(self):
        (self.root / "setup.sh").write_text(
            "#!/usr/bin/env bash\nvalues=(one two)\n", encoding="utf-8"
        )
        executor = RecordingExecutor(self.storage, ((0, "", False),))

        result = DockerRepairPreflight(executor, self.storage).run(
            self.root, changed("setup.sh")
        )

        self.assertTrue(result.accepted)
        self.assertEqual(executor.commands[0].argv, ("bash", "-n", "setup.sh"))

    def test_docker_network_failure_is_inconclusive_not_a_repair_rejection(self):
        (self.root / "Dockerfile").write_text("FROM example.invalid/base\n", encoding="utf-8")
        executor = RecordingExecutor(
            self.storage,
            ((1, "failed to resolve source metadata: network is unreachable", False),),
        )

        result = DockerRepairPreflight(executor, self.storage).run(
            self.root, changed("Dockerfile")
        )

        self.assertEqual(result.status, VerificationStatus.SKIPPED)
        self.assertTrue(result.accepted)
        self.assertIn("--check", executor.commands[0].argv)
        self.assertIn("inconclusive", result.checks[0].summary)

    def test_explicit_dockerfile_parse_error_rejects_rebuild(self):
        (self.root / "Dockerfile").write_text("FROM\n", encoding="utf-8")
        executor = RecordingExecutor(
            self.storage,
            ((1, "Dockerfile parse error line 1: FROM requires one argument", False),),
        )

        result = DockerRepairPreflight(executor, self.storage).run(
            self.root, changed("Dockerfile")
        )

        self.assertEqual(result.status, VerificationStatus.FAILED)
        self.assertFalse(result.accepted)
        self.assertIn("parse error", result.checks[0].summary)

    def test_removed_or_unrelated_files_do_not_run_commands(self):
        removed = FileChange("Dockerfile", ChangeKind.REMOVED, "before", None)
        executor = RecordingExecutor(self.storage, ())

        result = DockerRepairPreflight(executor, self.storage).run(
            self.root,
            EnvironmentDiff(files=(removed,), summary="removed Dockerfile"),
        )

        self.assertEqual(result.status, VerificationStatus.SKIPPED)
        self.assertTrue(result.accepted)
        self.assertEqual(executor.commands, [])

    def test_changed_symlink_is_rejected_without_invoking_parser(self):
        (self.root / "real.Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
        (self.root / "Dockerfile").symlink_to(self.root / "real.Dockerfile")
        executor = RecordingExecutor(self.storage, ())

        result = DockerRepairPreflight(executor, self.storage).run(
            self.root, changed("Dockerfile")
        )

        self.assertEqual(result.status, VerificationStatus.FAILED)
        self.assertFalse(result.accepted)
        self.assertIn("unsafe", result.checks[0].summary)
        self.assertEqual(executor.commands, [])


def docker_check_ready():
    if not shutil.which("docker"):
        return False
    return subprocess.run(
        ["docker", "info"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0


@unittest.skipUnless(docker_check_ready(), "Docker daemon is required")
class RepairPreflightDockerIntegrationTests(unittest.TestCase):
    def test_real_docker_check_parses_without_executing_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "run-must-not-execute"
            (root / "Dockerfile").write_text(
                "FROM scratch\nRUN touch /run-must-not-execute\n",
                encoding="utf-8",
            )
            storage = LocalArtifactStorage(root / "artifacts")
            result = DockerRepairPreflight(
                SubprocessCommandExecutor(storage), storage
            ).run(root, changed("Dockerfile"))

            self.assertEqual(result.status, VerificationStatus.PASSED)
            self.assertFalse(marker.exists())

    def test_structured_system_package_patch_passes_real_docker_check(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
            storage = LocalArtifactStorage(root / "artifacts")
            patched = PatchSystemPackagesTool(storage).invoke(
                {
                    "path": "Dockerfile",
                    "package_manager": "apk",
                    "packages": ["git"],
                },
                ToolContext("structured-docker-check", 1, str(root)),
            )

            result = DockerRepairPreflight(
                SubprocessCommandExecutor(storage), storage
            ).run(root, patched.environment_diff)

            self.assertEqual(result.status, VerificationStatus.PASSED)


if __name__ == "__main__":
    unittest.main()

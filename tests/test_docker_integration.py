import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from dprauto.adapters.python import PythonProjectParser
from dprauto.adapters.storage import LocalArtifactStorage
from dprauto.application import create_deterministic_builder
from dprauto.config import BuildConfig
from dprauto.domain.enums import BuildFailureKind, BuildStatus, FailureCategory
from dprauto.domain.models import SourceReference


FIXTURES = Path(__file__).parent / "fixtures" / "build"
BASE_IMAGE = "python:3.11-slim"


def docker_ready() -> bool:
    if not shutil.which("docker"):
        return False
    daemon = subprocess.run(
        ["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False
    )
    image = subprocess.run(
        ["docker", "image", "inspect", BASE_IMAGE],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return daemon.returncode == 0 and image.returncode == 0


@unittest.skipUnless(docker_ready(), f"Docker daemon and local {BASE_IMAGE} are required")
class DockerBuildIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        storage = LocalArtifactStorage(Path(self.temporary.name) / "artifacts")
        self.config = BuildConfig(
            timeout_seconds=120,
            image_repository="dprauto-integration",
            python_base_image="python:{version}-slim",
        )
        self.parser = PythonProjectParser()
        self.builder = create_deterministic_builder(storage, self.config)

    def build_and_run(self, fixture: str, expected: str, *, existing_dockerfile: bool) -> None:
        workspace = FIXTURES / fixture
        profile = self.parser.parse(SourceReference(f"fixture://{fixture}"), workspace)
        execution = self.builder.build(profile, workspace)
        expected_strategy = "docker" if existing_dockerfile else "template"
        self.assertEqual(execution.plan.strategy, expected_strategy)
        image = execution.plan.metadata["image_reference"]
        self.addCleanup(
            subprocess.run,
            ["docker", "image", "rm", "--force", image],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )

        result = execution.result
        self.assertEqual(result.status, BuildStatus.SUCCEEDED)
        self.assertIsNone(execution.failure)
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(result.logs)
        if existing_dockerfile:
            self.assertTrue(any(item.key.endswith("/inputs/Dockerfile") for item in result.outputs))
        run = subprocess.run(
            ["docker", "run", "--rm", image],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(run.returncode, 0, run.stdout)
        self.assertIn(expected, run.stdout)

    def test_requirements_script_template(self) -> None:
        self.build_and_run("requirements_script", "requirements-script-ok", existing_dockerfile=False)

    def test_setup_package_template(self) -> None:
        self.build_and_run("setup_package", "setup-package-ok", existing_dockerfile=False)

    def test_existing_dockerfile_project(self) -> None:
        self.build_and_run("docker_project", "docker-project-ok", existing_dockerfile=True)

    def test_real_docker_build_failure_falls_back_to_template(self) -> None:
        workspace = FIXTURES / "failing_project"
        profile = self.parser.parse(SourceReference("fixture://failing-project"), workspace)
        execution = self.builder.build(profile, workspace)

        self.assertEqual(execution.result.status, BuildStatus.SUCCEEDED)
        self.assertEqual(execution.plan.strategy, "template")
        self.assertIsNone(execution.failure)
        self.assertEqual(
            tuple(item.plan.strategy for item in execution.attempts),
            ("docker", "template"),
        )
        failed = execution.attempts[0]
        self.assertEqual(failed.result.status, BuildStatus.FAILED)
        self.assertNotEqual(failed.result.exit_code, 0)
        self.assertTrue(failed.result.logs)
        self.assertEqual(failed.failure.kind, BuildFailureKind.PROJECT_BUILD)
        self.assertEqual(failed.failure.category, FailureCategory.UNKNOWN)
        self.addCleanup(
            subprocess.run,
            ["docker", "image", "rm", "--force", execution.result.image_reference],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )


if __name__ == "__main__":
    unittest.main()

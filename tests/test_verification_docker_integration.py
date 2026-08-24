import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from dprauto.adapters.python import PythonProjectParser
from dprauto.adapters.storage import LocalArtifactStorage
from dprauto.application import create_deterministic_builder, create_layered_verifier
from dprauto.config import BuildConfig, VerificationConfig
from dprauto.domain.enums import VerificationLevel, VerificationStatus
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
class LayeredVerificationDockerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        storage = LocalArtifactStorage(Path(self.temporary.name) / "artifacts")
        self.config = BuildConfig(
            timeout_seconds=120,
            image_repository="dprauto-verification-integration",
            python_base_image="python:{version}-slim",
        )
        self.builder = create_deterministic_builder(storage, self.config)
        self.verifier = create_layered_verifier(
            storage,
            self.config,
            VerificationConfig(command_timeout_seconds=30, web_startup_timeout_seconds=10),
        )
        self.parser = PythonProjectParser()

    def verify_fixture(self, fixture: str) -> None:
        workspace = FIXTURES / fixture
        profile = self.parser.parse(SourceReference(f"fixture://{fixture}"), workspace)
        execution = self.builder.build(profile, workspace)
        image = execution.result.image_reference
        self.addCleanup(
            subprocess.run,
            ["docker", "image", "rm", "--force", image],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        report = self.verifier.verify(
            profile,
            execution.result,
            workspace,
            build_plan=execution.plan,
        )
        self.assertTrue(report.succeeded)
        self.assertEqual(
            report.result_for(VerificationLevel.TESTABILITY).status,
            VerificationStatus.SKIPPED,
        )
        self.assertTrue(report.result_for(VerificationLevel.INSTALLABILITY).passed)
        self.assertTrue(report.result_for(VerificationLevel.RUNNABILITY).passed)

    def test_script_image_is_installable_and_runnable(self) -> None:
        self.verify_fixture("requirements_script")

    def test_library_image_has_importable_public_api(self) -> None:
        self.verify_fixture("library_package")


if __name__ == "__main__":
    unittest.main()

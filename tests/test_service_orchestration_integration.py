import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from dprauto.adapters.storage import LocalArtifactStorage
from dprauto.adapters.verification.docker import DockerContainerRuntime
from dprauto.config import BuildConfig, VerificationConfig
from dprauto.domain.enums import CommandPurpose
from dprauto.domain.models import CommandSpec
from dprauto.ports.runtime import ServiceSpec, TestEnvironmentSpec

POSTGRES_IMAGE = "postgres:15-alpine"
REDIS_IMAGE = "redis:7-alpine"


def docker_services_ready() -> bool:
    if not shutil.which("docker"):
        return False
    for command in (
        ["docker", "info"],
        ["docker", "image", "inspect", POSTGRES_IMAGE],
        ["docker", "image", "inspect", REDIS_IMAGE],
    ):
        if (
            subprocess.run(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            ).returncode
            != 0
        ):
            return False
    return True


@unittest.skipUnless(
    docker_services_ready(),
    "Docker daemon and local PostgreSQL/Redis images are required",
)
class ServiceOrchestrationIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.runtime = DockerContainerRuntime(
            LocalArtifactStorage(Path(self.temporary.name) / "artifacts"),
            BuildConfig(use_cache=False),
            VerificationConfig(
                command_timeout_seconds=20,
                service_startup_timeout_seconds=20,
                postgres_service_image=POSTGRES_IMAGE,
                redis_service_image=REDIS_IMAGE,
            ),
        )

    def test_postgresql_health_connectivity_and_cleanup(self) -> None:
        execution = self.runtime.run_environment(
            POSTGRES_IMAGE,
            CommandSpec(
                (
                    "psql",
                    "-h",
                    "postgres",
                    "-U",
                    "postgres",
                    "-d",
                    "postgres",
                    "-c",
                    "SELECT 1",
                ),
                purpose=CommandPurpose.TEST,
                environment={"PGPASSWORD": "postgres"},
            ),
            TestEnvironmentSpec(
                services=(
                    ServiceSpec(
                        "postgresql",
                        POSTGRES_IMAGE,
                        "postgres",
                        {
                            "POSTGRES_USER": "postgres",
                            "POSTGRES_PASSWORD": "postgres",
                            "POSTGRES_DB": "postgres",
                        },
                        ("pg_isready", "-U", "postgres", "-d", "postgres"),
                    ),
                ),
            ),
            timeout_seconds=20,
        )

        self.assertTrue(execution.command_result.succeeded, execution.output_excerpt)
        self.assertEqual(execution.metadata["service_kinds"], ("postgresql",))
        self.assert_no_orchestrator_resources()

    def test_redis_health_connectivity_and_cleanup(self) -> None:
        execution = self.runtime.run_environment(
            REDIS_IMAGE,
            CommandSpec(
                ("redis-cli", "-h", "redis", "ping"),
                purpose=CommandPurpose.TEST,
            ),
            TestEnvironmentSpec(
                services=(
                    ServiceSpec(
                        "redis",
                        REDIS_IMAGE,
                        "redis",
                        healthcheck=("redis-cli", "ping"),
                    ),
                ),
            ),
            timeout_seconds=20,
        )

        self.assertTrue(execution.command_result.succeeded, execution.output_excerpt)
        self.assertIn("PONG", execution.output_excerpt)
        self.assert_no_orchestrator_resources()

    def assert_no_orchestrator_resources(self) -> None:
        containers = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}"],
            stdout=subprocess.PIPE,
            text=True,
            check=True,
        ).stdout.splitlines()
        networks = subprocess.run(
            ["docker", "network", "ls", "--format", "{{.Name}}"],
            stdout=subprocess.PIPE,
            text=True,
            check=True,
        ).stdout.splitlines()
        self.assertFalse(any(name.startswith("dprauto-service-") for name in containers))
        self.assertFalse(any(name.startswith("dprauto-test-") for name in networks))


if __name__ == "__main__":
    unittest.main()

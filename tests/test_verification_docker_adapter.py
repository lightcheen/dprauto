import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dprauto.adapters.storage import LocalArtifactStorage
from dprauto.adapters.verification.docker import DockerContainerRuntime
from dprauto.config import BuildConfig, VerificationConfig
from dprauto.domain.enums import CommandPurpose
from dprauto.domain.models import CommandSpec


class DockerVerificationAdapterTests(unittest.TestCase):
    def test_explicit_command_overrides_image_entrypoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = LocalArtifactStorage(Path(directory) / "artifacts")
            runtime = DockerContainerRuntime(
                storage,
                BuildConfig(docker_binary="docker"),
                VerificationConfig(
                    command_timeout_seconds=30,
                    docker_network="dprauto-eval",
                ),
            )
            calls = []

            def fake_run(argv, **kwargs):
                calls.append(list(argv))
                if "inspect" in argv and "--format" in argv:
                    return subprocess.CompletedProcess(argv, 0, stdout="0\n")
                if "start" in argv:
                    return subprocess.CompletedProcess(argv, 0, stdout=b"tests passed\n")
                return subprocess.CompletedProcess(argv, 0, stdout=b"")

            command = CommandSpec(
                ("python -m pytest -q",),
                purpose=CommandPurpose.TEST,
                shell=True,
            )
            with patch("dprauto.adapters.verification.docker.subprocess.run", side_effect=fake_run):
                execution = runtime.run_image(
                    "fixture:image-with-entrypoint",
                    command,
                    timeout_seconds=30,
                )

            create = calls[0]
            self.assertTrue(execution.command_result.succeeded)
            self.assertIn("--entrypoint", create)
            self.assertIn("--network", create)
            self.assertEqual(create[create.index("--network") + 1], "dprauto-eval")
            self.assertEqual(create.count("--env"), 6)
            self.assertIn("HTTP_PROXY", create)
            self.assertFalse(any("http://" in argument for argument in create))
            self.assertIn("--mount", create)
            self.assertEqual(
                create[create.index("--mount") + 1],
                "type=volume,source=dprauto-python-package-cache,target=/root/.cache",
            )
            self.assertLess(
                create.index("--entrypoint"),
                create.index("fixture:image-with-entrypoint"),
            )
            self.assertEqual(create[create.index("--entrypoint") + 1], "/bin/sh")

    def test_disabled_build_cache_is_not_mounted_for_verification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = DockerContainerRuntime(
                LocalArtifactStorage(Path(directory) / "artifacts"),
                BuildConfig(docker_binary="docker", use_cache=False),
            )
            calls = []

            def fake_run(argv, **kwargs):
                calls.append(list(argv))
                if "inspect" in argv and "--format" in argv:
                    return subprocess.CompletedProcess(argv, 0, stdout="0\n")
                return subprocess.CompletedProcess(argv, 0, stdout=b"")

            with patch(
                "dprauto.adapters.verification.docker.subprocess.run",
                side_effect=fake_run,
            ):
                runtime.run_image("fixture:image", None, timeout_seconds=30)

            self.assertNotIn("--mount", calls[0])

    def test_disabled_network_does_not_forward_proxy_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = DockerContainerRuntime(
                LocalArtifactStorage(Path(directory) / "artifacts"),
                BuildConfig(docker_binary="docker", allow_network=False),
            )
            calls = []

            def fake_run(argv, **kwargs):
                calls.append(list(argv))
                if "inspect" in argv and "--format" in argv:
                    return subprocess.CompletedProcess(argv, 0, stdout="0\n")
                return subprocess.CompletedProcess(argv, 0, stdout=b"")

            with patch(
                "dprauto.adapters.verification.docker.subprocess.run",
                side_effect=fake_run,
            ):
                runtime.run_image("fixture:image", None, timeout_seconds=30)

            self.assertNotIn("--env", calls[0])

    def test_web_probe_uses_configured_network(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = DockerContainerRuntime(
                LocalArtifactStorage(Path(directory) / "artifacts"),
                BuildConfig(docker_binary="docker"),
                VerificationConfig(docker_network="dprauto-eval"),
            )
            calls = []

            def fake_run(argv, **kwargs):
                calls.append(list(argv))
                return subprocess.CompletedProcess(argv, 1, stdout=b"create stopped")

            with patch(
                "dprauto.adapters.verification.docker.subprocess.run",
                side_effect=fake_run,
            ):
                runtime.probe_web(
                    "fixture:web",
                    None,
                    container_port=8000,
                    timeout_seconds=1,
                )

            create = calls[0]
            self.assertEqual(create[create.index("--network") + 1], "dprauto-eval")
            self.assertLess(create.index("--network"), create.index("fixture:web"))


if __name__ == "__main__":
    unittest.main()

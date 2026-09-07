import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from dprauto.adapters.storage import LocalArtifactStorage
from dprauto.adapters.verification.docker import DockerContainerRuntime
from dprauto.config import BuildConfig, VerificationConfig
from dprauto.domain.enums import CommandPurpose
from dprauto.domain.models import CommandSpec
from dprauto.ports.runtime import ServiceSpec, TestEnvironmentSpec


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
                cwd="modules/api",
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
            self.assertEqual(create[-2], "-c")
            self.assertEqual(
                create[create.index("--workdir") + 1],
                "/workspace/modules/api",
            )

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

    def test_service_environment_uses_isolated_network_and_cleans_every_resource(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = DockerContainerRuntime(
                LocalArtifactStorage(Path(directory) / "artifacts"),
                BuildConfig(docker_binary="docker", use_cache=False),
                VerificationConfig(service_startup_timeout_seconds=2),
            )
            calls = []

            def fake_run(argv, **kwargs):
                calls.append(list(argv))
                if len(argv) > 2 and argv[1] == "exec":
                    return subprocess.CompletedProcess(argv, 0, stdout=b"ready\n")
                if "inspect" in argv and "--format" in argv:
                    return subprocess.CompletedProcess(argv, 0, stdout="0\n")
                if argv[1:3] == ["start", "--attach"]:
                    return subprocess.CompletedProcess(argv, 0, stdout=b"tests passed\n")
                return subprocess.CompletedProcess(argv, 0, stdout=b"ok\n")

            service = ServiceSpec(
                "postgresql",
                "postgres:16-alpine",
                "postgres",
                {"POSTGRES_PASSWORD": "postgres"},
                ("pg_isready",),
            )
            command = CommandSpec(
                ("python", "-m", "unittest"),
                purpose=CommandPurpose.TEST,
            )
            with patch(
                "dprauto.adapters.verification.docker.subprocess.run",
                side_effect=fake_run,
            ):
                execution = runtime.run_environment(
                    "fixture:test",
                    command,
                    TestEnvironmentSpec(
                        services=(service,),
                        command_environment={"PGHOST": "postgres"},
                        required_executables=("psql",),
                    ),
                    timeout_seconds=30,
                )

            network_create = next(
                call for call in calls if call[1:3] == ["network", "create"]
            )
            network = network_create[-1]
            service_create = next(
                call
                for call in calls
                if call[1] == "create" and "postgres:16-alpine" in call
            )
            app_create = next(
                call for call in calls if call[1] == "create" and "fixture:test" in call
            )
            self.assertTrue(execution.command_result.succeeded)
            self.assertEqual(execution.metadata["service_kinds"], ("postgresql",))
            self.assertEqual(service_create[service_create.index("--network") + 1], network)
            self.assertEqual(app_create[app_create.index("--network") + 1], network)
            self.assertIn("PGHOST=postgres", app_create)
            self.assertIn("command -v psql", app_create[-1])
            self.assertFalse(any("--publish" in call for call in calls))
            self.assertTrue(
                any(call[1:3] == ["network", "rm"] and call[-1] == network for call in calls)
            )

    def test_executable_contract_is_checked_without_starting_a_service(self) -> None:
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
                return subprocess.CompletedProcess(argv, 0, stdout=b"ok\n")

            with patch(
                "dprauto.adapters.verification.docker.subprocess.run",
                side_effect=fake_run,
            ):
                execution = runtime.run_environment(
                    "fixture:test",
                    CommandSpec(("python", "-m", "unittest"), purpose=CommandPurpose.TEST),
                    TestEnvironmentSpec(required_executables=("tmux",)),
                    timeout_seconds=30,
                )

            create = calls[0]
            self.assertTrue(execution.command_result.succeeded)
            self.assertIn("command -v tmux", create[-1])
            self.assertNotIn("network", create)

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
                    CommandSpec(
                        ("./gradlew bootRun",),
                        purpose=CommandPurpose.RUN,
                        cwd="services/web",
                        shell=True,
                    ),
                    container_port=8000,
                    timeout_seconds=1,
                )

            create = calls[0]
            self.assertEqual(create[create.index("--network") + 1], "dprauto-eval")
            self.assertLess(create.index("--network"), create.index("fixture:web"))
            self.assertEqual(
                create[create.index("--workdir") + 1],
                "/workspace/services/web",
            )
            self.assertEqual(create[-2], "-c")

    def test_web_probe_uses_bridge_port_mapping_when_configured_network_is_host(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = DockerContainerRuntime(
                LocalArtifactStorage(Path(directory) / "artifacts"),
                BuildConfig(docker_binary="docker"),
                VerificationConfig(docker_network="host"),
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
                    CommandSpec(
                        ("python manage.py runserver 0.0.0.0:8000",),
                        purpose=CommandPurpose.RUN,
                        shell=True,
                    ),
                    container_port=8000,
                    timeout_seconds=1,
                )

            create = calls[0]
            self.assertIn("--publish", create)
            self.assertNotIn("--network", create)
            self.assertNotIn("host", create)

    def test_web_probe_disables_environment_proxies_for_local_http(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = DockerContainerRuntime(
                LocalArtifactStorage(Path(directory) / "artifacts"),
                BuildConfig(docker_binary="docker"),
            )

            def fake_run(argv, **kwargs):
                return subprocess.CompletedProcess(argv, 0, stdout=b"ok\n")

            response = MagicMock()
            response.status = 200
            response.__enter__.return_value = response
            opener = MagicMock()
            opener.open.side_effect = [ConnectionResetError(104, "reset"), response]
            with (
                patch(
                    "dprauto.adapters.verification.docker.subprocess.run",
                    side_effect=fake_run,
                ),
                patch.object(runtime, "_is_running", return_value=True),
                patch.object(runtime, "_port_open", return_value=True),
                patch(
                    "dprauto.adapters.verification.docker.urllib.request.build_opener",
                    return_value=opener,
                ) as build_opener,
            ):
                probe = runtime.probe_web(
                    "fixture:web",
                    CommandSpec(
                        ("python manage.py runserver 0.0.0.0:8000",),
                        purpose=CommandPurpose.RUN,
                        shell=True,
                    ),
                    container_port=8000,
                    timeout_seconds=1,
                )

            self.assertTrue(probe.http_reachable)
            self.assertEqual(probe.http_status, 200)
            self.assertEqual(build_opener.call_count, 2)
            self.assertEqual(opener.open.call_args.args[0].split(":", 1)[0], "http")


if __name__ == "__main__":
    unittest.main()

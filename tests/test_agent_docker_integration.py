import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from dprauto.adapters.python import PythonProjectParser
from dprauto.adapters.storage import LocalArtifactStorage
from dprauto.agent.state import create_agent_state
from dprauto.application import create_agent_workflow
from dprauto.config import AgentConfig, AppConfig, BuildConfig, StorageConfig
from dprauto.domain.enums import AgentPhase, BuildStatus, VerificationStatus
from dprauto.domain.models import SourceReference
from dprauto.ports.llm import LLMResponse


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


class ScriptedRepairLLM:
    def __init__(self, replacement):
        self.replacement = replacement
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        operation = request.metadata["operation"]
        if operation == "investigate_failure":
            return LLMResponse(
                "",
                structured={
                    "complete": True,
                    "rationale": "the Dockerfile and build log already prove the failure",
                    "actions": [],
                },
            )
        if operation == "analyze_failure":
            return LLMResponse(
                "",
                structured={"diagnosis": "Dockerfile has an intentionally failing RUN command"},
            )
        return LLMResponse(
            "",
            structured={
                "hypothesis": "restore the known-good deterministic Dockerfile",
                "summary": "replace only Dockerfile",
                "actions": [
                    {
                        "tool": "modify_build_script",
                        "arguments": {"path": "Dockerfile", "content": self.replacement},
                        "rationale": "remove the injected build-only failure",
                    }
                ],
            },
        )


@unittest.skipUnless(docker_ready(), f"Docker daemon and local {BASE_IMAGE} are required")
class AgentDockerIntegrationTests(unittest.TestCase):
    def repair_fixture(self, fixture, broken_dockerfile, fixed_dockerfile, expected):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            shutil.copytree(FIXTURES / fixture, workspace)
            (workspace / "Dockerfile").write_text(broken_dockerfile, encoding="utf-8")
            storage = LocalArtifactStorage(root / "runs")
            llm = ScriptedRepairLLM(fixed_dockerfile)
            config = AppConfig(
                build=BuildConfig(
                    timeout_seconds=120,
                    image_repository="dprauto-agent-integration",
                    python_base_image="python:{version}-slim",
                    # This integration specifically exercises the LLM repair
                    # path. Portfolio fallback has separate service tests.
                    strategy_portfolio_enabled=False,
                ),
                agent=AgentConfig(
                    max_attempts=3,
                    max_repeated_failures=2,
                    max_total_seconds=300,
                ),
                storage=StorageConfig(root=root / "runs"),
            )
            profile = PythonProjectParser().parse(
                SourceReference(f"fixture://agent/{fixture}"), workspace
            )
            state = create_agent_state(f"agent-{fixture}")
            state["project_profile"] = profile
            final = create_agent_workflow(storage, llm, config).run(state, workspace)
            image = final["build_plan"].metadata["image_reference"]
            self.addCleanup(
                subprocess.run,
                ["docker", "image", "rm", "--force", image],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )

            self.assertEqual(final["phase"], AgentPhase.COMPLETED)
            self.assertEqual(final["build_result"].status, BuildStatus.SUCCEEDED)
            self.assertTrue(final["verification_report"].succeeded)
            self.assertEqual(len(final["verification_results"]), 3)
            self.assertEqual(final["stop_reason"], "layered verification succeeded")
            self.assertEqual(final["attempt_number"], 1)
            self.assertEqual(len(final["environment_diffs"]), 1)
            self.assertFalse(final["environment_diff"].source_changed)
            self.assertEqual(
                final["repair_preflight"].status,
                VerificationStatus.PASSED,
            )
            self.assertEqual((workspace / "Dockerfile").read_text(), fixed_dockerfile)
            self.assertEqual(
                [request.metadata["operation"] for request in llm.requests],
                ["investigate_failure", "analyze_failure", "plan_fix"],
            )
            planning_payload = llm.requests[2].messages[1].content
            self.assertIn('"patch_system_packages"', planning_payload)
            self.assertIn('"patch_python_dependencies"', planning_payload)
            self.assertIn('"patch_base_image"', planning_payload)
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
            self.assertTrue(
                any(item.key.endswith("environment-diff.json") for item in final["artifacts"])
            )
            self.assertTrue(
                any(item.key.endswith("repair-preflight.json") for item in final["artifacts"])
            )

    def test_repairs_injected_errors_in_two_previously_successful_projects(self) -> None:
        cases = (
            (
                "docker_project",
                "FROM python:3.11-slim\nWORKDIR /workspace\nCOPY app.py /workspace/app.py\n"
                "RUN echo 'intentional agent failure' && exit 17\n",
                "FROM python:3.11-slim\nWORKDIR /workspace\nCOPY app.py /workspace/app.py\n"
                "RUN python -m compileall -q /workspace/app.py\n"
                'CMD ["python", "/workspace/app.py"]\n',
                "docker-project-ok",
            ),
            (
                "requirements_script",
                "FROM python:3.11-slim\nWORKDIR /workspace\nCOPY main.py /workspace/main.py\n"
                "RUN echo 'intentional agent failure' && exit 19\n",
                "FROM python:3.11-slim\nWORKDIR /workspace\nCOPY main.py /workspace/main.py\n"
                "RUN python -m compileall -q /workspace/main.py\n"
                'CMD ["python", "/workspace/main.py"]\n',
                "requirements-script-ok",
            ),
        )
        for fixture, broken, fixed, expected in cases:
            with self.subTest(fixture=fixture):
                self.repair_fixture(fixture, broken, fixed, expected)


if __name__ == "__main__":
    unittest.main()

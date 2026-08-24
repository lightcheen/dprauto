import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from dprauto.adapters.storage import LocalArtifactStorage
from dprauto.agent.candidates import RepairCandidateManager
from dprauto.domain.enums import BuildStage, ChangeKind, CommandPurpose
from dprauto.domain.models import (
    BuildPlan,
    BuildStep,
    CommandSpec,
    EnvironmentDiff,
    FileChange,
    GeneratedFile,
)


def digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class RepairCandidateManagerTests(unittest.TestCase):
    @staticmethod
    def generated_plan() -> BuildPlan:
        command = CommandSpec(("docker", "build", "."), purpose=CommandPurpose.BUILD)
        return BuildPlan(
            "generated-plan",
            "generated-project",
            "template",
            (BuildStep("docker-build", BuildStage.BUILD, command),),
            generated_files=(
                GeneratedFile("Dockerfile", "FROM python:3.11-slim\n"),
                GeneratedFile(
                    "setup.sh",
                    "#!/bin/sh\npython -m pip install .\n",
                    executable=True,
                ),
            ),
        )

    def test_create_materializes_generated_files_without_replacing_project_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            project_setup = "#!/bin/sh\necho project-owned\n"
            (workspace / "setup.sh").write_text(project_setup, encoding="utf-8")
            storage = LocalArtifactStorage(workspace / "artifacts")
            manager = RepairCandidateManager(storage)
            self.addCleanup(manager.close)

            candidate = manager.create(
                {"workspace": str(workspace), "build_plan": self.generated_plan()},
                1,
            )
            self.addCleanup(manager.cleanup, candidate)

            self.assertEqual(candidate.materialized_generated_files, ("Dockerfile",))
            self.assertEqual(
                Path(candidate.workspace, "Dockerfile").read_text(encoding="utf-8"),
                "FROM python:3.11-slim\n",
            )
            self.assertEqual(
                Path(candidate.workspace, "setup.sh").read_text(encoding="utf-8"),
                project_setup,
            )
            self.assertFalse((workspace / "Dockerfile").exists())

    def test_accept_promotes_modified_and_unchanged_generated_build_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            storage = LocalArtifactStorage(workspace / "artifacts")
            manager = RepairCandidateManager(storage)
            self.addCleanup(manager.close)
            plan = self.generated_plan()
            candidate = manager.create(
                {"workspace": str(workspace), "build_plan": plan},
                1,
            )
            docker_before = plan.generated_files[0].content
            docker_after = docker_before + 'CMD ["python", "-V"]\n'
            Path(candidate.workspace, "Dockerfile").write_text(
                docker_after,
                encoding="utf-8",
            )
            docker_change = FileChange(
                "Dockerfile",
                ChangeKind.MODIFIED,
                digest(docker_before),
                digest(docker_after),
            )
            diff = EnvironmentDiff(
                files=(docker_change,),
                build_scripts=(docker_change,),
            )
            candidate = manager.with_diff(candidate, diff, diff)

            accepted = manager.accept(candidate)

            self.assertEqual(accepted.status, "accepted")
            self.assertEqual(
                (workspace / "Dockerfile").read_text(encoding="utf-8"),
                docker_after,
            )
            self.assertEqual(
                (workspace / "setup.sh").read_text(encoding="utf-8"),
                plan.generated_files[1].content,
            )
            self.assertTrue(os.stat(workspace / "setup.sh").st_mode & 0o100)

    def test_reject_discards_materialized_generated_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            storage = LocalArtifactStorage(workspace / "artifacts")
            manager = RepairCandidateManager(storage)
            self.addCleanup(manager.close)
            candidate = manager.create(
                {"workspace": str(workspace), "build_plan": self.generated_plan()},
                1,
            )

            rejected = manager.reject(candidate, "test rejection")

            self.assertEqual(rejected.status, "rejected")
            self.assertFalse((workspace / "Dockerfile").exists())
            self.assertFalse((workspace / "setup.sh").exists())

    def test_multiround_candidate_promotes_the_entire_candidate_chain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            docker_before = "FROM python:3.11-slim\n"
            setup_before = "python -m pip install .\n"
            docker_after = docker_before + "RUN apt-get update\n"
            setup_after = setup_before + "python -m pytest\n"
            (workspace / "Dockerfile").write_text(docker_before, encoding="utf-8")
            (workspace / "setup.sh").write_text(setup_before, encoding="utf-8")
            storage = LocalArtifactStorage(workspace / "artifacts")
            manager = RepairCandidateManager(storage)
            self.addCleanup(manager.close)

            first = manager.create({"workspace": str(workspace)}, 1)
            Path(first.workspace, "Dockerfile").write_text(
                docker_after, encoding="utf-8"
            )
            docker_change = FileChange(
                "Dockerfile",
                ChangeKind.MODIFIED,
                digest(docker_before),
                digest(docker_after),
            )
            first_diff = EnvironmentDiff(
                files=(docker_change,), build_scripts=(docker_change,)
            )
            first = manager.with_diff(first, first_diff, first_diff)

            second = manager.create(
                {"workspace": str(workspace), "repair_candidate": first}, 2
            )
            manager.cleanup(first)
            Path(second.workspace, "setup.sh").write_text(
                setup_after, encoding="utf-8"
            )
            setup_change = FileChange(
                "setup.sh",
                ChangeKind.MODIFIED,
                digest(setup_before),
                digest(setup_after),
            )
            second_diff = EnvironmentDiff(
                files=(setup_change,), build_scripts=(setup_change,)
            )
            cumulative = EnvironmentDiff(
                files=(docker_change, setup_change),
                build_scripts=(docker_change, setup_change),
            )
            second = manager.with_diff(second, second_diff, cumulative)

            accepted = manager.accept(second)

            self.assertEqual(accepted.status, "accepted")
            self.assertEqual(
                (workspace / "Dockerfile").read_text(encoding="utf-8"), docker_after
            )
            self.assertEqual(
                (workspace / "setup.sh").read_text(encoding="utf-8"), setup_after
            )


if __name__ == "__main__":
    unittest.main()

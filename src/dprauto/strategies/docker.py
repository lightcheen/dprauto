"""Strategy for repositories that already provide a Dockerfile."""

from __future__ import annotations

from datetime import datetime
from pathlib import PurePosixPath

from dprauto.config import BuildConfig
from dprauto.domain.enums import BuildStage, CommandPurpose
from dprauto.domain.models import BuildPlan, BuildResult, BuildStep, CommandSpec, ProjectProfile
from dprauto.proxy import docker_proxy_build_arguments
from dprauto.strategies.common import (
    RecordedBuildRunner,
    stable_image_reference,
    stable_plan_id,
)


class DockerStrategy:
    name = "docker"

    def __init__(self, runner: RecordedBuildRunner, config: BuildConfig | None = None) -> None:
        self.runner = runner
        self.config = config or BuildConfig()

    def supports(self, profile: ProjectProfile) -> bool:
        return bool(profile.dockerfiles)

    def create_plan(self, profile: ProjectProfile) -> BuildPlan:
        dockerfile = min(
            profile.dockerfiles,
            key=lambda path: (
                PurePosixPath(path).name.lower() != "dockerfile",
                len(PurePosixPath(path).parts),
                path,
            ),
        )
        image = stable_image_reference(profile, self.config.image_repository)
        argv = [
            self.config.docker_binary,
            "build",
            "--file",
            dockerfile,
            "--tag",
            image,
        ]
        if not self.config.use_cache:
            argv.append("--no-cache")
        if not self.config.allow_network:
            argv.extend(("--network", "none"))
        elif self.config.docker_network:
            argv.extend(("--network", self.config.docker_network))
        argv.extend(docker_proxy_build_arguments(self.config))
        argv.append(".")
        command = CommandSpec(
            tuple(argv),
            purpose=CommandPurpose.BUILD,
            timeout_seconds=self.config.timeout_seconds,
        )
        return BuildPlan(
            plan_id=stable_plan_id(self.name, profile, command),
            project_id=profile.project_id,
            strategy=self.name,
            steps=(BuildStep("docker-build", BuildStage.BUILD, command),),
            network_allowed=self.config.allow_network,
            cache_enabled=self.config.use_cache,
            metadata={
                "image_reference": image,
                "dockerfile": dockerfile,
                "proxy_environment_forwarded": (
                    self.config.allow_network
                    and self.config.forward_proxy_environment
                ),
            },
            source_files=(dockerfile,),
        )

    def build(
        self,
        plan: BuildPlan,
        workspace,
        *,
        deadline_at: datetime | None = None,
    ) -> BuildResult:
        return self.runner.run(plan, workspace, deadline_at=deadline_at)

"""Minimal Pack/CNB strategy with a fixed, recorded builder."""

from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import shutil

from dprauto.config import BuildConfig
from dprauto.domain.enums import BuildStage, CommandPurpose
from dprauto.domain.models import BuildPlan, BuildResult, BuildStep, CommandSpec, ProjectProfile
from dprauto.proxy import cleared_proxy_environment
from dprauto.strategies.common import (
    RecordedBuildRunner,
    stable_image_reference,
    stable_plan_id,
)


class CNBStrategy:
    name = "cnb"

    def __init__(self, runner: RecordedBuildRunner, config: BuildConfig | None = None) -> None:
        self.runner = runner
        self.config = config or BuildConfig()

    def supports(self, profile: ProjectProfile) -> bool:
        standard_names = {"pyproject.toml", "setup.py", "setup.cfg", "requirements.txt"}
        return any(language.lower() == "python" for language in profile.languages) and bool(
            standard_names & {path.rsplit("/", 1)[-1].lower() for path in profile.dependency_files}
        )

    def available(self) -> bool:
        """Return whether Pack can actually be executed in this deployment."""

        binary = self.config.pack_binary
        if "/" in binary:
            path = Path(binary).expanduser()
            return path.is_file() and os.access(path, os.X_OK)
        return shutil.which(binary) is not None

    def create_plan(self, profile: ProjectProfile) -> BuildPlan:
        image = stable_image_reference(profile, self.config.image_repository)
        argv = [
            self.config.pack_binary,
            "build",
            image,
            "--path",
            ".",
            "--builder",
            self.config.cnb_builder,
            "--pull-policy",
            "if-not-present",
        ]
        if self.config.cnb_lifecycle_image:
            argv.extend(("--lifecycle-image", self.config.cnb_lifecycle_image))
        if not self.config.use_cache:
            argv.append("--clear-cache")
        network = "none" if not self.config.allow_network else self.config.docker_network
        if network:
            argv.extend(("--network", network))
        command = CommandSpec(
            tuple(argv),
            purpose=CommandPurpose.BUILD,
            timeout_seconds=self.config.timeout_seconds,
            environment=cleared_proxy_environment(self.config),
        )
        return BuildPlan(
            plan_id=stable_plan_id(self.name, profile, command),
            project_id=profile.project_id,
            strategy=self.name,
            steps=(BuildStep("pack-build", BuildStage.BUILD, command),),
            network_allowed=self.config.allow_network,
            cache_enabled=self.config.use_cache,
            metadata={
                "image_reference": image,
                "builder": self.config.cnb_builder,
                "lifecycle_image": self.config.cnb_lifecycle_image,
                "metric": "installability",
                "proxy_environment_forwarded": (
                    self.config.allow_network
                    and self.config.forward_proxy_environment
                ),
            },
        )

    def build(
        self,
        plan: BuildPlan,
        workspace,
        *,
        deadline_at: datetime | None = None,
    ) -> BuildResult:
        return self.runner.run(plan, workspace, deadline_at=deadline_at)

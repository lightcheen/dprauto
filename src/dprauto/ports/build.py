"""Build planning and execution port."""

from pathlib import Path
from datetime import datetime
from typing import Protocol, runtime_checkable

from dprauto.domain.models import BuildPlan, BuildResult, ProjectProfile


@runtime_checkable
class BuildStrategy(Protocol):
    def supports(self, profile: ProjectProfile) -> bool:
        """Return whether this strategy can handle the project."""
        ...

    def create_plan(self, profile: ProjectProfile) -> BuildPlan:
        """Create a replayable build plan without executing it."""
        ...

    def build(
        self,
        plan: BuildPlan,
        workspace: Path,
        *,
        deadline_at: datetime | None = None,
    ) -> BuildResult:
        """Execute an already-recorded plan in the given workspace."""
        ...

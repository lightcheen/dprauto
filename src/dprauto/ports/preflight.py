"""Port for cheap validation of an isolated repair candidate."""

from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from dprauto.domain.models import EnvironmentDiff, RepairPreflightResult


@runtime_checkable
class RepairPreflightRunner(Protocol):
    def run(
        self,
        workspace: Path,
        environment_diff: EnvironmentDiff,
        *,
        deadline_at: datetime | None = None,
    ) -> RepairPreflightResult:
        """Validate changed build scripts without performing a full build."""

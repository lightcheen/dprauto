"""Workspace environment snapshot and comparison port."""

from pathlib import Path
from typing import Protocol, runtime_checkable

from dprauto.domain.models import EnvironmentDiff, EnvironmentSnapshot, ProjectProfile


@runtime_checkable
class EnvironmentDiffer(Protocol):
    def snapshot(self, profile: ProjectProfile, workspace: Path) -> EnvironmentSnapshot:
        ...

    def compare(
        self,
        before: EnvironmentSnapshot,
        after: EnvironmentSnapshot,
    ) -> EnvironmentDiff:
        ...

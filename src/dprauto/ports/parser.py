"""Project inspection port."""

from pathlib import Path
from typing import Protocol, runtime_checkable

from dprauto.domain.models import ProjectProfile, SourceReference


@runtime_checkable
class ProjectParser(Protocol):
    def parse(self, source: SourceReference, workspace: Path) -> ProjectProfile:
        """Inspect a source tree and return a normalized profile."""
        ...

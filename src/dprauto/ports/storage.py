"""Artifact persistence port."""

from typing import Protocol, runtime_checkable

from dprauto.domain.models import ArtifactRef


@runtime_checkable
class Storage(Protocol):
    def save(
        self,
        key: str,
        content: bytes,
        *,
        media_type: str = "application/octet-stream",
    ) -> ArtifactRef:
        """Persist immutable artifact content and return its reference."""
        ...

    def load(self, artifact: ArtifactRef) -> bytes:
        """Load artifact content by reference."""
        ...

    def exists(self, artifact: ArtifactRef) -> bool:
        """Return whether the artifact still exists."""
        ...

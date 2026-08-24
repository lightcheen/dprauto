"""Filesystem-backed immutable artifact storage."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path, PurePosixPath

from dprauto.domain.models import ArtifactRef
from dprauto.errors import StorageError


class LocalArtifactStorage:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def save(
        self,
        key: str,
        content: bytes,
        *,
        media_type: str = "application/octet-stream",
    ) -> ArtifactRef:
        target = self._target(key)
        digest = hashlib.sha256(content).hexdigest()
        if target.exists():
            existing = target.read_bytes()
            if existing != content:
                raise StorageError(f"artifact key already contains different content: {key}")
            return ArtifactRef(key, digest=digest, media_type=media_type, size_bytes=len(content))

        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".dprauto-", dir=target.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(target)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise StorageError(f"failed to save artifact {key}: {exc}") from exc
        return ArtifactRef(key, digest=digest, media_type=media_type, size_bytes=len(content))

    def load(self, artifact: ArtifactRef) -> bytes:
        target = self._target(artifact.key)
        try:
            content = target.read_bytes()
        except OSError as exc:
            raise StorageError(f"failed to load artifact {artifact.key}: {exc}") from exc
        if artifact.digest and hashlib.sha256(content).hexdigest() != artifact.digest:
            raise StorageError(f"artifact digest mismatch: {artifact.key}")
        return content

    def exists(self, artifact: ArtifactRef) -> bool:
        try:
            return self._target(artifact.key).is_file()
        except StorageError:
            return False

    def path_for(self, artifact: ArtifactRef) -> Path:
        """Return a local path for infrastructure adapters that require one."""

        target = self._target(artifact.key)
        if not target.is_file():
            raise StorageError(f"artifact does not exist: {artifact.key}")
        return target

    def _target(self, key: str) -> Path:
        path = PurePosixPath(key)
        if not key.strip() or path.is_absolute() or ".." in path.parts or path.as_posix() == ".":
            raise StorageError(f"artifact key must be a safe relative path: {key!r}")
        target = (self.root / path.as_posix()).resolve()
        if self.root not in target.parents:
            raise StorageError(f"artifact key escapes storage root: {key!r}")
        return target

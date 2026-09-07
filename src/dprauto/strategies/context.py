"""Shared source-context policy for generated Dockerfiles."""

from __future__ import annotations

from dprauto.domain.models import GeneratedFile, ProjectProfile


_BASE_EXCLUDES = (
    ".dprauto",
    ".cnb-benchmark-source-ready",
    "**/__pycache__",
    "**/.pytest_cache",
    "**/.mypy_cache",
)


def requires_vcs_metadata(profile: ProjectProfile) -> bool:
    """Return whether repository evidence says the build reads VCS metadata."""

    return profile.metadata.get("vcs_metadata_required") is True


def generated_dockerignore(profile: ProjectProfile) -> GeneratedFile:
    """Keep project sources/tests while excluding only disposable context data."""

    excludes = list(_BASE_EXCLUDES)
    if not requires_vcs_metadata(profile):
        excludes.insert(0, ".git")
    evidence = profile.metadata.get("vcs_metadata_evidence", ())
    comments = ["# DPRAuto generated source-context contract"]
    if requires_vcs_metadata(profile):
        comments.append("# .git retained: " + ", ".join(str(item) for item in evidence[:4]))
    return GeneratedFile(
        "Dockerfile.dockerignore",
        "\n".join((*comments, *excludes)) + "\n",
        media_type="text/plain",
    )

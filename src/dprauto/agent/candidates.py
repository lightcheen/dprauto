"""Isolated repair workspaces and atomic promotion into the accepted tree."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from dataclasses import replace
from pathlib import Path, PurePosixPath
from uuid import uuid4

from dprauto.agent.models import RepairCandidate
from dprauto.agent.state import AgentState
from dprauto.domain.enums import ChangeKind
from dprauto.domain.models import BuildPlan, EnvironmentDiff, FileChange
from dprauto.errors import AgentWorkflowError
from dprauto.ports.storage import Storage


class RepairCandidateManager:
    """Own candidate directories and commit only an explicitly accepted diff."""

    def __init__(self, storage: Storage) -> None:
        self._root = Path(tempfile.mkdtemp(prefix="dprauto-candidates-")).resolve()
        storage_root = getattr(storage, "root", None)
        self._storage_root = (
            Path(storage_root).expanduser().resolve() if storage_root is not None else None
        )

    def create(self, state: AgentState, attempt_number: int) -> RepairCandidate:
        accepted_workspace = Path(state["workspace"]).expanduser().resolve()
        parent = state.get("repair_candidate")
        if parent is not None and parent.status == "pending":
            source = Path(parent.workspace).resolve()
            rollback = parent
            parent_id = parent.candidate_id
        else:
            source = accepted_workspace
            rollback = None
            parent_id = None
        if not source.is_dir():
            raise AgentWorkflowError(f"repair candidate source is not a directory: {source}")

        candidate_id = f"{attempt_number:04d}-{uuid4().hex}"
        target = self._root / candidate_id
        try:
            shutil.copytree(
                source,
                target,
                symlinks=True,
                ignore=self._ignore_for(source),
            )
        except OSError as exc:
            shutil.rmtree(target, ignore_errors=True)
            raise AgentWorkflowError(f"failed to create isolated repair candidate: {exc}") from exc

        try:
            materialized_generated_files = (
                rollback.materialized_generated_files
                if rollback is not None
                else self._materialize_generated_files(target, state.get("build_plan"))
            )
        except (OSError, AgentWorkflowError) as exc:
            shutil.rmtree(target, ignore_errors=True)
            if isinstance(exc, AgentWorkflowError):
                raise
            raise AgentWorkflowError(
                f"failed to materialize generated build files: {exc}"
            ) from exc

        return RepairCandidate(
            candidate_id=candidate_id,
            attempt_number=attempt_number,
            workspace=str(target),
            accepted_workspace=str(accepted_workspace),
            parent_candidate_id=parent_id,
            materialized_generated_files=materialized_generated_files,
            accepted_project_profile=(
                rollback.accepted_project_profile
                if rollback is not None
                else state.get("project_profile")
            ),
            accepted_build_plan=(
                rollback.accepted_build_plan
                if rollback is not None
                else state.get("build_plan")
            ),
            accepted_build_result=(
                rollback.accepted_build_result
                if rollback is not None
                else state.get("build_result")
            ),
            accepted_failure=(
                rollback.accepted_failure if rollback is not None else state.get("failure")
            ),
            accepted_verification_report=(
                rollback.accepted_verification_report
                if rollback is not None
                else state.get("verification_report")
            ),
            accepted_verification_results=(
                rollback.accepted_verification_results
                if rollback is not None
                else state.get("verification_results", ())
            ),
            accepted_regression_result=(
                rollback.accepted_regression_result
                if rollback is not None
                else state.get("regression_result")
            ),
            cumulative_environment_diff=(
                rollback.cumulative_environment_diff or rollback.environment_diff
                if rollback is not None
                else None
            ),
        )

    def with_diff(
        self,
        candidate: RepairCandidate,
        environment_diff: EnvironmentDiff,
        cumulative_environment_diff: EnvironmentDiff,
    ) -> RepairCandidate:
        return replace(
            candidate,
            environment_diff=environment_diff,
            cumulative_environment_diff=cumulative_environment_diff,
        )

    def accept(self, candidate: RepairCandidate) -> RepairCandidate:
        diff = candidate.cumulative_environment_diff or candidate.environment_diff
        if diff is None or diff.is_empty:
            raise AgentWorkflowError("cannot accept a repair candidate without a change")
        source = Path(candidate.workspace).resolve()
        target = Path(candidate.accepted_workspace).resolve()
        promotion_diff = self._promotion_diff(candidate, source, target, diff)
        self._promote_files(source, target, promotion_diff)
        accepted = replace(
            candidate,
            status="accepted",
            workspace=str(target),
            disposition_reason="candidate improved the accepted environment",
        )
        self.cleanup(candidate)
        return accepted

    def reject(self, candidate: RepairCandidate, reason: str) -> RepairCandidate:
        rejected = replace(candidate, status="rejected", disposition_reason=reason)
        self.cleanup(candidate)
        return rejected

    def cleanup(self, candidate: RepairCandidate) -> None:
        path = Path(candidate.workspace).resolve()
        temporary_root = Path(tempfile.gettempdir()).resolve()
        owned_parent = (
            path.parent == self._root
            or (
                path.parent.parent == temporary_root
                and path.parent.name.startswith("dprauto-candidates-")
            )
        )
        if owned_parent and path.name.startswith(f"{candidate.attempt_number:04d}-"):
            shutil.rmtree(path, ignore_errors=True)
            try:
                path.parent.rmdir()
            except OSError:
                pass

    def close(self) -> None:
        # A pending candidate may be referenced by a durable LangGraph
        # checkpoint and must survive process shutdown for resume().
        try:
            self._root.rmdir()
        except OSError:
            pass

    def _ignore_for(self, source: Path):
        excluded_relative: Path | None = None
        if self._storage_root is not None:
            try:
                excluded_relative = self._storage_root.relative_to(source)
            except ValueError:
                excluded_relative = None

        def ignore(directory: str, names: list[str]) -> set[str]:
            current = Path(directory).resolve().relative_to(source)
            ignored: set[str] = set()
            if excluded_relative is not None:
                for name in names:
                    relative = current / name
                    if relative == excluded_relative:
                        ignored.add(name)
            return ignored

        return ignore

    @staticmethod
    def _materialize_generated_files(
        workspace: Path,
        plan: BuildPlan | None,
    ) -> tuple[str, ...]:
        """Write generated build inputs into a candidate without replacing project files.

        Generated files used to live only in ``BuildPlan`` and in the build runner's
        temporary directory.  Repair tools consequently could see their contents in
        LLM context but could not edit them in the candidate workspace.  Materializing
        them before the pre-mutation snapshot gives tools and the environment differ a
        single, shared baseline.

        A project-owned file always wins a path collision.  This matters for template
        plans that expose a generated ``setup.sh`` for diagnostics while the repository
        already contains its own setup script.
        """

        if plan is None:
            return ()
        root = workspace.resolve()
        materialized: list[str] = []
        for generated in plan.generated_files:
            relative = PurePosixPath(generated.path)
            if relative.is_absolute() or ".." in relative.parts:
                raise AgentWorkflowError(
                    f"generated build file has an unsafe path: {generated.path}"
                )
            target = root / relative.as_posix()
            current = root
            for part in relative.parts[:-1]:
                current = current / part
                if current.is_symlink():
                    raise AgentWorkflowError(
                        f"generated build file path contains a symlink: {generated.path}"
                    )
            resolved_parent = target.parent.resolve()
            if resolved_parent != root and root not in resolved_parent.parents:
                raise AgentWorkflowError(
                    f"generated build file escapes candidate workspace: {generated.path}"
                )
            if target.exists() or target.is_symlink():
                if not target.is_file() or target.is_symlink():
                    raise AgentWorkflowError(
                        f"generated build file conflicts with a non-file path: {generated.path}"
                    )
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(generated.content, encoding="utf-8")
            if generated.executable:
                target.chmod(0o700)
            materialized.append(relative.as_posix())
        return tuple(materialized)

    @staticmethod
    def _promotion_diff(
        candidate: RepairCandidate,
        source: Path,
        target: Path,
        diff: EnvironmentDiff,
    ) -> EnvironmentDiff:
        """Include every materialized build input in an accepted candidate commit."""

        changes = {
            change.path: change
            for change in (*diff.files, *diff.build_scripts, *diff.business_source)
        }
        for value in candidate.materialized_generated_files:
            destination = target / value
            if destination.exists() or destination.is_symlink():
                raise AgentWorkflowError(
                    "accepted workspace changed while a generated build file was pending: "
                    f"{value}"
                )
            candidate_file = source / value
            if not candidate_file.is_file() or candidate_file.is_symlink():
                raise AgentWorkflowError(
                    f"materialized generated build file is missing or unsafe: {value}"
                )
            changes[value] = FileChange(
                value,
                ChangeKind.ADDED,
                None,
                hashlib.sha256(candidate_file.read_bytes()).hexdigest(),
            )
        return EnvironmentDiff(files=tuple(changes.values()))

    @staticmethod
    def _promote_files(source: Path, target: Path, diff: EnvironmentDiff) -> None:
        changes = {
            change.path: change
            for change in (*diff.files, *diff.build_scripts, *diff.business_source)
        }
        backups: dict[Path, tuple[bytes | None, int | None]] = {}
        try:
            for value, change in changes.items():
                relative = PurePosixPath(value)
                if relative.is_absolute() or ".." in relative.parts:
                    raise AgentWorkflowError(
                        f"repair candidate contains an unsafe changed path: {value}"
                    )
                destination = target / relative.as_posix()
                resolved_parent = destination.parent.resolve()
                if resolved_parent != target and target not in resolved_parent.parents:
                    raise AgentWorkflowError(
                        f"repair candidate path escapes the accepted workspace: {value}"
                    )
                if destination.is_symlink():
                    raise AgentWorkflowError(
                        f"repair candidate cannot replace a symlink: {value}"
                    )
                if destination.exists() and not destination.is_file():
                    raise AgentWorkflowError(
                        f"repair candidate cannot replace a non-file path: {value}"
                    )
                backups[destination] = (
                    destination.read_bytes() if destination.is_file() else None,
                    destination.stat().st_mode if destination.exists() else None,
                )
                if change.kind is ChangeKind.REMOVED:
                    destination.unlink(missing_ok=True)
                    continue
                candidate_file = source / relative.as_posix()
                if not candidate_file.is_file() or candidate_file.is_symlink():
                    raise AgentWorkflowError(
                        f"accepted candidate file is missing or unsafe: {value}"
                    )
                resolved_candidate = candidate_file.resolve()
                if source not in resolved_candidate.parents:
                    raise AgentWorkflowError(
                        f"accepted candidate file escapes its workspace: {value}"
                    )
                content = candidate_file.read_bytes()
                if change.after_digest is not None:
                    digest = hashlib.sha256(content).hexdigest()
                    if digest != change.after_digest:
                        raise AgentWorkflowError(
                            f"accepted candidate digest does not match its diff: {value}"
                        )
                destination.parent.mkdir(parents=True, exist_ok=True)
                descriptor, temporary_name = tempfile.mkstemp(
                    prefix=".dprauto-promote-", dir=destination.parent
                )
                temporary = Path(temporary_name)
                try:
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(content)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.chmod(temporary, candidate_file.stat().st_mode)
                    temporary.replace(destination)
                except BaseException:
                    temporary.unlink(missing_ok=True)
                    raise
        except BaseException:
            for destination, (content, mode) in reversed(tuple(backups.items())):
                if content is None:
                    destination.unlink(missing_ok=True)
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
                if mode is not None:
                    os.chmod(destination, mode)
            raise

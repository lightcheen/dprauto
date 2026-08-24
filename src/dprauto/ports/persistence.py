"""Agent checkpoint and complete repair-history persistence port."""

from typing import Any, Protocol, runtime_checkable

from dprauto.agent.models import AttemptedMethod, RepairRecord
from dprauto.domain.models import ArtifactRef


@runtime_checkable
class AgentPersistence(Protocol):
    @property
    def checkpointer(self) -> Any:
        """Return a LangGraph-compatible durable checkpointer."""
        ...

    def record(self, repair: RepairRecord) -> ArtifactRef:
        """Idempotently persist one complete repair round."""
        ...

    def was_attempted(self, run_id: str, method_fingerprint: str) -> bool:
        """Return whether this exact repair method already ran in this thread."""
        ...

    def recent_failed_methods(self, run_id: str, limit: int) -> tuple[AttemptedMethod, ...]:
        """Load compact failed-method memories; full records remain external."""
        ...

    def checkpoint_count(self, run_id: str) -> int:
        """Return the number of LangGraph checkpoints for a thread."""
        ...

    def close(self) -> None:
        """Release persistence resources."""
        ...

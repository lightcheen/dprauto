"""SQLite LangGraph checkpoints plus indexed external repair history."""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from dprauto.agent.models import AttemptedMethod, RepairRecord
from dprauto.domain.models import ArtifactRef
from dprauto.errors import StorageError
from dprauto.ports.storage import Storage
from dprauto.serialization import to_json_bytes


class SQLiteAgentPersistence:
    """Lightweight synchronous persistence for one local Agent process."""

    def __init__(self, database: Path, storage: Storage) -> None:
        self.database = database.expanduser().resolve()
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.database, check_same_thread=False)
        self._checkpoint_connection = sqlite3.connect(
            self.database,
            check_same_thread=False,
        )
        os.chmod(self.database, 0o600)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._checkpoint_connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.RLock()
        self._storage = storage
        serializer = JsonPlusSerializer(
            allowed_msgpack_modules=(
                ("dprauto.agent.models", "AttemptedMethod"),
                ("dprauto.agent.models", "ContextSummary"),
                ("dprauto.agent.models", "FixPlan"),
                ("dprauto.agent.models", "RepairCandidate"),
                ("dprauto.agent.models", "ToolCall"),
                ("dprauto.agent.models", "ToolResult"),
                ("dprauto.application.build", "BuildStrategyAttempt"),
                ("dprauto.domain.enums", "AgentPhase"),
                ("dprauto.domain.enums", "BuildFailureKind"),
                ("dprauto.domain.enums", "BuildStage"),
                ("dprauto.domain.enums", "BuildStatus"),
                ("dprauto.domain.enums", "ChangeKind"),
                ("dprauto.domain.enums", "CommandPurpose"),
                ("dprauto.domain.enums", "FailureCategory"),
                ("dprauto.domain.enums", "EnvironmentBuildStatus"),
                ("dprauto.domain.enums", "ProjectType"),
                ("dprauto.domain.enums", "RegressionStatus"),
                ("dprauto.domain.enums", "RiskLevel"),
                ("dprauto.domain.enums", "VerificationLevel"),
                ("dprauto.domain.enums", "VerificationStatus"),
                ("dprauto.domain.models", "ArtifactRef"),
                ("dprauto.domain.models", "BuildPlan"),
                ("dprauto.domain.models", "BuildResult"),
                ("dprauto.domain.models", "BuildStep"),
                ("dprauto.domain.models", "CommandResult"),
                ("dprauto.domain.models", "CommandSpec"),
                ("dprauto.domain.models", "DependencyChange"),
                ("dprauto.domain.models", "EnvironmentDiff"),
                ("dprauto.domain.models", "EnvironmentBuildResult"),
                ("dprauto.domain.models", "BuildScriptSnapshot"),
                ("dprauto.domain.models", "EnvironmentSnapshot"),
                ("dprauto.domain.models", "FailureInfo"),
                ("dprauto.domain.models", "FileChange"),
                ("dprauto.domain.models", "GeneratedFile"),
                ("dprauto.domain.models", "ProjectCommand"),
                ("dprauto.domain.models", "ProjectProfile"),
                ("dprauto.domain.models", "RepairPreflightCheck"),
                ("dprauto.domain.models", "RepairPreflightResult"),
                ("dprauto.domain.models", "RegressionBaseline"),
                ("dprauto.domain.models", "RegressionExpectation"),
                ("dprauto.domain.models", "RegressionFinding"),
                ("dprauto.domain.models", "RegressionResult"),
                ("dprauto.domain.models", "SourceReference"),
                ("dprauto.domain.models", "ValueChange"),
                ("dprauto.domain.models", "VerificationCheck"),
                ("dprauto.domain.models", "VerificationReport"),
                ("dprauto.domain.models", "VerificationResult"),
            )
        )
        self._checkpointer = SqliteSaver(
            self._checkpoint_connection,
            serde=serializer,
        )
        self._setup_history()
        self._closed = False

    @property
    def checkpointer(self) -> SqliteSaver:
        return self._checkpointer

    def record(self, repair: RepairRecord) -> ArtifactRef:
        key = (
            f"agent-runs/{repair.run_id}/history/"
            f"{repair.attempt_number:04d}-{repair.method_fingerprint}.json"
        )
        artifact = self._storage.save(
            key,
            to_json_bytes(repair),
            media_type="application/json",
        )
        failure_fingerprint = (
            repair.failure_after.fingerprint if repair.failure_after is not None else ""
        )
        with self._lock, self._connection:
            existing = self._connection.execute(
                "SELECT method_fingerprint, record_key FROM repair_history "
                "WHERE run_id = ? AND attempt_number = ?",
                (repair.run_id, repair.attempt_number),
            ).fetchone()
            if existing and existing != (repair.method_fingerprint, artifact.key):
                raise StorageError(
                    "repair history attempt already contains a different record: "
                    f"{repair.run_id}/{repair.attempt_number}"
                )
            if existing:
                return artifact
            try:
                self._connection.execute(
                    "INSERT INTO repair_history "
                    "(run_id, attempt_number, method_fingerprint, outcome, hypothesis, "
                    "failure_fingerprint, record_key, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        repair.run_id,
                        repair.attempt_number,
                        repair.method_fingerprint,
                        repair.outcome,
                        repair.fix_plan.hypothesis,
                        failure_fingerprint,
                        artifact.key,
                        repair.created_at.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise StorageError(
                    f"repair method was already recorded for run {repair.run_id}: "
                    f"{repair.method_fingerprint}"
                ) from exc
        return artifact

    def was_attempted(self, run_id: str, method_fingerprint: str) -> bool:
        with self._lock:
            row = self._connection.execute(
                "SELECT 1 FROM repair_history "
                "WHERE run_id = ? AND method_fingerprint = ? LIMIT 1",
                (run_id, method_fingerprint),
            ).fetchone()
        return row is not None

    def recent_failed_methods(
        self,
        run_id: str,
        limit: int,
    ) -> tuple[AttemptedMethod, ...]:
        if limit <= 0:
            return ()
        with self._lock:
            rows = self._connection.execute(
                "SELECT method_fingerprint, hypothesis, outcome, failure_fingerprint "
                "FROM repair_history WHERE run_id = ? AND outcome != 'succeeded' "
                "ORDER BY attempt_number DESC LIMIT ?",
                (run_id, limit),
            ).fetchall()
        return tuple(
            AttemptedMethod(
                fingerprint=row[0],
                hypothesis=row[1],
                outcome=row[2],
                failure_fingerprint=row[3],
            )
            for row in reversed(rows)
        )

    def checkpoint_count(self, run_id: str) -> int:
        config = {"configurable": {"thread_id": run_id}}
        return sum(1 for _ in self._checkpointer.list(config))

    def close(self) -> None:
        if self._closed:
            return
        with self._lock:
            self._connection.close()
            self._checkpoint_connection.close()
            self._closed = True

    def _setup_history(self) -> None:
        with self._connection:
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS repair_history ("
                "run_id TEXT NOT NULL, "
                "attempt_number INTEGER NOT NULL, "
                "method_fingerprint TEXT NOT NULL, "
                "outcome TEXT NOT NULL, "
                "hypothesis TEXT NOT NULL, "
                "failure_fingerprint TEXT NOT NULL, "
                "record_key TEXT NOT NULL, "
                "created_at TEXT NOT NULL, "
                "PRIMARY KEY (run_id, attempt_number), "
                "UNIQUE (run_id, method_fingerprint)"
                ")"
            )

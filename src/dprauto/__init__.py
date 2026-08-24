"""Public contracts for dprauto."""

from dprauto.agent.state import AgentState, create_agent_state
from dprauto.config import AppConfig, load_config
from dprauto.domain.models import (
    ArtifactRef,
    BuildPlan,
    BuildResult,
    BuildStep,
    CommandResult,
    CommandSpec,
    DependencyChange,
    EnvironmentDiff,
    EnvironmentSnapshot,
    FailureInfo,
    FileChange,
    GeneratedFile,
    ProjectCommand,
    ProjectProfile,
    SourceReference,
    ValueChange,
    VerificationResult,
)
from dprauto.domain.enums import BuildFailureKind, ProjectType

__all__ = [
    "AgentState",
    "AppConfig",
    "ArtifactRef",
    "BuildPlan",
    "BuildResult",
    "BuildStep",
    "BuildFailureKind",
    "CommandResult",
    "CommandSpec",
    "DependencyChange",
    "EnvironmentDiff",
    "EnvironmentSnapshot",
    "FailureInfo",
    "FileChange",
    "GeneratedFile",
    "ProjectCommand",
    "ProjectProfile",
    "ProjectType",
    "SourceReference",
    "ValueChange",
    "VerificationResult",
    "create_agent_state",
    "load_config",
]

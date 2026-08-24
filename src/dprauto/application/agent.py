"""Composition root for the LangGraph repair agent and concrete adapters."""

from __future__ import annotations

from dprauto.adapters.execution import SubprocessCommandExecutor
from dprauto.adapters.llm import (
    APIModelSettings,
    FailoverLLMClient,
    LLMRepairPlanner,
    OpenAICompatibleLLMClient,
)
from dprauto.adapters.persistence import SQLiteAgentPersistence
from dprauto.adapters.preflight import DockerRepairPreflight
from dprauto.adapters.python import PythonEnvironmentDiffer, PythonProjectParser
from dprauto.agent.tools import (
    BuildImageTool,
    GetBuildLogTool,
    InspectProjectTool,
    ListProjectFilesTool,
    ModifyBuildScriptTool,
    PatchBaseImageTool,
    PatchPythonDependenciesTool,
    PatchSystemPackagesTool,
    PatchVerificationDependenciesTool,
    ReadFileTool,
    RunCommandTool,
    SearchProjectTool,
    ToolRegistry,
)
from dprauto.agent.context import AgentContextManager
from dprauto.agent.workflow import AgentWorkflow
from dprauto.agent.full_workflow import EnvironmentBuildWorkflow
from dprauto.application.build import create_deterministic_builder
from dprauto.application.verification import create_layered_verifier
from dprauto.application.regression import PersistedRegressionChecker
from dprauto.config import AppConfig
from dprauto.ports.llm import LLMClient
from dprauto.ports.persistence import AgentPersistence
from dprauto.ports.storage import Storage
from dprauto.observability.llm_calls import HourlyLLMCallLogger


def create_agent_workflow(
    storage: Storage,
    llm_client: LLMClient,
    config: AppConfig | None = None,
    persistence: AgentPersistence | None = None,
) -> AgentWorkflow:
    """Wire production tools while preserving Port/Adapter boundaries."""

    app_config = config or AppConfig()
    parser = PythonProjectParser()
    executor = SubprocessCommandExecutor(storage)
    builder = create_deterministic_builder(storage, app_config.build)
    verifier = create_layered_verifier(
        storage, app_config.build, app_config.verification
    )
    regression_checker = PersistedRegressionChecker(storage)
    environment_differ = PythonEnvironmentDiffer()
    preflight_runner = DockerRepairPreflight(
        executor,
        storage,
        docker_binary=app_config.build.docker_binary,
        timeout_seconds=app_config.agent.preflight_timeout_seconds,
    )
    agent_persistence = persistence or SQLiteAgentPersistence(
        app_config.storage.root / "agent-state.sqlite",
        storage,
    )
    registry = ToolRegistry(
        (
            InspectProjectTool(parser),
            ListProjectFilesTool(),
            ReadFileTool(),
            SearchProjectTool(),
            PatchSystemPackagesTool(storage, app_config.security),
            PatchPythonDependenciesTool(storage, app_config.security),
            PatchVerificationDependenciesTool(storage, app_config.security),
            PatchBaseImageTool(storage, app_config.security),
            ModifyBuildScriptTool(storage, app_config.security),
            RunCommandTool(executor, max_timeout_seconds=app_config.build.timeout_seconds),
            BuildImageTool(builder),
            GetBuildLogTool(storage),
        )
    )
    return AgentWorkflow(
        LLMRepairPlanner(
            llm_client,
            AgentContextManager(app_config.agent),
            max_investigation_actions_per_round=min(
                2, app_config.agent.max_investigation_actions
            ),
        ),
        registry,
        storage,
        app_config.agent,
        agent_persistence,
        verifier,
        regression_checker,
        environment_differ,
        preflight_runner,
    )


def create_api_llm_client(config: AppConfig | None = None) -> FailoverLLMClient:
    """Create the API LLM client from myapi.json without logging its API key."""

    app_config = config or AppConfig()
    settings = APIModelSettings.all_from_json(app_config.llm.api_config_path)
    logger = HourlyLLMCallLogger(app_config.llm.request_log_root)
    return FailoverLLMClient(
        tuple(
            OpenAICompatibleLLMClient(
                model,
                logger,
                temperature=app_config.llm.temperature,
                timeout_seconds=app_config.llm.timeout_seconds,
                max_output_tokens=app_config.llm.max_output_tokens,
            )
            for model in settings
        ),
        attempts_per_model=app_config.llm.timeout_retries_per_model + 1,
    )


def create_api_agent_workflow(
    storage: Storage,
    config: AppConfig | None = None,
    persistence: AgentPersistence | None = None,
) -> AgentWorkflow:
    """Production composition using myapi.json and hourly request/response logs."""

    app_config = config or AppConfig()
    return create_agent_workflow(
        storage,
        create_api_llm_client(app_config),
        app_config,
        persistence,
    )


def create_environment_build_workflow(
    storage: Storage,
    llm_client: LLMClient,
    config: AppConfig | None = None,
    persistence: AgentPersistence | None = None,
) -> EnvironmentBuildWorkflow:
    """Compose the full workflow around the existing repair workflow."""

    return EnvironmentBuildWorkflow(
        create_agent_workflow(storage, llm_client, config, persistence),
        storage,
    )


def create_api_environment_build_workflow(
    storage: Storage,
    config: AppConfig | None = None,
    persistence: AgentPersistence | None = None,
) -> EnvironmentBuildWorkflow:
    """Full production workflow using myapi.json and hourly LLM call logs."""

    app_config = config or AppConfig()
    return create_environment_build_workflow(
        storage,
        create_api_llm_client(app_config),
        app_config,
        persistence,
    )

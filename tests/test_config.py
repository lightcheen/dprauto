import unittest
from pathlib import Path

from dprauto.config import AppConfig, load_config
from dprauto.errors import ConfigurationError


class ConfigurationTests(unittest.TestCase):
    def test_defaults_are_safe_and_llm_is_optional(self) -> None:
        config = load_config({})
        self.assertIsInstance(config, AppConfig)
        self.assertFalse(config.security.allow_source_changes)
        self.assertFalse(config.security.allow_privileged_execution)
        self.assertFalse(config.llm.enabled)
        self.assertEqual(config.llm.api_config_path, Path("myapi.json"))
        self.assertEqual(config.llm.request_log_root, Path("logs/llm"))
        self.assertEqual(config.agent.max_context_characters, 24_000)
        self.assertEqual(config.agent.preflight_timeout_seconds, 30)
        self.assertEqual(config.agent.max_investigation_rounds, 3)
        self.assertEqual(config.agent.max_investigation_actions, 6)
        self.assertEqual(config.agent.max_evidence_characters, 10_000)
        self.assertEqual(config.verification.command_timeout_seconds, 300)
        self.assertEqual(config.verification.jvm_command_timeout_seconds, 900)
        self.assertEqual(
            config.verification.dependency_command_timeout_seconds,
            180,
        )
        self.assertEqual(config.llm.timeout_retries_per_model, 1)
        self.assertEqual(config.llm.max_timeout_attempts_per_operation, 2)
        self.assertEqual(config.llm.max_output_tokens, 4096)
        self.assertEqual(config.verification.web_path, "/")
        self.assertEqual(config.verification.pytest_version, "8.3.5")
        self.assertEqual(config.verification.pytest_xdist_version, "3.6.1")
        self.assertEqual(config.verification.tox_version, "4.23.2")
        self.assertEqual(config.verification.nox_version, "2024.10.9")
        self.assertEqual(config.verification.max_parallel_test_workers, 4)
        self.assertEqual(config.verification.max_test_files_per_slice, 8)
        self.assertTrue(config.verification.service_orchestration_enabled)
        self.assertEqual(config.verification.service_startup_timeout_seconds, 30)
        self.assertEqual(config.verification.max_service_containers, 2)
        self.assertEqual(config.verification.postgres_service_image, "postgres:16-alpine")
        self.assertEqual(config.verification.redis_service_image, "redis:7-alpine")
        self.assertTrue(config.verification.minimal_test_dependency_closure_enabled)
        self.assertEqual(config.verification.max_dependency_analysis_files, 256)
        self.assertEqual(config.build.docker_network, "")
        self.assertTrue(config.build.strategy_portfolio_enabled)
        self.assertEqual(config.build.max_strategy_attempts, 3)
        self.assertTrue(config.build.forward_proxy_environment)
        self.assertEqual(config.build.poetry_version, "1.8.5")
        self.assertEqual(config.build.poetry_tool_image, "")
        self.assertEqual(config.build.poetry_tool_timeout_seconds, 900)
        self.assertEqual(config.build.default_java_version, "17")
        self.assertEqual(
            config.build.maven_base_image,
            "maven:3.9.9-eclipse-temurin-{version}",
        )
        self.assertEqual(config.build.gradle_base_image, "gradle:8.12.1-jdk{version}")
        self.assertEqual(config.build.native_base_image, "debian:bookworm-slim")
        self.assertEqual(config.build.timeout_seconds, 3600)
        self.assertEqual(config.build.max_build_jobs, 4)
        self.assertEqual(config.verification.docker_network, "")
        self.assertEqual(config.storage.root, Path("runs"))

    def test_environment_overrides_are_loaded_from_one_prefix(self) -> None:
        config = load_config(
            {
                "DPRAUTO_BUILD_TIMEOUT_SECONDS": "42",
                "DPRAUTO_BUILD_STRATEGY_PORTFOLIO_ENABLED": "off",
                "DPRAUTO_BUILD_MAX_STRATEGY_ATTEMPTS": "2",
                "DPRAUTO_BUILD_ALLOW_NETWORK": "off",
                "DPRAUTO_BUILD_FORWARD_PROXY_ENVIRONMENT": "off",
                "DPRAUTO_BUILD_DOCKER_BINARY": "/usr/local/bin/docker",
                "DPRAUTO_BUILD_CNB_BUILDER": "example/builder@sha256:abc",
                "DPRAUTO_BUILD_DOCKER_NETWORK": "host",
                "DPRAUTO_BUILD_POETRY_VERSION": "2.1.4",
                "DPRAUTO_BUILD_POETRY_TOOL_IMAGE": (
                    "tools/poetry:python-{version}-poetry-{poetry_version}"
                ),
                "DPRAUTO_BUILD_POETRY_TOOL_TIMEOUT_SECONDS": "700",
                "DPRAUTO_BUILD_DEFAULT_JAVA_VERSION": "21",
                "DPRAUTO_BUILD_MAVEN_BASE_IMAGE": "example/maven:jdk-{version}",
                "DPRAUTO_BUILD_GRADLE_BASE_IMAGE": "example/gradle:jdk-{version}",
                "DPRAUTO_BUILD_NATIVE_BASE_IMAGE": "example/native:fixed",
                "DPRAUTO_BUILD_MAX_BUILD_JOBS": "5",
                "DPRAUTO_AGENT_MAX_ATTEMPTS": "3",
                "DPRAUTO_AGENT_MAX_CONTEXT_CHARACTERS": "8000",
                "DPRAUTO_AGENT_MAX_FAILED_METHODS": "4",
                "DPRAUTO_AGENT_PREFLIGHT_TIMEOUT_SECONDS": "11",
                "DPRAUTO_AGENT_MAX_INVESTIGATION_ROUNDS": "2",
                "DPRAUTO_AGENT_MAX_INVESTIGATION_ACTIONS": "4",
                "DPRAUTO_AGENT_MAX_EVIDENCE_CHARACTERS": "7000",
                "DPRAUTO_AGENT_MAX_PLAN_FEEDBACK_ROUNDS": "3",
                "DPRAUTO_VERIFICATION_COMMAND_TIMEOUT_SECONDS": "17",
                "DPRAUTO_VERIFICATION_JVM_COMMAND_TIMEOUT_SECONDS": "71",
                "DPRAUTO_VERIFICATION_DEPENDENCY_COMMAND_TIMEOUT_SECONDS": "29",
                "DPRAUTO_VERIFICATION_WEB_STARTUP_TIMEOUT_SECONDS": "9",
                "DPRAUTO_VERIFICATION_WEB_PATH": "/healthz",
                "DPRAUTO_VERIFICATION_DOCKER_NETWORK": "dprauto-verify",
                "DPRAUTO_VERIFICATION_PYTEST_VERSION": "8.4.2",
                "DPRAUTO_VERIFICATION_PYTEST_XDIST_VERSION": "3.7.0",
                "DPRAUTO_VERIFICATION_TOX_VERSION": "4.24.2",
                "DPRAUTO_VERIFICATION_NOX_VERSION": "2025.2.9",
                "DPRAUTO_VERIFICATION_MAX_PARALLEL_TEST_WORKERS": "6",
                "DPRAUTO_VERIFICATION_MAX_TEST_FILES_PER_SLICE": "5",
                "DPRAUTO_VERIFICATION_SERVICE_ORCHESTRATION_ENABLED": "off",
                "DPRAUTO_VERIFICATION_SERVICE_STARTUP_TIMEOUT_SECONDS": "13",
                "DPRAUTO_VERIFICATION_MAX_SERVICE_CONTAINERS": "3",
                "DPRAUTO_VERIFICATION_POSTGRES_SERVICE_IMAGE": "postgres:15-alpine",
                "DPRAUTO_VERIFICATION_REDIS_SERVICE_IMAGE": "redis:6-alpine",
                "DPRAUTO_VERIFICATION_MINIMAL_TEST_DEPENDENCY_CLOSURE_ENABLED": "off",
                "DPRAUTO_VERIFICATION_MAX_DEPENDENCY_ANALYSIS_FILES": "512",
                "DPRAUTO_LLM_PROVIDER": "provider",
                "DPRAUTO_LLM_MODEL": "model",
                "DPRAUTO_LLM_TEMPERATURE": "0.25",
                "DPRAUTO_LLM_API_CONFIG_PATH": "/tmp/model.json",
                "DPRAUTO_LLM_REQUEST_LOG_ROOT": "/tmp/llm-logs",
                "DPRAUTO_LLM_TIMEOUT_RETRIES_PER_MODEL": "2",
                "DPRAUTO_LLM_MAX_TIMEOUT_ATTEMPTS_PER_OPERATION": "3",
                "DPRAUTO_LLM_MAX_OUTPUT_TOKENS": "2048",
                "DPRAUTO_STORAGE_ROOT": "/tmp/dprauto-runs",
                "DPRAUTO_LOG_LEVEL": "debug",
            }
        )
        self.assertEqual(config.build.timeout_seconds, 42)
        self.assertFalse(config.build.strategy_portfolio_enabled)
        self.assertEqual(config.build.max_strategy_attempts, 2)
        self.assertFalse(config.build.allow_network)
        self.assertFalse(config.build.forward_proxy_environment)
        self.assertEqual(config.build.docker_binary, "/usr/local/bin/docker")
        self.assertEqual(config.build.cnb_builder, "example/builder@sha256:abc")
        self.assertEqual(config.build.docker_network, "host")
        self.assertEqual(config.build.poetry_version, "2.1.4")
        self.assertEqual(
            config.build.poetry_tool_image,
            "tools/poetry:python-{version}-poetry-{poetry_version}",
        )
        self.assertEqual(config.build.poetry_tool_timeout_seconds, 700)
        self.assertEqual(config.build.default_java_version, "21")
        self.assertEqual(config.build.maven_base_image, "example/maven:jdk-{version}")
        self.assertEqual(config.build.gradle_base_image, "example/gradle:jdk-{version}")
        self.assertEqual(config.build.native_base_image, "example/native:fixed")
        self.assertEqual(config.build.max_build_jobs, 5)
        self.assertEqual(config.agent.max_attempts, 3)
        self.assertEqual(config.agent.max_context_characters, 8_000)
        self.assertEqual(config.agent.max_failed_methods, 4)
        self.assertEqual(config.agent.preflight_timeout_seconds, 11)
        self.assertEqual(config.agent.max_investigation_rounds, 2)
        self.assertEqual(config.agent.max_investigation_actions, 4)
        self.assertEqual(config.agent.max_evidence_characters, 7_000)
        self.assertEqual(config.agent.max_plan_feedback_rounds, 3)
        self.assertEqual(config.verification.command_timeout_seconds, 17)
        self.assertEqual(config.verification.jvm_command_timeout_seconds, 71)
        self.assertEqual(
            config.verification.dependency_command_timeout_seconds,
            29,
        )
        self.assertEqual(config.verification.web_startup_timeout_seconds, 9)
        self.assertEqual(config.verification.web_path, "/healthz")
        self.assertEqual(config.verification.docker_network, "dprauto-verify")
        self.assertEqual(config.verification.pytest_version, "8.4.2")
        self.assertEqual(config.verification.pytest_xdist_version, "3.7.0")
        self.assertEqual(config.verification.tox_version, "4.24.2")
        self.assertEqual(config.verification.nox_version, "2025.2.9")
        self.assertEqual(config.verification.max_parallel_test_workers, 6)
        self.assertEqual(config.verification.max_test_files_per_slice, 5)
        self.assertFalse(config.verification.service_orchestration_enabled)
        self.assertEqual(config.verification.service_startup_timeout_seconds, 13)
        self.assertEqual(config.verification.max_service_containers, 3)
        self.assertEqual(config.verification.postgres_service_image, "postgres:15-alpine")
        self.assertEqual(config.verification.redis_service_image, "redis:6-alpine")
        self.assertFalse(config.verification.minimal_test_dependency_closure_enabled)
        self.assertEqual(config.verification.max_dependency_analysis_files, 512)
        self.assertTrue(config.llm.enabled)
        self.assertEqual(config.llm.temperature, 0.25)
        self.assertEqual(config.llm.api_config_path, Path("/tmp/model.json"))
        self.assertEqual(config.llm.timeout_retries_per_model, 2)
        self.assertEqual(config.llm.max_timeout_attempts_per_operation, 3)
        self.assertEqual(config.llm.max_output_tokens, 2048)
        self.assertEqual(config.llm.request_log_root, Path("/tmp/llm-logs"))
        self.assertEqual(config.storage.root, Path("/tmp/dprauto-runs"))
        self.assertEqual(config.logging.level, "DEBUG")

    def test_invalid_values_raise_unified_configuration_error(self) -> None:
        for environment in (
            {"DPRAUTO_BUILD_ALLOW_NETWORK": "maybe"},
            {"DPRAUTO_BUILD_STRATEGY_PORTFOLIO_ENABLED": "maybe"},
            {"DPRAUTO_BUILD_MAX_STRATEGY_ATTEMPTS": "0"},
            {"DPRAUTO_BUILD_FORWARD_PROXY_ENVIRONMENT": "maybe"},
            {"DPRAUTO_AGENT_MAX_ATTEMPTS": "0"},
            {"DPRAUTO_AGENT_MAX_CONTEXT_CHARACTERS": "1999"},
            {"DPRAUTO_AGENT_PREFLIGHT_TIMEOUT_SECONDS": "0"},
            {"DPRAUTO_AGENT_MAX_INVESTIGATION_ROUNDS": "0"},
            {"DPRAUTO_AGENT_MAX_INVESTIGATION_ACTIONS": "0"},
            {"DPRAUTO_AGENT_MAX_EVIDENCE_CHARACTERS": "0"},
            {"DPRAUTO_VERIFICATION_DEPENDENCY_COMMAND_TIMEOUT_SECONDS": "0"},
            {"DPRAUTO_VERIFICATION_WEB_PATH": "health"},
            {"DPRAUTO_VERIFICATION_PYTEST_VERSION": ">=8"},
            {"DPRAUTO_VERIFICATION_PYTEST_VERSION": "latest"},
            {"DPRAUTO_VERIFICATION_PYTEST_XDIST_VERSION": ">=3"},
            {"DPRAUTO_VERIFICATION_TOX_VERSION": ""},
            {"DPRAUTO_VERIFICATION_NOX_VERSION": "2025 2 9"},
            {"DPRAUTO_VERIFICATION_MAX_PARALLEL_TEST_WORKERS": "0"},
            {"DPRAUTO_VERIFICATION_MAX_PARALLEL_TEST_WORKERS": "33"},
            {"DPRAUTO_VERIFICATION_MAX_TEST_FILES_PER_SLICE": "0"},
            {"DPRAUTO_VERIFICATION_MAX_TEST_FILES_PER_SLICE": "33"},
            {"DPRAUTO_VERIFICATION_SERVICE_ORCHESTRATION_ENABLED": "maybe"},
            {"DPRAUTO_VERIFICATION_SERVICE_STARTUP_TIMEOUT_SECONDS": "0"},
            {"DPRAUTO_VERIFICATION_MAX_SERVICE_CONTAINERS": "0"},
            {"DPRAUTO_VERIFICATION_MAX_SERVICE_CONTAINERS": "5"},
            {"DPRAUTO_VERIFICATION_POSTGRES_SERVICE_IMAGE": ""},
            {"DPRAUTO_VERIFICATION_REDIS_SERVICE_IMAGE": "redis latest"},
            {"DPRAUTO_VERIFICATION_MINIMAL_TEST_DEPENDENCY_CLOSURE_ENABLED": "maybe"},
            {"DPRAUTO_VERIFICATION_MAX_DEPENDENCY_ANALYSIS_FILES": "15"},
            {"DPRAUTO_VERIFICATION_MAX_DEPENDENCY_ANALYSIS_FILES": "2049"},
            {"DPRAUTO_BUILD_DOCKER_NETWORK": "invalid network"},
            {"DPRAUTO_VERIFICATION_DOCKER_NETWORK": "-invalid"},
            {"DPRAUTO_BUILD_POETRY_VERSION": ">=1.8"},
            {"DPRAUTO_BUILD_POETRY_TOOL_IMAGE": "tool:{unknown}"},
            {"DPRAUTO_BUILD_POETRY_TOOL_TIMEOUT_SECONDS": "0"},
            {"DPRAUTO_BUILD_DEFAULT_JAVA_VERSION": ">=17"},
            {"DPRAUTO_BUILD_MAVEN_BASE_IMAGE": "maven:{unknown}"},
            {"DPRAUTO_BUILD_GRADLE_BASE_IMAGE": "gradle jdk-{version}"},
            {"DPRAUTO_BUILD_NATIVE_BASE_IMAGE": ""},
            {"DPRAUTO_BUILD_MAX_BUILD_JOBS": "0"},
            {"DPRAUTO_BUILD_MAX_BUILD_JOBS": "33"},
            {"DPRAUTO_LLM_TEMPERATURE": "3"},
            {"DPRAUTO_LLM_MAX_TIMEOUT_ATTEMPTS_PER_OPERATION": "0"},
            {"DPRAUTO_LLM_MAX_TIMEOUT_ATTEMPTS_PER_OPERATION": "9"},
            {"DPRAUTO_LOG_LEVEL": "verbose"},
        ):
            with self.subTest(environment=environment):
                with self.assertRaises(ConfigurationError):
                    load_config(environment)


if __name__ == "__main__":
    unittest.main()

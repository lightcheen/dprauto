import unittest

from dprauto.adapters.llm import LLMRepairPlanner
from dprauto.agent.state import create_agent_state
from dprauto.domain.enums import BuildStage, FailureCategory
from dprauto.domain.models import FailureInfo
from dprauto.errors import LLMError
from dprauto.ports.llm import LLMResponse


class RecordingLLMClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        return self.responses.pop(0)


def failed_state():
    state = create_agent_state("planner-run")
    state["failure"] = FailureInfo(
        FailureCategory.BUILD_COMMAND,
        BuildStage.BUILD,
        "build command failed",
        "build-command:fixture",
        key_log="RUN false exited with code 1",
        possible_cause="Dockerfile contains a failing build command",
    )
    return state


def timed_out_state():
    state = create_agent_state("planner-timeout-run")
    state["failure"] = FailureInfo(
        FailureCategory.BUILD_COMMAND,
        BuildStage.DEPENDENCY_INSTALLATION,
        "Build command timed out",
        "timeout:fixture",
        key_log="Collecting pandas\ncommand timed out after 60 seconds",
        evidence=("timeout_profile=python-package-install",),
        suggestions=("Do not add dependencies for timeout alone.",),
    )
    return state


def failed_test_state():
    state = create_agent_state("planner-testability-run")
    state["failure"] = FailureInfo(
        FailureCategory.TEST,
        BuildStage.TEST,
        "Testability verification did not pass",
        "test:missing-pytest-mock",
        key_log="ModuleNotFoundError: No module named 'pytest_mock'",
        possible_cause="the test-only pytest-mock plugin is missing",
    )
    return state


class LLMRepairPlannerTests(unittest.TestCase):
    def test_llm_investigation_selects_only_valid_read_only_tool(self) -> None:
        client = RecordingLLMClient(
            [
                LLMResponse(
                    "",
                    structured={
                        "complete": False,
                        "rationale": "inspect declared dependencies",
                        "actions": [
                            {
                                "tool": "read_file",
                                "arguments": {"path": "pyproject.toml"},
                                "rationale": "confirm the package extra",
                            }
                        ],
                    },
                )
            ]
        )
        planner = LLMRepairPlanner(client)
        specification = {
            "read_file": {
                "effect": "observe",
                "argument_schema": {
                    "type": "object",
                    "properties": {"path": {"type": "string", "minLength": 1}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
            }
        }

        decision = planner.plan_investigation(failed_state(), (), specification)

        self.assertFalse(decision.complete)
        self.assertEqual(decision.actions[0].tool, "read_file")
        self.assertEqual(decision.actions[0].arguments["path"], "pyproject.toml")
        self.assertEqual(
            client.requests[0].metadata["operation"],
            "investigate_failure",
        )
        self.assertIn("minimum additional evidence", client.requests[0].messages[0].content)
        self.assertIn("truncated=true", client.requests[0].messages[0].content)

    def test_invalid_investigation_tool_is_corrected_to_completion(self) -> None:
        client = RecordingLLMClient(
            [
                LLMResponse(
                    "",
                    structured={
                        "complete": False,
                        "rationale": "change the project",
                        "actions": [
                            {
                                "tool": "modify_build_script",
                                "arguments": {"path": "Dockerfile", "content": "FROM scratch"},
                            }
                        ],
                    },
                ),
                LLMResponse(
                    "",
                    structured={
                        "complete": True,
                        "rationale": "existing evidence is sufficient",
                        "actions": [],
                    },
                ),
            ]
        )

        decision = LLMRepairPlanner(client).plan_investigation(
            failed_state(),
            (),
            {"read_file": {"effect": "observe", "argument_schema": {"type": "object"}}},
        )

        self.assertTrue(decision.complete)
        self.assertEqual(len(client.requests), 2)
        self.assertIn("unavailable investigation tool", client.requests[1].messages[1].content)

    def test_llm_analyzes_and_selects_structured_tool_call(self) -> None:
        client = RecordingLLMClient(
            [
                LLMResponse("", structured={"diagnosis": "RUN false is intentional"}),
                LLMResponse(
                    "",
                    structured={
                        "hypothesis": "replace the failing build instruction",
                        "summary": "repair Dockerfile",
                        "actions": [
                            {
                                "tool": "modify_build_script",
                                "arguments": {
                                    "path": "Dockerfile",
                                    "content": "FROM python:3.11-slim\nRUN true\n",
                                },
                                "rationale": "remove deterministic failure",
                            }
                        ],
                    },
                ),
            ]
        )
        planner = LLMRepairPlanner(client)
        state = failed_state()

        diagnosis = planner.analyze_failure(state, ())
        plan = planner.plan_fix(
            state,
            diagnosis,
            {"modify_build_script": "modify a build script"},
        )

        self.assertEqual(diagnosis, "RUN false is intentional")
        self.assertEqual(plan.actions[0].tool, "modify_build_script")
        self.assertEqual(client.requests[0].metadata["operation"], "analyze_failure")
        self.assertEqual(client.requests[1].metadata["operation"], "plan_fix")
        self.assertIn("never edit business source", client.requests[1].messages[0].content)
        self.assertIn("patch_system_packages", client.requests[1].messages[0].content)
        self.assertIn("one high-risk", client.requests[1].messages[0].content)
        self.assertIn(
            '"diagnosis":"concise root-cause analysis"',
            client.requests[0].messages[0].content,
        )
        self.assertIn('"hypothesis":"..."', client.requests[1].messages[0].content)

    def test_analysis_normalizes_shapes_observed_in_real_evaluation(self) -> None:
        responses = (
            {"analysis": "installation failed", "root_cause": "git is missing"},
            {"failure_analysis": {"root_cause": "the lock file is stale"}},
        )
        for structured, expected in zip(
            responses,
            ("git is missing", "the lock file is stale"),
        ):
            with self.subTest(structured=structured):
                client = RecordingLLMClient([LLMResponse("", structured=structured)])
                diagnosis = LLMRepairPlanner(client).analyze_failure(failed_state(), ())
                self.assertEqual(diagnosis, expected)

    def test_unavailable_tool_is_rejected_before_execution(self) -> None:
        invalid = LLMResponse(
            "",
            structured={
                "hypothesis": "unsafe action",
                "actions": [{"tool": "delete_project", "arguments": {}}],
            },
        )
        client = RecordingLLMClient(
            [invalid, invalid]
        )
        with self.assertRaises(LLMError):
            LLMRepairPlanner(client).plan_fix(
                failed_state(),
                "diagnosis",
                {"modify_build_script": "safe mutation"},
            )

    def test_invalid_tool_arguments_are_fed_back_once_and_corrected(self) -> None:
        client = RecordingLLMClient(
            [
                LLMResponse(
                    "",
                    structured={
                        "hypothesis": "patch build script",
                        "actions": [
                            {
                                "tool": "modify_build_script",
                                "arguments": {"path": "Dockerfile", "diff": "@@"},
                            }
                        ],
                    },
                ),
                LLMResponse(
                    "",
                    structured={
                        "hypothesis": "replace build script",
                        "actions": [
                            {
                                "tool": "modify_build_script",
                                "arguments": {
                                    "path": "Dockerfile",
                                    "content": "FROM python:3.11-slim\n",
                                },
                            }
                        ],
                    },
                ),
            ]
        )
        specification = {
            "modify_build_script": {
                "description": "replace build script",
                "argument_schema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "minLength": 1},
                        "content": {"type": "string", "minLength": 1},
                    },
                    "required": ["path", "content"],
                    "additionalProperties": False,
                },
            }
        }

        plan = LLMRepairPlanner(client).plan_fix(
            failed_state(), "diagnosis", specification
        )

        self.assertEqual(plan.actions[0].arguments["content"], "FROM python:3.11-slim\n")
        self.assertEqual(len(client.requests), 2)
        self.assertIn("missing required argument 'content'", client.requests[1].messages[1].content)
        self.assertEqual(client.requests[1].metadata["contract_attempt"], 2)

    def test_timeout_failure_adds_reduction_policy_to_plan_payload(self) -> None:
        client = RecordingLLMClient(
            [
                LLMResponse(
                    "",
                    structured={
                        "hypothesis": "reduce install scope",
                        "actions": [
                            {
                                "tool": "modify_build_script",
                                "arguments": {
                                    "path": "Dockerfile",
                                    "content": (
                                        "FROM python:3.11-slim\n"
                                        "RUN python -m pip install .\n"
                                    ),
                                },
                            }
                        ],
                    },
                )
            ]
        )

        LLMRepairPlanner(client).plan_fix(
            timed_out_state(),
            "pip install timed out",
            {"modify_build_script": "replace build script"},
        )

        payload = client.requests[0].messages[1].content
        self.assertIn('"timeout_repair"', payload)
        self.assertIn("reduce build/install cost", payload)
        self.assertIn("adding Python dependencies", payload)
        self.assertIn('"structured_patch_preference"', payload)

    def test_test_failure_routes_python_dependency_to_verification_overlay(self) -> None:
        client = RecordingLLMClient(
            [
                LLMResponse(
                    "",
                    structured={
                        "hypothesis": "install the missing test plugin",
                        "actions": [
                            {
                                "tool": "patch_verification_dependencies",
                                "arguments": {"packages": ["pytest-mock==3.14.0"]},
                            }
                        ],
                    },
                )
            ]
        )

        plan = LLMRepairPlanner(client).plan_fix(
            failed_test_state(),
            "pytest-mock is missing only from Testability",
            {
                "patch_verification_dependencies": {
                    "description": "add test-only Python dependencies",
                    "argument_schema": {
                        "type": "object",
                        "properties": {
                            "packages": {
                                "type": "array",
                                "items": {"type": "string"},
                                "minItems": 1,
                            }
                        },
                        "required": ["packages"],
                        "additionalProperties": False,
                    },
                }
            },
        )

        self.assertEqual(plan.actions[0].tool, "patch_verification_dependencies")
        payload = client.requests[0].messages[1].content
        self.assertIn('"verification_repair"', payload)
        self.assertIn("temporary Testability container only", payload)
        self.assertIn("runtime_image_must_remain_unchanged", payload)
        self.assertIn("patch_verification_dependencies", payload)


if __name__ == "__main__":
    unittest.main()

import unittest

from dprauto.agent.context import AgentContextManager
from dprauto.agent.models import AttemptedMethod, ContextSummary, ToolResult
from dprauto.agent.state import create_agent_state
from dprauto.config import AgentConfig
from dprauto.domain.enums import BuildStage, FailureCategory
from dprauto.domain.models import FailureInfo, ProjectProfile, SourceReference
from dprauto.serialization import to_json_bytes


class AgentContextManagerTests(unittest.TestCase):
    def test_recent_source_evidence_keeps_head_and_is_not_a_build_script(self) -> None:
        manager = AgentContextManager(AgentConfig(max_context_characters=5_000))
        state = create_agent_state("source-evidence-run")
        state["project_profile"] = ProjectProfile(
            "source-evidence-project",
            SourceReference("fixture"),
            languages=("Python",),
            dockerfiles=("Dockerfile",),
        )
        observations = (
            ToolResult(
                "read_file",
                True,
                "read Dockerfile",
                data={"path": "Dockerfile", "content": "FROM python:3.11-slim\n"},
            ),
            ToolResult(
                "read_file",
                True,
                "read large source",
                data={
                    "path": "src/project/common.py",
                    "content": (
                        "sys.stdout = io.TextIOWrapper(sys.stdout.buffer)\n"
                        + "middle = True\n" * 2_000
                        + "END = True\n"
                    ),
                    "start_line": 1,
                    "end_line": 400,
                    "total_lines": 2_002,
                    "truncated": True,
                    "next_start_line": 401,
                },
            ),
        )

        context = manager.build_llm_context(state, observations)

        self.assertEqual(
            tuple(item["path"] for item in context["current_build_scripts"]),
            ("Dockerfile",),
        )
        self.assertEqual(context["evidence"][0]["data"]["start_line"], 1)
        self.assertEqual(context["evidence"][0]["ref"], "observation:1")
        self.assertEqual(context["evidence"][0]["data"]["next_start_line"], 401)
        self.assertIn(
            "sys.stdout = io.TextIOWrapper",
            context["evidence"][0]["data"]["content"],
        )
        self.assertIn("characters omitted", context["evidence"][0]["data"]["content"])

    def test_large_history_is_compacted_to_a_fixed_llm_context(self) -> None:
        config = AgentConfig(
            max_context_characters=5_000,
            max_recent_modifications=3,
            max_failed_methods=3,
            max_resolved_issues=3,
        )
        manager = AgentContextManager(config)
        state = create_agent_state("context-run")
        state["project_profile"] = ProjectProfile(
            "large-project",
            SourceReference("fixture"),
            languages=("Python",),
            dockerfiles=("Dockerfile",),
            metadata={"large_scanner_output": "x" * 50_000},
        )
        state["failure"] = FailureInfo(
            FailureCategory.BUILD_COMMAND,
            BuildStage.BUILD,
            "current build failure",
            "failure:current",
            key_log="log-line\n" * 10_000,
            evidence=("timeout_profile=python-package-install",),
            suggestions=("reduce install scope",),
        )
        observations = (
            ToolResult(
                "read_file",
                True,
                "large Dockerfile",
                data={"path": "Dockerfile", "content": "RUN echo x\n" * 10_000},
            ),
        )

        sizes = []
        contexts = []
        for history_size in (20, 200, 2_000):
            state["context_summary"] = ContextSummary(
                resolved_issues=tuple(
                    f"resolved-{index}" for index in range(history_size)
                ),
                recent_modifications=tuple(
                    f"change-{index}" for index in range(history_size)
                ),
                failed_methods=tuple(
                    AttemptedMethod(
                        f"method-{index}", f"hypothesis-{index}", "failed"
                    )
                    for index in range(history_size)
                ),
                narrative="summary " * history_size,
            )
            context = manager.build_llm_context(state, observations)
            contexts.append(context)
            sizes.append(len(to_json_bytes(context).decode()))
        context = contexts[-1]

        self.assertLessEqual(max(sizes), config.max_context_characters)
        self.assertLess(max(sizes) - min(sizes), 2_000)
        self.assertEqual(
            set(context),
            {
                "project_profile",
                "current_build_scripts",
                "evidence",
                "resolved_issues",
                "current_failure",
                "recent_modifications",
                "failed_methods",
                "round_feedback",
                "context_summary",
            },
        )
        self.assertNotIn("failure_history", context)
        self.assertLessEqual(len(context["failed_methods"]), 2)
        self.assertLessEqual(len(context["recent_modifications"]), 2)
        self.assertLessEqual(len(context["current_failure"]["key_log"]), 3_000)
        self.assertIn(
            "timeout_profile=python-package-install",
            context["current_failure"]["evidence"],
        )
        self.assertIn("reduce install scope", context["current_failure"]["suggestions"])
        self.assertEqual(context["evidence"][0]["tool"], "read_file")
        self.assertEqual(context["evidence"][0]["ref"], "observation:0")
        self.assertEqual(context["evidence"][0]["data"]["path"], "Dockerfile")


if __name__ == "__main__":
    unittest.main()

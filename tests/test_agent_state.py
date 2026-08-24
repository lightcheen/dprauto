import unittest

from dprauto.agent.state import create_agent_state
from dprauto.domain.enums import AgentPhase
from dprauto.errors import ModelValidationError


class AgentStateTests(unittest.TestCase):
    def test_minimal_state_has_bounded_history_fields(self) -> None:
        state = create_agent_state("run-1")
        self.assertEqual(state["phase"], AgentPhase.CREATED)
        self.assertEqual(state["attempt_number"], 0)
        self.assertEqual(state["context_summary"].failed_methods, ())
        self.assertEqual(state["failure_history"], ())
        self.assertEqual(state["tool_results"], ())
        self.assertEqual(state["verification_results"], ())
        self.assertEqual(state["environment_diffs"], ())
        self.assertEqual(state["artifacts"], ())

    def test_run_id_is_required(self) -> None:
        with self.assertRaises(ModelValidationError):
            create_agent_state("  ")


if __name__ == "__main__":
    unittest.main()

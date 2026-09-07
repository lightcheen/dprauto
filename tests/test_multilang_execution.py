import json
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from evaluations.multilang.run_execution import (
    _json_compatible,
    evaluation_identity,
    parse_indices,
    reusable_record,
    summarize,
)
from evaluations.multilang.run_agent_canary import terminal_llm_service_error


def record(
    case_id: str,
    language: str,
    *,
    build: str = "succeeded",
    outcome: str = "succeeded",
    strict: bool = True,
    layers: tuple[str, str, str] = ("passed", "passed", "passed"),
    runtime_metadata: dict | None = None,
) -> dict:
    results = [
        {"level": name, "status": status}
        for name, status in zip(
            ("installability", "testability", "runnability"),
            layers,
        )
    ]
    if runtime_metadata is not None:
        results[-1]["metadata"] = runtime_metadata
    return {
        "case": {
            "case_id": case_id,
            "repo": f"example/{case_id}",
            "primary_language": language,
        },
        "build": {"result": {"status": build}},
        "verification": {"results": results},
        "outcome": {
            "status": outcome,
            "category": "none" if outcome == "succeeded" else "testability",
            "strict_test_succeeded": strict,
        },
        "elapsed_seconds": 10.0,
    }


class MultilangExecutionTests(unittest.TestCase):
    def test_agent_canary_stops_batch_on_terminal_llm_service_error(self) -> None:
        record = {
            "state_metrics": {
                "stop_reason": "[llm_error] LLM API returned HTTP 402: balance insufficient"
            }
        }

        self.assertEqual(
            terminal_llm_service_error(record),
            "LLM service returned terminal HTTP 402",
        )
        self.assertEqual(
            terminal_llm_service_error(
                {"state_metrics": {"stop_reason": "LLM request timed out"}}
            ),
            "",
        )

    def test_live_records_use_the_same_json_shape_as_resumed_records(self) -> None:
        @dataclass
        class Result:
            status: str

        normalized = _json_compatible({"build": {"result": Result("succeeded")}})

        self.assertEqual(normalized["build"]["result"]["status"], "succeeded")

    def test_index_ranges_are_deduplicated_and_bounded(self) -> None:
        self.assertEqual(parse_indices("1,3-5,4", maximum=5), (1, 3, 4, 5))
        with self.assertRaisesRegex(Exception, "within 1-5"):
            parse_indices("5-6", maximum=5)

    def test_evaluation_identity_covers_source_policy_and_implementation(self) -> None:
        case = {
            "case_id": "c-demo",
            "repo": "example/demo",
            "source": {"path": "/src/demo", "revision": "abc"},
        }
        first = evaluation_identity(
            case,
            manifest_sha256="m",
            ground_truth_sha256="g",
            implementation_sha256="one",
            policy={"timeout": 1},
        )
        second = evaluation_identity(
            case,
            manifest_sha256="m",
            ground_truth_sha256="g",
            implementation_sha256="two",
            policy={"timeout": 1},
        )
        self.assertNotEqual(first["digest"], second["digest"])

    def test_only_complete_digest_valid_records_are_reused(self) -> None:
        identity = {"payload": {"case": "demo"}}
        import hashlib

        encoded = json.dumps(identity["payload"], sort_keys=True, separators=(",", ":")).encode()
        identity["digest"] = hashlib.sha256(encoded).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "record.json"
            path.write_text(json.dumps({"complete": True, "evaluation_identity": identity}))
            self.assertIsNotNone(reusable_record(path, identity))
            path.write_text(json.dumps({"complete": False, "evaluation_identity": identity}))
            self.assertIsNone(reusable_record(path, identity))

    def test_summary_keeps_build_environment_and_strict_test_separate(self) -> None:
        records = [
            record("py-ok", "python"),
            record(
                "cpp-skipped",
                "cpp",
                outcome="succeeded",
                strict=False,
                layers=("passed", "skipped", "passed"),
            ),
            record(
                "java-fail",
                "java",
                build="failed",
                outcome="build_failed",
                strict=False,
                layers=("", "", ""),
            ),
        ]

        result = summarize(
            records,
            suite_id="suite",
            expected_case_count=3,
            selected_indices=(21, 22, 23),
            implementation_sha256="implementation",
            policy={"llm_enabled": False},
        )

        self.assertTrue(result["complete"])
        self.assertEqual(result["expected_case_count"], 3)
        self.assertEqual(result["selected_indices"], [21, 22, 23])
        self.assertEqual(result["standard_build_success"], 2)
        self.assertEqual(result["environment_success"], 2)
        self.assertEqual(result["strict_test_success"], 1)
        self.assertEqual(result["verification_layers"]["testability"]["skipped"], 1)
        self.assertEqual(result["languages"]["cpp"]["strict_test_succeeded"], 0)

    def test_summary_separates_artifact_presence_from_proven_runnability(self) -> None:
        records = [
            record(
                "artifact-only",
                "cpp",
                strict=False,
                runtime_metadata={
                    "runtime_outcome_category": "compiled-artifact-present",
                    "runtime_evidence_strength": "limited",
                    "runtime_contract": "compiled-library-availability",
                    "runtime_semantically_proven": False,
                },
            ),
            record(
                "service",
                "python",
                runtime_metadata={
                    "runtime_outcome_category": "service-responsive",
                    "runtime_evidence_strength": "strong",
                    "runtime_contract": "service-health",
                    "runtime_semantically_proven": True,
                },
            ),
        ]

        result = summarize(
            records,
            suite_id="suite",
            expected_case_count=2,
            implementation_sha256="implementation",
            policy={"llm_enabled": False},
        )

        self.assertEqual(result["schema_version"], 3)
        self.assertEqual(result["runnability_semantically_proven"], 1)
        self.assertEqual(result["artifact_only_runnability_pass"], 1)
        self.assertEqual(
            result["runtime_evidence_strength_distribution"],
            {"limited": 1, "strong": 1},
        )
        self.assertFalse(result["projects"][0]["runnability_semantically_proven"])

    def test_subset_summary_completion_uses_selected_case_count(self) -> None:
        result = summarize(
            [record("cpp-one", "cpp"), record("cpp-two", "cpp")],
            suite_id="suite",
            expected_case_count=2,
            selected_indices=(29, 30),
            implementation_sha256="implementation",
            policy={"llm_enabled": False},
        )

        self.assertTrue(result["complete"])
        self.assertEqual(result["record_count"], 2)
        self.assertEqual(result["selected_indices"], [29, 30])


if __name__ == "__main__":
    unittest.main()

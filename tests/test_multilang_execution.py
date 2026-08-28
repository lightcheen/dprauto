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


def record(
    case_id: str,
    language: str,
    *,
    build: str = "succeeded",
    outcome: str = "succeeded",
    strict: bool = True,
    layers: tuple[str, str, str] = ("passed", "passed", "passed"),
) -> dict:
    return {
        "case": {
            "case_id": case_id,
            "repo": f"example/{case_id}",
            "primary_language": language,
        },
        "build": {"result": {"status": build}},
        "verification": {
            "results": [
                {"level": name, "status": status}
                for name, status in zip(
                    ("installability", "testability", "runnability"),
                    layers,
                )
            ]
        },
        "outcome": {
            "status": outcome,
            "category": "none" if outcome == "succeeded" else "testability",
            "strict_test_succeeded": strict,
        },
        "elapsed_seconds": 10.0,
    }


class MultilangExecutionTests(unittest.TestCase):
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
            implementation_sha256="implementation",
            policy={"llm_enabled": False},
        )

        self.assertTrue(result["complete"])
        self.assertEqual(result["standard_build_success"], 2)
        self.assertEqual(result["environment_success"], 2)
        self.assertEqual(result["strict_test_success"], 1)
        self.assertEqual(result["verification_layers"]["testability"]["skipped"], 1)
        self.assertEqual(result["languages"]["cpp"]["strict_test_succeeded"], 0)


if __name__ == "__main__":
    unittest.main()

import argparse
import hashlib
import json
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from evaluations.prompt12.run_evaluation import (
    ensure_docker_networks,
    ensure_poetry_tool_images,
    evaluation_identity,
    evaluation_policy,
    load_source,
    make_config,
    required_poetry_python_versions,
    run_one,
    runnability_contract as _runnability_contract,
    runnability_evidence_strength as _runnability_evidence_strength,
    runnability_outcome_category as _runnability_outcome_category,
    runnability_semantically_passed as _runnability_semantically_passed,
    selected_indices,
    slug,
    summarize,
    testability_evidence_strength as _testability_evidence_strength,
    testability_observed_count as _testability_observed_count,
    testability_outcome_category as _testability_outcome_category,
    testability_semantically_passed as _testability_semantically_passed,
)


BASELINE = (
    Path(__file__).resolve().parents[1]
    / "evaluations"
    / "baselines"
    / "python-21-20260818.json"
)


class EvaluationHarnessTests(unittest.TestCase):
    def test_required_poetry_versions_follow_selected_project_constraint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.mkdir()
            (source / "pyproject.toml").write_text(
                "[tool.poetry]\n"
                'name = "fixture"\n'
                'version = "1.0"\n'
                "[tool.poetry.dependencies]\n"
                'python = "~3.9"\n'
                "[build-system]\n"
                'requires = ["poetry-core"]\n'
                'build-backend = "poetry.core.masonry.api"\n',
                encoding="utf-8",
            )
            cases = [(0, {"repo": "example/poetry", "source": str(source)})]

            versions = required_poetry_python_versions(
                cases, make_config(Path(directory) / "output")
            )

            self.assertEqual(versions, ("3.9",))

    def test_poetry_tool_image_is_built_once_and_fingerprinted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            config = make_config(output)
            calls = []
            fingerprint = ""

            def fake_run(argv, **kwargs):
                nonlocal fingerprint
                calls.append(list(argv))
                if argv[1:3] == ["image", "inspect"]:
                    if not fingerprint:
                        return CompletedProcess(argv, 1, stdout="missing")
                    return CompletedProcess(argv, 0, stdout=fingerprint + "\n")
                dockerfile = kwargs["input"]
                fingerprint = dockerfile.rsplit(
                    "dprauto.tool.recipe-sha256=", 1
                )[1].strip()
                return CompletedProcess(argv, 0, stdout="built")

            with patch(
                "evaluations.prompt12.run_evaluation.subprocess.run",
                side_effect=fake_run,
            ):
                images = ensure_poetry_tool_images(config, ("3.11",), output)

            self.assertEqual(len(images), 1)
            self.assertEqual(images[0]["python_version"], "3.11")
            self.assertEqual(len(calls), 3)
            self.assertIn("--network", calls[1])
            self.assertEqual(calls[1][calls[1].index("--network") + 1], "host")
            self.assertEqual(calls[1].count("--build-arg"), 6)
            self.assertIn("HTTP_PROXY", calls[1])
            self.assertTrue((output / "preflight" / "poetry-tool-images.json").is_file())

    def test_poetry_tool_timeout_preserves_binary_partial_log(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            config = make_config(output)

            def fake_run(argv, **kwargs):
                if argv[1:3] == ["image", "inspect"]:
                    return CompletedProcess(argv, 1, stdout="missing")
                raise subprocess.TimeoutExpired(
                    argv,
                    timeout=1,
                    output=b"partial build output\n",
                )

            with patch(
                "evaluations.prompt12.run_evaluation.subprocess.run",
                side_effect=fake_run,
            ):
                with self.assertRaisesRegex(RuntimeError, "exceeded"):
                    ensure_poetry_tool_images(config, ("3.11",), output)

            log = output / "preflight" / "poetry-python-3.11.log"
            self.assertIn("partial build output", log.read_text(encoding="utf-8"))
            self.assertIn("timed out", log.read_text(encoding="utf-8"))

    def test_evaluation_network_is_created_idempotently(self) -> None:
        config = make_config(Path("/tmp/dprauto-network-test"))
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(list(argv))
            if argv[2] == "inspect":
                return CompletedProcess(argv, 1, stdout="not found")
            return CompletedProcess(argv, 0, stdout="network-id")

        with patch(
            "evaluations.prompt12.run_evaluation.subprocess.run",
            side_effect=fake_run,
        ):
            ensure_docker_networks(config)

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][-1], "dprauto-eval")
        self.assertEqual(calls[1][1:4], ["network", "create", "--driver"])
        self.assertIn("dprauto.evaluation=true", calls[1])

    def test_frozen_python_21_baseline_is_complete_and_self_consistent(self) -> None:
        baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
        cases = baseline["cases"]

        self.assertEqual(baseline["schema_version"], 1)
        self.assertEqual(baseline["selection"]["indices"], list(range(4, 25)))
        self.assertEqual(len(cases), 21)
        self.assertEqual([case["index"] for case in cases], list(range(4, 25)))
        self.assertEqual(len({case["repo"] for case in cases}), 21)
        self.assertTrue(all(case["revision"] for case in cases))
        self.assertTrue(all(Path(case["source"]).is_absolute() for case in cases))
        repository_root = Path(__file__).resolve().parents[1]
        source_manifest = repository_root / baseline["selection"]["source_manifest"]
        self.assertEqual(
            hashlib.sha256(source_manifest.read_bytes()).hexdigest(),
            baseline["source_manifest_sha256"],
        )
        source_summary = repository_root / baseline["source_run"] / "summary.json"
        if source_summary.is_file():
            self.assertEqual(
                hashlib.sha256(source_summary.read_bytes()).hexdigest(),
                baseline["source_summary_sha256"],
            )
        derived_metrics = {
            "project_total": len(cases),
            "standard_build_success": sum(
                case["standard_build_status"] == "succeeded" for case in cases
            ),
            "final_environment_success": sum(
                case["final_status"] == "succeeded" for case in cases
            ),
            "entered_agent": sum(case["agent_participated"] for case in cases),
            "agent_repair_success": sum(
                case["agent_participated"] and case["final_status"] == "succeeded"
                for case in cases
            ),
            "installability_pass": sum(
                case["verification"]["installability"] == "passed" for case in cases
            ),
            "testability_pass": sum(
                case["verification"]["testability"] == "passed" for case in cases
            ),
            "runnability_pass": sum(
                case["verification"]["runnability"] == "passed" for case in cases
            ),
            "regression_count": sum(case["final_status"] == "regression" for case in cases),
        }
        self.assertEqual(baseline["metrics"], derived_metrics)

    def test_selected_indices_accepts_values_and_ranges(self) -> None:
        self.assertEqual(selected_indices("5,8,10,12,17,23-24"), {5, 8, 10, 12, 17, 23, 24})

    def test_selected_indices_rejects_empty_or_invalid_ranges(self) -> None:
        with self.assertRaises(argparse.ArgumentTypeError):
            selected_indices("")
        with self.assertRaises(argparse.ArgumentTypeError):
            selected_indices("8-5")

    def test_record_without_evaluation_identity_is_not_reused(self) -> None:
        class RecordingWorkflow:
            called = False

            def run(self, run_id, source, workspace):
                self.called = True
                raise RuntimeError("recording workflow intentionally stops")

            @staticmethod
            def persisted_state(run_id):
                return {}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            output = root / "output"
            source.mkdir()
            repo = "example/stale-record"
            record = output / "records" / f"04-{slug(repo)}.json"
            record.parent.mkdir(parents=True)
            record.write_text('{"stale": true}\n', encoding="utf-8")
            workflow = RecordingWorkflow()

            run_one(
                workflow,
                output / "artifacts",
                output / "llm-logs",
                output,
                {
                    "repo": repo,
                    "source": str(source),
                    "historical_cnb_status": "failure",
                },
                3,
            )

            self.assertTrue(workflow.called, "a record without a matching identity is stale")

    def test_matching_evaluation_identity_reuses_record(self) -> None:
        class RecordingWorkflow:
            calls = 0

            def run(self, run_id, source, workspace):
                self.calls += 1
                return None

            @staticmethod
            def persisted_state(run_id):
                return {}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            output = root / "output"
            source.mkdir()
            (source / "pyproject.toml").write_text(
                '[project]\nname = "identity"\n', encoding="utf-8"
            )
            case = {
                "repo": "example/matching-record",
                "source": str(source),
                "historical_cnb_status": "failure",
            }
            workflow = RecordingWorkflow()

            first = run_one(
                workflow,
                output / "artifacts",
                output / "llm-logs",
                output,
                case,
                3,
            )
            second = run_one(
                workflow,
                output / "artifacts",
                output / "llm-logs",
                output,
                case,
                3,
            )

            self.assertEqual(workflow.calls, 1)
            self.assertEqual(first["evaluation_identity"], second["evaluation_identity"])

    def test_evaluation_identity_changes_with_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_path = root / "source"
            source_path.mkdir()
            case = {
                "repo": "example/policy-change",
                "source": str(source_path),
                "historical_cnb_status": "failure",
            }
            source = load_source(source_path, case["repo"])
            original = make_config(root / "original")
            changed = replace(
                original,
                build=replace(original.build, timeout_seconds=301),
            )

            before = evaluation_identity(
                case,
                3,
                source_path,
                source,
                original,
                implementation_sha256="fixed-implementation",
            )
            after = evaluation_identity(
                case,
                3,
                source_path,
                source,
                changed,
                implementation_sha256="fixed-implementation",
            )

            self.assertNotEqual(before["digest"], after["digest"])

    def test_evaluation_policy_records_test_slice_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))

            policy = evaluation_policy(config)

        self.assertEqual(
            policy["verification"]["max_test_files_per_slice"],
            8,
        )

    def test_evaluation_policy_records_llm_timeout_attempt_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))

            policy = evaluation_policy(config)

        self.assertEqual(
            policy["llm"]["max_timeout_attempts_per_operation"],
            2,
        )

    def test_evaluation_identity_changes_with_source_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_path = root / "source"
            source_path.mkdir()
            source_file = source_path / "pyproject.toml"
            source_file.write_text('[project]\nname = "before"\n', encoding="utf-8")
            case = {
                "repo": "example/source-change",
                "source": str(source_path),
                "historical_cnb_status": "failure",
            }
            source = load_source(source_path, case["repo"])
            config = make_config(root / "output")
            before = evaluation_identity(
                case,
                3,
                source_path,
                source,
                config,
                implementation_sha256="fixed-implementation",
            )

            source_file.write_text('[project]\nname = "after"\n', encoding="utf-8")
            after = evaluation_identity(
                case,
                3,
                source_path,
                source,
                config,
                implementation_sha256="fixed-implementation",
            )

            self.assertNotEqual(before["digest"], after["digest"])

    def test_summarize_counts_final_status_regressions(self) -> None:
        record = {
            "repo": "example/regression",
            "source_path": "/tmp/source",
            "workspace_path": "/tmp/workspace",
            "historical_cnb_status": "success",
            "standard_build": {
                "status": "succeeded",
                "started_at": "2026-08-14T00:00:00+00:00",
                "finished_at": "2026-08-14T00:00:01+00:00",
            },
            "elapsed_seconds": 3.0,
            "final_result": {
                "agent_participated": True,
                "final_status": "regression",
                "repair_attempts": 1,
            },
            "initial_failure": {"category": "test"},
            "state_metrics": {
                "stop_reason": "repair cost regression",
                "ineffective_modifications": 0,
                "duplicate_repair_plan": 0,
            },
            "llm": {
                "calls": 0,
                "api_seconds": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
            },
        }

        summary = summarize([record])

        self.assertEqual(summary["repair_quality"]["regression_count"], 1)
        self.assertEqual(summary["repair_quality"]["regression_rate"], 1.0)

    def test_summarize_separates_policy_skip_from_test_failure(self) -> None:
        record = {
            "repo": "example/requires-secret",
            "source_path": "/tmp/source",
            "workspace_path": "/tmp/workspace",
            "historical_cnb_status": "failure",
            "standard_build": {
                "status": "succeeded",
                "started_at": "2026-08-24T00:00:00+00:00",
                "finished_at": "2026-08-24T00:00:01+00:00",
            },
            "elapsed_seconds": 2.0,
            "final_result": {
                "agent_participated": False,
                "final_status": "succeeded",
                "repair_attempts": 0,
                "installability": {"status": "passed"},
                "testability": {"status": "skipped"},
                "runnability": {"status": "passed"},
            },
            "initial_failure": None,
            "state_metrics": {
                "stop_reason": "",
                "ineffective_modifications": 0,
                "duplicate_repair_plan": 0,
            },
            "llm": {
                "calls": 0,
                "api_seconds": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
            },
        }

        summary = summarize([record])

        self.assertEqual(summary["verification"]["testability_pass"], 0)
        self.assertEqual(summary["verification"]["testability_skipped"], 1)
        self.assertEqual(summary["verification"]["testability_failed_or_error"], 0)
        self.assertEqual(summary["verification"]["build_success_test_failure"], 0)

    def test_summarize_uses_terminal_failure_for_unresolved_infrastructure(self) -> None:
        record = {
            "repo": "example/network-after-repair",
            "source_path": "/tmp/source",
            "workspace_path": "/tmp/workspace",
            "historical_cnb_status": "success",
            "standard_build": {
                "status": "failed",
                "started_at": "2026-08-18T00:00:00+00:00",
                "finished_at": "2026-08-18T00:00:01+00:00",
            },
            "elapsed_seconds": 3.0,
            "final_result": {
                "agent_participated": True,
                "final_status": "infrastructure_failed",
                "repair_attempts": 1,
                "failure": {
                    "category": "network",
                    "kind": "network",
                    "infrastructure_related": True,
                    "key_log": "Temporary failure resolving deb.debian.org",
                },
            },
            "initial_failure": {
                "category": "system_dependency",
                "kind": "project_build",
                "infrastructure_related": False,
            },
            "state_metrics": {
                "stop_reason": "infrastructure failure",
                "ineffective_modifications": 0,
                "duplicate_repair_plan": 0,
            },
            "llm": {
                "calls": 2,
                "api_seconds": 1,
                "input_tokens": 10,
                "output_tokens": 5,
                "total_tokens": 15,
            },
        }

        summary = summarize([record])

        self.assertEqual(summary["failure_type_distribution"], {"system_dependency": 1})
        self.assertEqual(summary["unresolved_failure_types"], {"network": 1})
        self.assertEqual(summary["infrastructure"], {"network_failure": 1})
        self.assertEqual(summary["projects"][0]["final_failure_category"], "network")

    def test_summarize_separates_agent_build_repair_from_runtime_failure(self) -> None:
        record = {
            "repo": "example/repaired-native-library",
            "source_path": "/tmp/source",
            "workspace_path": "/tmp/workspace",
            "historical_cnb_status": "failure",
            "standard_build": {
                "status": "failed",
                "started_at": "2026-09-06T00:00:00+00:00",
                "finished_at": "2026-09-06T00:00:01+00:00",
            },
            "elapsed_seconds": 3.0,
            "final_result": {
                "agent_participated": True,
                "final_status": "verification_failed",
                "repair_attempts": 2,
                "build_result": {"status": "succeeded"},
                "installability": {"status": "passed"},
                "testability": {
                    "status": "passed",
                    "metadata": {
                        "outcome_category": "tests-passed",
                        "test_evidence_strength": "strong",
                        "observed_test_count": 12,
                        "candidate_fallback_used": True,
                        "command_attempts": ({"candidate": 1}, {"candidate": 2}),
                    },
                },
                "runnability": {"status": "failed"},
                "failure": {"category": "run", "kind": "project_build"},
            },
            "initial_failure": {
                "category": "system_dependency",
                "kind": "project_build",
            },
            "state_metrics": {
                "stop_reason": "verification repair rejected",
                "ineffective_modifications": 0,
                "duplicate_repair_plan": 0,
            },
            "llm": {
                "calls": 2,
                "api_seconds": 1,
                "input_tokens": 10,
                "output_tokens": 5,
                "total_tokens": 15,
            },
        }

        summary = summarize([record])

        self.assertEqual(summary["summary_schema_version"], 4)
        self.assertEqual(summary["agent_capability"]["build_repair_candidates"], 1)
        self.assertEqual(summary["agent_capability"]["build_repair_success"], 1)
        self.assertEqual(summary["agent_capability"]["environment_repair_success"], 0)
        self.assertEqual(summary["agent_capability"]["agent_repair_success"], 0)
        self.assertEqual(
            summary["agent_build_repaired_failure_types"],
            {"system_dependency": 1},
        )
        self.assertEqual(
            summary["verification"]["failure_stage_distribution"],
            {"runnability": 1},
        )
        self.assertEqual(summary["verification"]["test_success_run_failure"], 1)
        self.assertEqual(
            summary["verification"]["testability_outcome_categories"],
            {"tests-passed": 1},
        )
        self.assertEqual(
            summary["verification"]["test_evidence_strength_distribution"],
            {"strong": 1},
        )
        self.assertEqual(summary["verification"]["observed_test_count_total"], 12)
        self.assertEqual(summary["verification"]["test_candidate_fallbacks"], 1)
        self.assertEqual(summary["verification"]["runnability_pass"], 0)
        self.assertEqual(summary["verification"]["reported_runnability_pass"], 0)
        self.assertEqual(
            summary["verification"]["runnability_outcome_categories"],
            {"runtime-command-failure": 1},
        )
        self.assertTrue(summary["projects"][0]["build_repaired_by_agent"])
        self.assertFalse(summary["projects"][0]["environment_repaired_by_agent"])
        self.assertEqual(summary["projects"][0]["final_failure_stage"], "runnability")
        self.assertEqual(summary["projects"][0]["testability_outcome_category"], "tests-passed")
        self.assertEqual(summary["projects"][0]["test_candidate_attempts"], 2)

    def test_legacy_testability_output_reclassifies_zero_test_false_success(self) -> None:
        record = {
            "final_result": {
                "testability": {
                    "status": "passed",
                    "checks": [
                        {
                            "metadata": {
                                "output_excerpt": (
                                    "Internal ctest changing into directory /workspace/build\n"
                                    "No tests were found!!!\n"
                                )
                            }
                        }
                    ],
                }
            }
        }

        self.assertEqual(
            _testability_outcome_category(record),
            "no-tests-collected",
        )
        self.assertEqual(_testability_observed_count(record), 0)
        self.assertEqual(_testability_evidence_strength(record), "limited")
        self.assertFalse(_testability_semantically_passed(record))

    def test_legacy_compiled_artifact_pass_is_limited_runnability_evidence(self) -> None:
        record = {
            "final_result": {
                "runnability": {
                    "status": "passed",
                    "checks": [
                        {"name": "library-artifact", "status": "passed"},
                        {
                            "name": "library-artifact-or-tests",
                            "status": "passed",
                            "metadata": {
                                "artifact_count": 3,
                                "project_tests_passed": False,
                            },
                        },
                    ],
                }
            }
        }

        self.assertEqual(
            _runnability_outcome_category(record),
            "compiled-artifact-present",
        )
        self.assertEqual(_runnability_evidence_strength(record), "limited")
        self.assertEqual(_runnability_contract(record), "compiled-library-availability")
        self.assertFalse(_runnability_semantically_passed(record))


if __name__ == "__main__":
    unittest.main()

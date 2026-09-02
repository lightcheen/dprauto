#!/usr/bin/env python3
"""Score production probe output against evaluator-only workspace ground truth."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any


HERE = Path(__file__).resolve().parent
DEFAULT_PROBE = HERE / "probe-results.json"
DEFAULT_ORACLE = HERE / "hidden-oracles" / "workspaces.json"
DEFAULT_OUTPUT = HERE / "quality-report.json"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def predicted_build_system(record: dict[str, Any]) -> str:
    reported = str(record.get("primary_build_system", "")).casefold()
    if reported:
        return reported
    managers = {str(item).casefold() for item in record.get("package_managers", [])}
    build_files = {PurePosixPath(path).name for path in record.get("build_files", [])}
    if "poetry" in managers:
        return "poetry"
    if "pyproject.toml" in build_files:
        return "python-packaging"
    if build_files & {"setup.py", "setup.cfg"}:
        return "setuptools"
    return ""


def expected_primary(case: dict[str, Any]) -> tuple[set[str], set[str]]:
    primary_ids = set(case["primary_component_ids"])
    components = [
        component
        for component in case["components"]
        if component["component_id"] in primary_ids
    ]
    roots = {component["root"] for component in components}
    systems = {
        entry["build_system"].casefold()
        for component in components
        for entry in component["build_entries"]
        if entry["role"] == "primary"
    }
    return roots, systems


def metric(passed: int, total: int) -> dict[str, int | float]:
    return {"passed": passed, "total": total, "rate": round(passed / total, 6)}


def score(probe: dict[str, Any], oracle: dict[str, Any]) -> dict[str, Any]:
    probe_cases = {record["case_id"]: record for record in probe["records"]}
    oracle_cases = {case["case_id"]: case for case in oracle["cases"]}
    if set(probe_cases) != set(oracle_cases):
        raise ValueError("probe and workspace oracle case IDs differ")

    discovery_passed = 0
    selection_passed = 0
    system_passed = 0
    plan_passed = 0
    false_positive_candidates = 0
    false_success_plans = 0
    scan_truncated = 0
    statuses: Counter[str] = Counter()
    records: list[dict[str, Any]] = []

    for case_id in sorted(oracle_cases):
        observed = probe_cases[case_id]
        expected_roots, expected_systems = expected_primary(oracle_cases[case_id])
        candidate_roots = {
            str(candidate.get("root", ""))
            for candidate in observed.get("component_candidates", [])
        }
        selected_root = str(observed.get("selected_component_root", ""))
        selected_system = predicted_build_system(observed)
        discovered = bool(candidate_roots & expected_roots)
        selected = selected_root in expected_roots
        system_correct = selected_system in expected_systems
        planned = observed.get("status") == "planned"
        false_candidates = len(candidate_roots - expected_roots)
        false_success = planned and not (selected and system_correct)

        discovery_passed += int(discovered)
        selection_passed += int(selected)
        system_passed += int(system_correct)
        plan_passed += int(planned)
        false_positive_candidates += false_candidates
        false_success_plans += int(false_success)
        scan_truncated += int(bool(observed.get("scan_truncated", False)))
        statuses[str(observed.get("status", "missing"))] += 1
        records.append(
            {
                "case_id": case_id,
                "component_discovered": discovered,
                "primary_selected": selected,
                "build_system_correct": system_correct,
                "production_plan_created": planned,
                "false_positive_candidates": false_candidates,
                "false_success_plan": false_success,
            }
        )

    total = len(oracle_cases)
    return {
        "schema_version": 1,
        "suite_id": oracle["suite_id"],
        "scope": "static discovery/parser/production-planning quality; no Docker execution",
        "identity": {
            "probe_sha256": "",
            "workspace_oracle_sha256": "",
        },
        "metrics": {
            "component_discovery_recall_at_k": metric(discovery_passed, total),
            "primary_selection_accuracy_at_1": metric(selection_passed, total),
            "build_system_accuracy": metric(system_passed, total),
            "production_plan_success": metric(plan_passed, total),
            "false_positive_candidate_count": false_positive_candidates,
            "false_success_plan_count": false_success_plans,
            "scan_truncated_count": scan_truncated,
            "status_counts": dict(sorted(statuses.items())),
        },
        "execution_metrics": {
            "status": "not_run",
            "installability": None,
            "testability": None,
            "runnability": None,
            "strict_success": None,
        },
        "records": records,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe", type=Path, default=DEFAULT_PROBE)
    parser.add_argument("--oracle", type=Path, default=DEFAULT_ORACLE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    probe = json.loads(args.probe.read_text(encoding="utf-8"))
    oracle = json.loads(args.oracle.read_text(encoding="utf-8"))
    report = score(probe, oracle)
    report["identity"] = {
        "probe_sha256": sha256(args.probe),
        "workspace_oracle_sha256": sha256(args.oracle),
    }
    args.output.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    metrics = report["metrics"]
    for name in (
        "component_discovery_recall_at_k",
        "primary_selection_accuracy_at_1",
        "build_system_accuracy",
        "production_plan_success",
    ):
        value = metrics[name]
        print(f"{name}: {value['passed']}/{value['total']} ({value['rate']:.2%})")
    print(f"false_positive_candidate_count: {metrics['false_positive_candidate_count']}")
    print(f"false_success_plan_count: {metrics['false_success_plan_count']}")


if __name__ == "__main__":
    main()

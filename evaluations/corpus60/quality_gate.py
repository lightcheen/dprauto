#!/usr/bin/env python3
"""Run the reproducible P0 corpus quality gate and reject metric regressions."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any


HERE = Path(__file__).resolve().parent
REPOSITORY_ROOT = HERE.parents[1]
THRESHOLDS = HERE / "quality-thresholds.json"
REPORT = HERE / "quality-report.json"


def run(command: tuple[str, ...], *, environment: dict[str, str] | None = None) -> None:
    subprocess.run(
        command,
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=True,
    )


def compare(metrics: dict[str, Any], thresholds: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for name, minimum in thresholds["minimum_passed"].items():
        observed = int(metrics.get(name, {}).get("passed", -1))
        if observed < minimum:
            errors.append(f"{name} regressed: {observed} < {minimum}")
    for name, maximum in thresholds["maximum_counts"].items():
        if name.startswith("status:"):
            observed = int(metrics.get("status_counts", {}).get(name.partition(":")[2], 0))
        else:
            if name not in metrics:
                errors.append(f"required count is missing: {name}")
                continue
            observed = int(metrics[name])
        if observed > maximum:
            errors.append(f"{name} regressed: {observed} > {maximum}")
    return errors


def main() -> None:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(REPOSITORY_ROOT / "src")
    python = sys.executable
    commands = (
        (python, str(HERE / "validate_corpus.py"), "--require-sources", "--require-freeze"),
        (python, str(HERE / "validate_workspace_ground_truth.py")),
        (python, str(REPOSITORY_ROOT / "evaluations/holdout/validate_holdout.py")),
        (python, str(HERE / "probe_dprauto.py")),
        (python, str(HERE / "score_probe.py")),
    )
    for command in commands:
        run(command, environment=environment)

    thresholds = json.loads(THRESHOLDS.read_text(encoding="utf-8"))
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    errors = compare(report["metrics"], thresholds)
    if thresholds.get("suite_id") != report.get("suite_id"):
        errors.append("quality thresholds and report refer to different suites")
    if report["execution_metrics"]["status"] != "not_run":
        errors.append("P0 static gate must not represent execution metrics as completed")
    if errors:
        raise SystemExit("quality gate failed:\n- " + "\n- ".join(errors))
    print("P0 corpus quality gate passed without claiming Docker execution success")


if __name__ == "__main__":
    main()

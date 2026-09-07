#!/usr/bin/env python3
"""Run the pinned multilingual corpus through deterministic build and verification.

This harness deliberately does not construct an Agent or LLM client.  It reuses
the production parser, deterministic strategy portfolio, and layered verifier,
while adding evaluation identity, per-case checkpoints, and aggregate metrics.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
import time
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from dprauto.adapters.multilang import MultiLanguageProjectParser
from dprauto.adapters.storage import LocalArtifactStorage
from dprauto.application import create_deterministic_builder, create_layered_verifier
from dprauto.config import BuildConfig, VerificationConfig
from dprauto.domain.enums import BuildStatus, VerificationLevel, VerificationStatus
from dprauto.domain.models import SourceReference
from dprauto.serialization import to_json_bytes

try:  # Support both module import in tests and direct script execution.
    from .run_evaluation import DEFAULT_MANIFEST, load_and_validate
except ImportError:  # pragma: no cover - exercised by the real CLI invocation.
    from run_evaluation import DEFAULT_MANIFEST, load_and_validate


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
IDENTITY_SCHEMA_VERSION = 1
RECORD_SCHEMA_VERSION = 1
SUMMARY_SCHEMA_VERSION = 3
DEFAULT_CASE_TIMEOUT_SECONDS = 4_800


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def implementation_digest() -> str:
    """Hash production Python and this harness in stable path order."""

    paths = sorted((REPOSITORY_ROOT / "src" / "dprauto").rglob("*.py"))
    paths.append(Path(__file__).resolve())
    digest = hashlib.sha256()
    for path in paths:
        relative = path.relative_to(REPOSITORY_ROOT).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def evaluation_policy(
    build_config: BuildConfig,
    verification_config: VerificationConfig,
    *,
    case_timeout_seconds: int,
) -> dict[str, Any]:
    return {
        "agent_enabled": False,
        "llm_enabled": False,
        "build": asdict(build_config),
        "verification": asdict(verification_config),
        "case_timeout_seconds": case_timeout_seconds,
        "source_workspaces_are_read_only": True,
    }


def evaluation_identity(
    case: Mapping[str, Any],
    *,
    manifest_sha256: str,
    ground_truth_sha256: str,
    implementation_sha256: str,
    policy: Mapping[str, Any],
) -> dict[str, Any]:
    source = case["source"]
    payload = {
        "schema_version": IDENTITY_SCHEMA_VERSION,
        "suite_case_id": case["case_id"],
        "repo": case["repo"],
        "source_path": source["path"],
        "source_revision": source["revision"],
        "manifest_sha256": manifest_sha256,
        "ground_truth_sha256": ground_truth_sha256,
        "implementation_sha256": implementation_sha256,
        "policy": policy,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {"payload": payload, "digest": _sha256_bytes(encoded)}


def reusable_record(path: Path, expected_identity: Mapping[str, Any]) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or not value.get("complete"):
        return None
    identity = value.get("evaluation_identity")
    if identity != expected_identity or not isinstance(identity, dict):
        return None
    payload = identity.get("payload")
    if not isinstance(payload, dict):
        return None
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if identity.get("digest") != _sha256_bytes(encoded):
        return None
    return value


def _atomic_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(to_json_bytes(value))
    temporary.replace(path)


def _json_compatible(value: Any) -> dict[str, Any]:
    """Use the persisted representation for both live and resumed aggregation."""

    normalized = json.loads(to_json_bytes(value))
    if not isinstance(normalized, dict):
        raise TypeError("evaluation records must serialize to JSON objects")
    return normalized


def parse_indices(value: str, *, maximum: int) -> tuple[int, ...]:
    selected: set[int] = set()
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        match = re.fullmatch(r"(\d+)(?:-(\d+))?", item)
        if not match:
            raise argparse.ArgumentTypeError(f"invalid index or range: {item!r}")
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if start > end or start < 1 or end > maximum:
            raise argparse.ArgumentTypeError(
                f"index range {item!r} must be within 1-{maximum}"
            )
        selected.update(range(start, end + 1))
    if not selected:
        raise argparse.ArgumentTypeError("at least one index is required")
    return tuple(sorted(selected))


def _verification_statuses(report: Any) -> dict[str, str]:
    if report is None:
        return {}
    return {result.level.value: result.status.value for result in report.results}


def _strict_verification_succeeded(report: Any) -> bool:
    statuses = _verification_statuses(report)
    raw_passed = all(
        statuses.get(level.value) == VerificationStatus.PASSED.value
        for level in (
            VerificationLevel.INSTALLABILITY,
            VerificationLevel.TESTABILITY,
            VerificationLevel.RUNNABILITY,
        )
    )
    runnability = report.result_for(VerificationLevel.RUNNABILITY)
    runtime_proven = (
        runnability.metadata.get("runtime_semantically_proven", True)
        if runnability is not None
        else False
    )
    return bool(raw_passed and runtime_proven)


def _outcome(execution: Any, report: Any, error: str = "") -> dict[str, Any]:
    if error:
        return {"status": "runner_error", "category": "runner", "summary": error}
    result = execution.result
    failure = execution.failure
    if result.status is not BuildStatus.SUCCEEDED:
        infrastructure = bool(failure and failure.infrastructure_related)
        return {
            "status": "infrastructure_failed" if infrastructure else "build_failed",
            "category": failure.category.value if failure else "unclassified",
            "summary": failure.message if failure else result.summary,
        }
    if report is None:
        return {
            "status": "runner_error",
            "category": "verification",
            "summary": "successful build produced no verification report",
        }
    statuses = _verification_statuses(report)
    if report.succeeded:
        return {
            "status": "succeeded",
            "category": "none",
            "summary": "build and layered environment verification succeeded",
            "strict_test_succeeded": _strict_verification_succeeded(report),
            "verification_statuses": statuses,
        }
    failed = [name for name, status in statuses.items() if status in {"failed", "error"}]
    return {
        "status": "verification_failed",
        "category": failed[0] if failed else "verification",
        "summary": "one or more layered verification checks failed",
        "strict_test_succeeded": False,
        "verification_statuses": statuses,
    }


def _remove_evaluation_image(image: str, docker_binary: str) -> dict[str, Any]:
    if not image:
        return {"attempted": False, "removed": False}
    completed = subprocess.run(
        [docker_binary, "image", "rm", image],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    return {
        "attempted": True,
        "removed": completed.returncode == 0,
        "exit_code": completed.returncode,
        "output_excerpt": completed.stdout[-2_000:],
    }


def run_case(
    case: Mapping[str, Any],
    ground_truth: Mapping[str, Any],
    *,
    identity: Mapping[str, Any],
    parser: Any,
    builder: Any,
    verifier: Any,
    build_config: BuildConfig,
    case_timeout_seconds: int,
    retain_image: bool,
) -> dict[str, Any]:
    started_at = datetime.now(timezone.utc)
    started = time.monotonic()
    profile = None
    execution = None
    report = None
    error = ""
    cleanup: dict[str, Any] = {"attempted": False, "removed": False}
    source = case["source"]
    workspace = Path(source["path"]).resolve()
    deadline_at = started_at + timedelta(seconds=case_timeout_seconds)
    try:
        profile = parser.parse(
            SourceReference(str(workspace), str(source["revision"])),
            workspace,
        )
        execution = builder.build(profile, workspace, deadline_at=deadline_at)
        if execution.result.status is BuildStatus.SUCCEEDED:
            report = verifier.verify(
                profile,
                execution.result,
                workspace,
                build_plan=execution.plan,
                deadline_at=deadline_at,
            )
    except Exception as exc:  # Evaluation boundary: preserve the remaining corpus.
        error = f"{type(exc).__name__}: {exc}"
    finally:
        image = execution.result.image_reference if execution is not None else ""
        if image and not retain_image:
            cleanup = _remove_evaluation_image(image, build_config.docker_binary)
    finished_at = datetime.now(timezone.utc)
    outcome = _outcome(execution, report, error) if execution is not None else _outcome(None, None, error or "build did not start")
    return _json_compatible({
        "schema_version": RECORD_SCHEMA_VERSION,
        "complete": True,
        "evaluation_identity": identity,
        "case": dict(case),
        "ground_truth": dict(ground_truth),
        "source_workspace": str(workspace),
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "elapsed_seconds": time.monotonic() - started,
        "outcome": outcome,
        "profile": profile,
        "build": execution,
        "verification": report,
        "error": error,
        "image_cleanup": cleanup,
    })


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def _record_layer_status(record: Mapping[str, Any], level: str) -> str:
    result = _record_layer_result(record, level)
    return str(result.get("status", "")) if result else ""


def _record_layer_result(record: Mapping[str, Any], level: str) -> Mapping[str, Any]:
    verification = record.get("verification")
    if not isinstance(verification, dict):
        return {}
    results = verification.get("results")
    if not isinstance(results, list):
        return {}
    for result in results:
        if isinstance(result, dict) and result.get("level") == level:
            return result
    return {}


def _runnability_evidence(record: Mapping[str, Any]) -> tuple[str, str, str, bool]:
    result = _record_layer_result(record, "runnability")
    metadata = result.get("metadata") if isinstance(result, Mapping) else {}
    if not isinstance(metadata, Mapping):
        metadata = {}
    status = str(result.get("status", ""))
    outcome = str(metadata.get("runtime_outcome_category", ""))
    strength = str(metadata.get("runtime_evidence_strength", ""))
    contract = str(metadata.get("runtime_contract", ""))
    if not result:
        return "not-run", "none", "unknown", False
    if not outcome:
        checks = {
            str(check.get("name", "")): check
            for check in result.get("checks", ())
            if isinstance(check, Mapping)
        }
        names = set(checks)
        if status == "passed" and "web-http" in names:
            outcome, strength, contract = "service-responsive", "strong", "service-health"
        elif status == "passed" and "cli-output" in names:
            outcome, strength, contract = "cli-invocable", "moderate", "cli-entrypoint"
        elif status == "passed" and "script-observable-effect" in names:
            outcome, strength, contract = (
                "script-observable-execution",
                "strong",
                "script-observable-effect",
            )
        elif status == "passed" and "library-import" in names:
            outcome, strength, contract = "python-library-import-only", "limited", "library-load"
        elif status == "passed" and "library-artifact" in names:
            observed = checks.get("library-artifact-or-tests", {}).get(
                "metadata", {}
            )
            test_backed = bool(
                isinstance(observed, Mapping)
                and observed.get("project_tests_passed")
            )
            outcome, strength, contract = (
                (
                    "compiled-library-test-backed"
                    if test_backed
                    else "compiled-artifact-present"
                ),
                "moderate" if test_backed else "limited",
                "compiled-library-availability",
            )
        elif status == "passed":
            outcome, strength, contract = "runtime-command-passed", "moderate", "unknown"
        else:
            outcome, strength, contract = "runtime-command-failure", "none", contract or "unknown"
    proven = metadata.get("runtime_semantically_proven")
    if not isinstance(proven, bool):
        proven = status == "passed" and strength in {"strong", "moderate"}
    return outcome, strength or "none", contract or "unknown", bool(proven)


def _record_strict_verification_succeeded(record: Mapping[str, Any]) -> bool:
    return bool(
        all(
            _record_layer_status(record, level) == VerificationStatus.PASSED.value
            for level in ("installability", "testability", "runnability")
        )
        and _runnability_evidence(record)[3]
    )


def summarize(
    records: Sequence[Mapping[str, Any]],
    *,
    suite_id: str,
    expected_case_count: int,
    selected_indices: Sequence[int] = (),
    implementation_sha256: str,
    policy: Mapping[str, Any],
) -> dict[str, Any]:
    status_counts = Counter(str(record["outcome"]["status"]) for record in records)
    build_counts = Counter(
        str(record.get("build", {}).get("result", {}).get("status", "not_started"))
        if isinstance(record.get("build"), dict)
        else "not_started"
        for record in records
    )
    layer_counts = {
        level: dict(
            sorted(
                Counter(_record_layer_status(record, level) or "not_run" for record in records).items()
            )
        )
        for level in ("installability", "testability", "runnability")
    }
    runtime_evidence = [_runnability_evidence(record) for record in records]
    languages: dict[str, dict[str, int]] = {}
    for language in sorted({str(record["case"]["primary_language"]) for record in records}):
        selected = [record for record in records if record["case"]["primary_language"] == language]
        languages[language] = {
            "total": len(selected),
            "build_succeeded": sum(
                isinstance(record.get("build"), dict)
                and record["build"].get("result", {}).get("status") == "succeeded"
                for record in selected
            ),
            "environment_succeeded": sum(record["outcome"]["status"] == "succeeded" for record in selected),
            "strict_test_succeeded": sum(
                _record_strict_verification_succeeded(record)
                for record in selected
            ),
            "runnability_semantically_proven": sum(
                _runnability_evidence(record)[3] for record in selected
            ),
        }
    elapsed = [float(record.get("elapsed_seconds", 0.0)) for record in records]
    return {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "suite_id": suite_id,
        "execution_performed": True,
        "agent_enabled": False,
        "llm_enabled": False,
        "expected_case_count": expected_case_count,
        "selected_indices": list(selected_indices),
        "record_count": len(records),
        "complete": len(records) == expected_case_count,
        "implementation_sha256": implementation_sha256,
        "policy": policy,
        "outcomes": dict(sorted(status_counts.items())),
        "build_statuses": dict(sorted(build_counts.items())),
        "standard_build_success": build_counts["succeeded"],
        "environment_success": status_counts["succeeded"],
        "strict_test_success": sum(
            _record_strict_verification_succeeded(record) for record in records
        ),
        "verification_layers": layer_counts,
        "runnability_semantically_proven": sum(item[3] for item in runtime_evidence),
        "artifact_only_runnability_pass": sum(
            _record_layer_status(record, "runnability") == "passed"
            and evidence[1] == "limited"
            for record, evidence in zip(records, runtime_evidence)
        ),
        "runnability_outcome_categories": dict(
            sorted(Counter(item[0] for item in runtime_evidence).items())
        ),
        "runtime_evidence_strength_distribution": dict(
            sorted(Counter(item[1] for item in runtime_evidence).items())
        ),
        "runtime_contract_distribution": dict(
            sorted(Counter(item[2] for item in runtime_evidence).items())
        ),
        "languages": languages,
        "failure_categories": dict(
            sorted(
                Counter(
                    str(record["outcome"].get("category", "unclassified"))
                    for record in records
                    if record["outcome"]["status"] != "succeeded"
                ).items()
            )
        ),
        "timing_seconds": {
            "total": sum(elapsed),
            "average": sum(elapsed) / len(elapsed) if elapsed else 0.0,
            "p50": _percentile(elapsed, 0.50),
            "p95": _percentile(elapsed, 0.95),
            "maximum": max(elapsed, default=0.0),
        },
        "projects": [
            {
                "case_id": record["case"]["case_id"],
                "repo": record["case"]["repo"],
                "language": record["case"]["primary_language"],
                "build_status": (
                    record.get("build", {}).get("result", {}).get("status", "not_started")
                    if isinstance(record.get("build"), dict)
                    else "not_started"
                ),
                "outcome": record["outcome"]["status"],
                "category": record["outcome"].get("category", ""),
                "installability": _record_layer_status(record, "installability"),
                "testability": _record_layer_status(record, "testability"),
                "runnability": _record_layer_status(record, "runnability"),
                "runnability_semantically_proven": _runnability_evidence(record)[3],
                "runnability_outcome_category": _runnability_evidence(record)[0],
                "runtime_evidence_strength": _runnability_evidence(record)[1],
                "runtime_contract": _runnability_evidence(record)[2],
                "elapsed_seconds": record.get("elapsed_seconds", 0.0),
            }
            for record in records
        ],
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    defaults = BuildConfig()
    verification_defaults = VerificationConfig()
    parser = argparse.ArgumentParser(
        description="Execute pinned multilingual cases with deterministic build and layered verification; no LLM is used."
    )
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--indices", default="1-21")
    parser.add_argument("--force", action="store_true", help="Ignore matching completed records.")
    parser.add_argument("--retain-images", action="store_true")
    parser.add_argument("--case-timeout-seconds", type=int, default=DEFAULT_CASE_TIMEOUT_SECONDS)
    parser.add_argument("--build-timeout-seconds", type=int, default=defaults.timeout_seconds)
    parser.add_argument("--verification-timeout-seconds", type=int, default=verification_defaults.command_timeout_seconds)
    parser.add_argument("--docker-network", default=defaults.docker_network)
    parser.add_argument("--image-repository", default="dprauto/multilang-m10")
    parser.add_argument("--python-base-image", default=defaults.python_base_image)
    parser.add_argument("--maven-base-image", default=defaults.maven_base_image)
    parser.add_argument("--gradle-base-image", default=defaults.gradle_base_image)
    parser.add_argument("--native-base-image", default=defaults.native_base_image)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.case_timeout_seconds <= 0:
        raise SystemExit("--case-timeout-seconds must be positive")
    manifest_path = args.manifest.expanduser().resolve()
    manifest, ground_truth, ground_truth_path = load_and_validate(
        manifest_path,
        strict_sources=True,
    )
    selected = parse_indices(args.indices, maximum=len(manifest["cases"]))
    output = args.output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    build_config = BuildConfig(
        timeout_seconds=args.build_timeout_seconds,
        docker_network=args.docker_network,
        image_repository=args.image_repository,
        python_base_image=args.python_base_image,
        maven_base_image=args.maven_base_image,
        gradle_base_image=args.gradle_base_image,
        native_base_image=args.native_base_image,
    )
    verification_config = VerificationConfig(
        command_timeout_seconds=args.verification_timeout_seconds,
        docker_network=args.docker_network,
    )
    policy = evaluation_policy(
        build_config,
        verification_config,
        case_timeout_seconds=args.case_timeout_seconds,
    )
    implementation_sha256 = implementation_digest()
    manifest_sha256 = _sha256_bytes(manifest_path.read_bytes())
    ground_truth_sha256 = _sha256_bytes(ground_truth_path.read_bytes())
    truth_by_id = {case["case_id"]: case for case in ground_truth["cases"]}
    storage = LocalArtifactStorage(output / "artifacts")
    parser = MultiLanguageProjectParser()
    builder = create_deterministic_builder(storage, build_config)
    verifier = create_layered_verifier(storage, build_config, verification_config)
    identities = {
        index: evaluation_identity(
            case,
            manifest_sha256=manifest_sha256,
            ground_truth_sha256=ground_truth_sha256,
            implementation_sha256=implementation_sha256,
            policy=policy,
        )
        for index, case in enumerate(manifest["cases"], start=1)
    }
    records: dict[int, dict[str, Any]] = {}
    for index in selected:
        case = manifest["cases"][index - 1]
        record_path = output / "records" / f"{index:02d}-{case['case_id']}.json"
        cached = reusable_record(record_path, identities[index])
        if cached is not None:
            records[index] = cached

    for index in selected:
        case = manifest["cases"][index - 1]
        identity = identities[index]
        record_path = output / "records" / f"{index:02d}-{case['case_id']}.json"
        cached = None if args.force else records.get(index)
        if cached is not None:
            records[index] = cached
            print(f"[{index:02d}] SKIP {case['repo']}: identity matched", flush=True)
        else:
            print(f"[{index:02d}] START {case['repo']}", flush=True)
            record = run_case(
                case,
                truth_by_id[case["case_id"]],
                identity=identity,
                parser=parser,
                builder=builder,
                verifier=verifier,
                build_config=build_config,
                case_timeout_seconds=args.case_timeout_seconds,
                retain_image=args.retain_images,
            )
            records[index] = record
            _atomic_write(record_path, record)
            print(
                f"[{index:02d}] END {case['repo']}: {record['outcome']['status']} "
                f"in {record['elapsed_seconds']:.1f}s",
                flush=True,
            )
        ordered = [records[item] for item in sorted(records)]
        summary = summarize(
            ordered,
            suite_id=manifest["suite_id"],
            expected_case_count=len(selected),
            selected_indices=selected,
            implementation_sha256=implementation_sha256,
            policy=policy,
        )
        _atomic_write(output / "summary.json", summary)

    final = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    print(
        f"SUMMARY records={final['record_count']} build={final['standard_build_success']} "
        f"environment={final['environment_success']} strict_test={final['strict_test_success']}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

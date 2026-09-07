#!/usr/bin/env python3
"""Run a bounded multilingual Agent/LLM repair canary on isolated workspaces."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dprauto.adapters.storage import LocalArtifactStorage
from dprauto.application import create_api_environment_build_workflow
from dprauto.config import (
    AgentConfig,
    AppConfig,
    BuildConfig,
    LLMConfig,
    StorageConfig,
    VerificationConfig,
)
from dprauto.domain.models import SourceReference
from dprauto.serialization import to_json_bytes
from evaluations.prompt12.run_evaluation import (
    ensure_docker_networks,
    evaluation_identity,
    evaluation_policy,
    reusable_record,
    run_one,
    selected_indices,
    slug,
    summarize,
)

try:
    from .run_evaluation import load_and_validate
except ImportError:  # pragma: no cover - direct CLI execution
    from run_evaluation import load_and_validate


DEFAULT_MANIFEST = Path(__file__).with_name("manifest-high-star-30-20260903.json")
DEFAULT_OUTPUT = Path(__file__).with_name("runs") / "m17-llm-canary-c9c629c-20260906"
DEFAULT_NATIVE_BASE_IMAGE = (
    "docker.io/library/debian:bookworm-slim@sha256:"
    "88200866dfff7ea7f5cbcb6ec7c8a701889efe6fe859fe64d6990e4b07ea4171"
)


def implementation_digest() -> str:
    paths = [
        Path(__file__).resolve(),
        ROOT / "evaluations" / "prompt12" / "run_evaluation.py",
        *sorted((ROOT / "src" / "dprauto").rglob("*.py")),
    ]
    digest = hashlib.sha256()
    for path in paths:
        relative = path.relative_to(ROOT).as_posix().encode("utf-8")
        digest.update(relative)
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def make_config(args: argparse.Namespace, output: Path) -> AppConfig:
    return AppConfig(
        build=BuildConfig(
            timeout_seconds=args.build_timeout_seconds,
            image_repository=f"dprauto/multilang-{output.name}",
            docker_network=args.docker_network,
            native_base_image=args.native_base_image,
            maven_base_image="maven:3.9-eclipse-temurin-{version}",
            gradle_base_image="maven:3.9-eclipse-temurin-{version}",
            use_cache=True,
        ),
        agent=AgentConfig(
            max_attempts=args.max_repair_attempts,
            max_repeated_failures=2,
            max_total_seconds=args.agent_timeout_seconds,
        ),
        verification=VerificationConfig(
            command_timeout_seconds=300,
            jvm_command_timeout_seconds=900,
            dependency_command_timeout_seconds=180,
            native_test_preparation_timeout_seconds=900,
            docker_network=args.verification_network,
        ),
        llm=LLMConfig(
            api_config_path=ROOT / "myapi.json",
            request_log_root=output / "llm-logs",
            timeout_seconds=120,
            timeout_retries_per_model=0,
            max_timeout_attempts_per_operation=2,
        ),
        storage=StorageConfig(root=output / "artifacts"),
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bounded Agent/LLM canary; source repositories are copied before repair."
    )
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--indices", type=selected_indices, default={23, 29, 30})
    parser.add_argument("--build-timeout-seconds", type=int, default=1200)
    parser.add_argument("--agent-timeout-seconds", type=int, default=2400)
    parser.add_argument("--max-repair-attempts", type=int, default=2)
    parser.add_argument("--docker-network", default="host")
    parser.add_argument("--verification-network", default="dprauto-agent-canary")
    parser.add_argument("--native-base-image", default=DEFAULT_NATIVE_BASE_IMAGE)
    return parser.parse_args(argv)


def _adapt_case(case: dict) -> dict:
    return {
        "repo": case["repo"],
        "source": case["source"]["path"],
        "historical_cnb_status": "multilang-m16-build-failed",
    }


def terminal_llm_service_error(record: dict) -> str:
    """Return a batch-fatal authentication/billing error without exposing secrets."""

    metrics = record.get("state_metrics")
    reason = str(metrics.get("stop_reason", "")) if isinstance(metrics, dict) else ""
    match = re.search(r"LLM API returned HTTP (401|402|403)\b", reason, re.IGNORECASE)
    return f"LLM service returned terminal HTTP {match.group(1)}" if match else ""


def canary_summary(
    records: list[dict],
    *,
    args: argparse.Namespace,
    config: AppConfig,
    implementation_sha256: str,
    batch_stop_reason: str = "",
) -> dict:
    summary = summarize(records)
    summary["evaluation_policy"] = {
        "sample_size": len(args.indices),
        "build_timeout_seconds": config.build.timeout_seconds,
        "verification_timeout_seconds": config.verification.command_timeout_seconds,
        "native_test_preparation_timeout_seconds": (
            config.verification.native_test_preparation_timeout_seconds
        ),
        "agent_max_attempts": config.agent.max_attempts,
        "agent_max_total_seconds": config.agent.max_total_seconds,
        "llm_timeout_seconds": config.llm.timeout_seconds,
        "llm_max_attempts_per_operation": (
            config.llm.max_timeout_attempts_per_operation
        ),
        "source_workspaces_are_copies": True,
    }
    summary["complete"] = len(records) == len(args.indices)
    summary["selected_indices"] = sorted(args.indices)
    summary["expected_case_count"] = len(args.indices)
    summary["record_count"] = len(records)
    summary["batch_stop_reason"] = batch_stop_reason
    summary["evaluation_identity"] = {
        "implementation_sha256": implementation_sha256,
        "policy": evaluation_policy(config),
    }
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.max_repair_attempts < 1 or args.max_repair_attempts > 3:
        raise SystemExit("--max-repair-attempts must be within 1-3")
    manifest_path = args.manifest.expanduser().resolve()
    manifest, _, _ = load_and_validate(manifest_path, strict_sources=True)
    if not args.indices or max(args.indices) > len(manifest["cases"]):
        raise SystemExit(f"--indices must be within 1-{len(manifest['cases'])}")
    output = args.output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    if not (ROOT / "myapi.json").is_file():
        raise SystemExit(f"LLM configuration is missing: {ROOT / 'myapi.json'}")

    config = make_config(args, output)
    ensure_docker_networks(config)
    storage = LocalArtifactStorage(output / "artifacts")
    workflow = create_api_environment_build_workflow(storage, config)
    digest = implementation_digest()
    records = []
    batch_stop_reason = ""
    try:
        for one_based_index in sorted(args.indices):
            original = manifest["cases"][one_based_index - 1]
            case = _adapt_case(original)
            source_path = Path(original["source"]["path"]).resolve()
            source = SourceReference(
                original["repo"],
                str(original["source"]["revision"]),
            )
            identity = evaluation_identity(
                case,
                one_based_index - 1,
                source_path,
                source,
                config,
                implementation_sha256=digest,
            )
            record = run_one(
                workflow,
                output / "artifacts",
                output / "llm-logs",
                output,
                case,
                one_based_index - 1,
                config=config,
                implementation_sha256=digest,
                expected_identity=identity,
                source_reference=source,
                run_id_prefix="multilang-m17",
            )
            records.append(record)
            batch_stop_reason = terminal_llm_service_error(record)
            summary = canary_summary(
                records,
                args=args,
                config=config,
                implementation_sha256=digest,
                batch_stop_reason=batch_stop_reason,
            )
            (output / "summary.json").write_bytes(to_json_bytes(summary))
            if batch_stop_reason:
                print(f"BATCH STOP: {batch_stop_reason}", flush=True)
                break
    finally:
        workflow.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

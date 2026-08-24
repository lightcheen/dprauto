#!/usr/bin/env python3
"""Reproducible Prompt 12 evaluation harness; this does not add Agent behavior."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import statistics
import subprocess
import time
from collections import Counter
from functools import lru_cache
from datetime import datetime
from pathlib import Path
from typing import Any

from dprauto.adapters.storage import LocalArtifactStorage
from dprauto.adapters.python import PythonProjectParser
from dprauto.application.agent import create_api_environment_build_workflow
from dprauto.config import (
    AgentConfig,
    AppConfig,
    BuildConfig,
    LLMConfig,
    StorageConfig,
    VerificationConfig,
)
from dprauto.domain.enums import VerificationStatus
from dprauto.domain.models import SourceReference
from dprauto.proxy import docker_proxy_build_arguments
from dprauto.serialization import to_json_bytes
from dprauto.strategies.template import TemplateStrategy, poetry_tool_image_reference


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = Path(__file__).with_name("manifest.json")
DEFAULT_OUTPUT = Path(__file__).with_name("runs") / "first-round-20260812"
EVALUATION_IDENTITY_SCHEMA = 1
WORKSPACE_IGNORED_NAMES = (
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".tox",
    "dist",
    "build",
)
DOCKER_BUILTIN_NETWORKS = frozenset({"bridge", "host", "none"})


def ensure_docker_networks(config: AppConfig) -> None:
    """Create configured user-defined bridge networks without daemon changes."""

    names = {
        config.build.docker_network,
        config.verification.docker_network,
    } - {""} - DOCKER_BUILTIN_NETWORKS
    for name in sorted(names):
        inspect = subprocess.run(
            [
                config.build.docker_binary,
                "network",
                "inspect",
                "--format",
                "{{.Driver}}",
                name,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        if inspect.returncode == 0:
            if inspect.stdout.strip() != "bridge":
                raise RuntimeError(
                    f"Docker network {name!r} exists but is not a bridge network"
                )
            continue
        created = subprocess.run(
            [
                config.build.docker_binary,
                "network",
                "create",
                "--driver",
                "bridge",
                "--label",
                "dprauto.evaluation=true",
                name,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        if created.returncode != 0:
            # Another process may have created the same reusable network.
            raced = subprocess.run(
                [
                    config.build.docker_binary,
                    "network",
                    "inspect",
                    "--format",
                    "{{.Driver}}",
                    name,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            )
            if raced.returncode != 0 or raced.stdout.strip() != "bridge":
                detail = created.stdout.strip() or raced.stdout.strip()
                raise RuntimeError(
                    f"Could not create Docker bridge network {name!r}: {detail}"
                )


def required_poetry_python_versions(
    indexed_cases: list[tuple[int, dict[str, Any]]],
    config: AppConfig,
) -> tuple[str, ...]:
    """Resolve tool-image variants only for Poetry cases selected in this run."""

    parser = PythonProjectParser()
    versions: set[str] = set()
    for _, case in indexed_cases:
        source_path = Path(case["source"]).resolve()
        profile = parser.parse(load_source(source_path, case["repo"]), source_path)
        if "poetry" not in profile.package_managers:
            continue
        versions.add(
            TemplateStrategy.resolve_python_version(
                profile.runtime_constraints.get("python", ""),
                config.build.default_python_version,
            )
        )
    return tuple(sorted(versions))


def ensure_poetry_tool_images(
    config: AppConfig,
    python_versions: tuple[str, ...],
    output: Path,
) -> list[dict[str, str]]:
    """Build versioned Poetry base layers once, outside per-project metrics."""

    if not config.build.poetry_tool_image:
        return []
    preflight = output / "preflight"
    preflight.mkdir(parents=True, exist_ok=True)
    resolved: list[dict[str, str]] = []
    for version in python_versions:
        image = poetry_tool_image_reference(config.build, version)
        base_image = config.build.python_base_image.format(version=version)
        recipe_body = "\n".join(
            (
                f"FROM {base_image}",
                "ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1",
                "RUN --mount=type=cache,target=/root/.cache/pip \\",
                "    python -m pip install \"poetry=="
                f"{config.build.poetry_version}\"",
                "WORKDIR /workspace",
            )
        ) + "\n"
        fingerprint = hashlib.sha256(recipe_body.encode("utf-8")).hexdigest()
        dockerfile = recipe_body + (
            "LABEL dprauto.tool.kind=poetry "
            f"dprauto.tool.recipe-sha256={fingerprint}\n"
        )
        inspect_command = [
            config.build.docker_binary,
            "image",
            "inspect",
            "--format",
            '{{index .Config.Labels "dprauto.tool.recipe-sha256"}}',
            image,
        ]
        inspected = subprocess.run(
            inspect_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        if inspected.returncode == 0 and inspected.stdout.strip() == fingerprint:
            print(f"[preflight] REUSE Poetry tool image {image}", flush=True)
        else:
            command = [
                config.build.docker_binary,
                "build",
                "--file",
                "-",
                "--tag",
                image,
            ]
            if not config.build.use_cache:
                command.append("--no-cache")
            network = (
                "none"
                if not config.build.allow_network
                else config.build.docker_network
            )
            if network:
                command.extend(("--network", network))
            command.extend(docker_proxy_build_arguments(config.build))
            command.append(".")
            print(f"[preflight] BUILD Poetry tool image {image}", flush=True)
            try:
                built = subprocess.run(
                    command,
                    input=dockerfile,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=config.build.poetry_tool_timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                partial = exc.stdout or ""
                if isinstance(partial, bytes):
                    partial = partial.decode("utf-8", "replace")
                captured = partial + "\nPoetry tool image build timed out\n"
                (preflight / f"poetry-python-{version}.log").write_text(
                    captured, encoding="utf-8"
                )
                raise RuntimeError(
                    f"Poetry tool image {image!r} exceeded "
                    f"{config.build.poetry_tool_timeout_seconds}s"
                ) from exc
            (preflight / f"poetry-python-{version}.log").write_text(
                built.stdout, encoding="utf-8"
            )
            if built.returncode != 0:
                raise RuntimeError(
                    f"Could not build Poetry tool image {image!r}; "
                    f"see {preflight / f'poetry-python-{version}.log'}"
                )
            inspected = subprocess.run(
                inspect_command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            )
            if inspected.returncode != 0 or inspected.stdout.strip() != fingerprint:
                raise RuntimeError(
                    f"Poetry tool image {image!r} did not preserve its recipe fingerprint"
                )
        resolved.append(
            {
                "python_version": version,
                "poetry_version": config.build.poetry_version,
                "image": image,
                "recipe_sha256": fingerprint,
            }
        )
    (preflight / "poetry-tool-images.json").write_bytes(to_json_bytes(resolved))
    return resolved


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", value.lower()).strip("-._")


def selected_indices(value: str) -> set[int]:
    selected: set[int] = set()
    for raw_part in value.split(","):
        part = raw_part.strip()
        if not part:
            continue
        if "-" in part:
            start_text, end_text = part.split("-", 1)
            start = int(start_text)
            end = int(end_text)
            if start <= 0 or end < start:
                raise argparse.ArgumentTypeError(f"invalid index range: {part}")
            selected.update(range(start, end + 1))
        else:
            index = int(part)
            if index <= 0:
                raise argparse.ArgumentTypeError(f"indices are 1-based: {part}")
            selected.add(index)
    if not selected:
        raise argparse.ArgumentTypeError("at least one index is required")
    return selected


def json_value(value: Any) -> Any:
    return json.loads(to_json_bytes(value))


def load_source(path: Path, repo: str) -> SourceReference:
    marker = path / ".cnb-benchmark-source-ready"
    if marker.is_file():
        payload = json.loads(marker.read_text(encoding="utf-8"))
        return SourceReference(payload.get("repo", repo), payload.get("ref"))
    return SourceReference(repo)


def copy_workspace(source: Path, target: Path) -> None:
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        source,
        target,
        symlinks=False,
        ignore=shutil.ignore_patterns(*WORKSPACE_IGNORED_NAMES),
    )


@lru_cache(maxsize=1)
def implementation_digest() -> str:
    """Hash the harness and DPRAuto implementation used to produce records."""

    paths = [Path(__file__).resolve(), *sorted((ROOT / "src" / "dprauto").rglob("*.py"))]
    digest = hashlib.sha256()
    for path in paths:
        relative = path.relative_to(ROOT).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def source_tree_digest(source: Path) -> str:
    """Hash the source inputs copied into an evaluation workspace."""

    digest = hashlib.sha256()
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if any(part in WORKSPACE_IGNORED_NAMES for part in relative.parts):
            continue
        if path.is_symlink():
            digest.update(relative.as_posix().encode("utf-8"))
            digest.update(b"\0link\0")
            digest.update(
                hashlib.sha256(
                    path.readlink().as_posix().encode("utf-8")
                ).digest()
            )
        elif path.is_file():
            digest.update(relative.as_posix().encode("utf-8"))
            digest.update(b"\0file\0")
            file_digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    file_digest.update(chunk)
            digest.update(file_digest.digest())
    return digest.hexdigest()


def evaluation_policy(config: AppConfig) -> dict[str, Any]:
    """Return behavior-affecting, secret-free evaluation settings."""

    model_pool: list[dict[str, str]] = []
    try:
        payload = json.loads(config.llm.api_config_path.expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    if isinstance(payload, dict) and all(
        key in payload for key in ("model", "base_url")
    ):
        model_pool.append(
            {
                "name": str(payload.get("model", "")),
                "model": str(payload.get("model", "")),
                "base_url_sha256": hashlib.sha256(
                    str(payload.get("base_url", "")).encode("utf-8")
                ).hexdigest(),
            }
        )
    elif isinstance(payload, dict):
        for name, candidate in payload.items():
            if isinstance(candidate, dict):
                model_pool.append(
                    {
                        "name": str(name),
                        "model": str(candidate.get("model", "")),
                        "base_url_sha256": hashlib.sha256(
                            str(candidate.get("base_url", "")).encode("utf-8")
                        ).hexdigest(),
                    }
                )
    return {
        "build": {
            "default_strategy": config.build.default_strategy,
            "timeout_seconds": config.build.timeout_seconds,
            "strategy_portfolio_enabled": config.build.strategy_portfolio_enabled,
            "max_strategy_attempts": config.build.max_strategy_attempts,
            "allow_network": config.build.allow_network,
            "forward_proxy_environment": config.build.forward_proxy_environment,
            "use_cache": config.build.use_cache,
            "docker_binary": config.build.docker_binary,
            "image_repository": config.build.image_repository,
            "default_python_version": config.build.default_python_version,
            "python_base_image": config.build.python_base_image,
            "pack_binary": config.build.pack_binary,
            "cnb_builder": config.build.cnb_builder,
            "cnb_lifecycle_image": config.build.cnb_lifecycle_image,
            "docker_network": config.build.docker_network,
            "poetry_version": config.build.poetry_version,
            "poetry_tool_image": config.build.poetry_tool_image,
            "poetry_tool_timeout_seconds": config.build.poetry_tool_timeout_seconds,
        },
        "agent": {
            "max_attempts": config.agent.max_attempts,
            "max_repeated_failures": config.agent.max_repeated_failures,
            "max_total_seconds": config.agent.max_total_seconds,
            "max_context_characters": config.agent.max_context_characters,
            "max_recent_modifications": config.agent.max_recent_modifications,
            "max_failed_methods": config.agent.max_failed_methods,
            "max_resolved_issues": config.agent.max_resolved_issues,
            "max_investigation_rounds": config.agent.max_investigation_rounds,
            "max_investigation_actions": config.agent.max_investigation_actions,
            "max_evidence_characters": config.agent.max_evidence_characters,
        },
        "verification": {
            "command_timeout_seconds": config.verification.command_timeout_seconds,
            "web_startup_timeout_seconds": config.verification.web_startup_timeout_seconds,
            "web_path": config.verification.web_path,
            "output_excerpt_characters": config.verification.output_excerpt_characters,
            "docker_network": config.verification.docker_network,
            "pytest_version": config.verification.pytest_version,
            "pytest_xdist_version": config.verification.pytest_xdist_version,
            "tox_version": config.verification.tox_version,
            "nox_version": config.verification.nox_version,
            "max_parallel_test_workers": config.verification.max_parallel_test_workers,
            "max_test_files_per_slice": config.verification.max_test_files_per_slice,
        },
        "llm": {
            "provider": config.llm.provider,
            "model": config.llm.model,
            "temperature": config.llm.temperature,
            "timeout_seconds": config.llm.timeout_seconds,
            "timeout_retries_per_model": config.llm.timeout_retries_per_model,
            "max_output_tokens": config.llm.max_output_tokens,
            "model_pool": model_pool,
        },
        "security": {
            "allow_source_changes": config.security.allow_source_changes,
            "allow_privileged_execution": config.security.allow_privileged_execution,
            "allowed_mutation_globs": list(config.security.allowed_mutation_globs),
        },
        "workspace_ignored_names": list(WORKSPACE_IGNORED_NAMES),
    }


def evaluation_identity(
    case: dict[str, Any],
    index: int,
    source_path: Path,
    source: SourceReference,
    config: AppConfig,
    *,
    implementation_sha256: str | None = None,
) -> dict[str, Any]:
    implementation_sha256 = implementation_sha256 or implementation_digest()
    payload = {
        "case": {
            "index": index + 1,
            "repo": case["repo"],
            "historical_cnb_status": case["historical_cnb_status"],
        },
        "source": {
            "path": str(source_path),
            "reference": json_value(source),
            "tree_sha256": source_tree_digest(source_path),
        },
        "policy": evaluation_policy(config),
        "implementation_sha256": implementation_sha256,
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return {
        "schema_version": EVALUATION_IDENTITY_SCHEMA,
        "digest": hashlib.sha256(encoded).hexdigest(),
        "payload": payload,
    }


def reusable_record(path: Path, expected_identity: dict[str, Any]) -> dict[str, Any] | None:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    identity = record.get("evaluation_identity")
    if not isinstance(identity, dict):
        return None
    payload = identity.get("payload")
    if not isinstance(payload, dict):
        return None
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if identity.get("digest") != hashlib.sha256(encoded).hexdigest():
        return None
    return record if identity == expected_identity else None


def attempt_results(artifact_root: Path) -> dict[str, dict[str, Any]]:
    values = {}
    for path in (artifact_root / "attempts").glob("*/result.json"):
        try:
            values[path.parent.name] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
    return values


def portfolio_results(artifact_root: Path) -> dict[str, dict[str, Any]]:
    values = {}
    for path in (artifact_root / "build-portfolios").glob("*/selection.json"):
        try:
            values[path.parent.name] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
    return values


def llm_metrics(log_root: Path, run_id: str) -> dict[str, Any]:
    calls = []
    for path in sorted(log_root.glob("*.log")):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("metadata", {}).get("run_id") == run_id:
                calls.append(entry)
    input_tokens = sum(
        (entry.get("response") or {}).get("input_tokens") or 0 for entry in calls
    )
    output_tokens = sum(
        (entry.get("response") or {}).get("output_tokens") or 0 for entry in calls
    )
    return {
        "calls": len(calls),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "api_seconds": round(sum(entry.get("duration_seconds", 0) for entry in calls), 6),
        "errors": sum(bool(entry.get("error")) for entry in calls),
    }


def make_config(output: Path) -> AppConfig:
    return AppConfig(
        build=BuildConfig(
            timeout_seconds=300,
            image_repository="dprauto-eval",
            use_cache=True,
            # The daemon's default bridge has no outbound connectivity and
            # BuildKit only accepts default/none/host as per-build modes.
            # Runtime verification remains isolated on the dedicated bridge.
            docker_network="host",
            poetry_version="1.8.5",
            poetry_tool_image=(
                "dprauto-tools/python-poetry:python-{version}-poetry-{poetry_version}"
            ),
            poetry_tool_timeout_seconds=900,
        ),
        agent=AgentConfig(
            max_attempts=2,
            max_repeated_failures=2,
            max_total_seconds=900,
        ),
        verification=VerificationConfig(
            command_timeout_seconds=90,
            web_startup_timeout_seconds=20,
            docker_network="dprauto-eval",
        ),
        llm=LLMConfig(
            api_config_path=ROOT / "myapi.json",
            request_log_root=output / "llm-logs",
            timeout_seconds=120,
        ),
        storage=StorageConfig(root=output / "artifacts"),
    )


def ineffective_modifications(state: dict[str, Any]) -> int:
    history = state.get("failure_history", ())
    same_failure = sum(
        before.fingerprint == after.fingerprint
        for before, after in zip(history, history[1:])
    )
    no_change = "made no environment change" in state.get("stop_reason", "")
    return same_failure + int(no_change)


def run_one(
    workflow,
    storage_root: Path,
    llm_root: Path,
    output: Path,
    case,
    index,
    *,
    config: AppConfig | None = None,
    implementation_sha256: str | None = None,
    expected_identity: dict[str, Any] | None = None,
):
    repo = case["repo"]
    project_slug = f"{index + 1:02d}-{slug(repo)}"
    record_path = output / "records" / f"{project_slug}.json"
    source_path = Path(case["source"]).resolve()
    source = load_source(source_path, repo)
    effective_config = config or make_config(output)
    identity = expected_identity or evaluation_identity(
        case,
        index,
        source_path,
        source,
        effective_config,
        implementation_sha256=implementation_sha256,
    )
    if record_path.is_file():
        cached = reusable_record(record_path, identity)
        if cached is not None:
            print(f"[{index + 1:02d}] SKIP {repo}: identity matched", flush=True)
            return cached
        print(f"[{index + 1:02d}] STALE {repo}: identity mismatch", flush=True)

    identity_short = identity["digest"][:12]
    workspace = output / "workspaces" / f"{project_slug}-{identity_short}"
    copy_workspace(source_path, workspace)
    run_id = f"prompt12-{index + 1:02d}-{slug(repo)}-{identity_short}"
    before = attempt_results(storage_root)
    before_portfolios = portfolio_results(storage_root)
    started = time.monotonic()
    print(
        f"[{index + 1:02d}] START {repo} "
        f"(historical={case['historical_cnb_status']})",
        flush=True,
    )
    try:
        result = workflow.run(run_id, source, workspace)
        state = workflow.persisted_state(run_id)
        error = ""
    except Exception as exc:  # evaluation must retain crashes as data
        result = None
        try:
            state = workflow.persisted_state(run_id)
        except Exception:
            state = {}
        error = f"{type(exc).__name__}: {exc}"
    elapsed = time.monotonic() - started
    after = attempt_results(storage_root)
    new_attempts = [value for key, value in after.items() if key not in before]
    new_attempts.sort(key=lambda item: item["started_at"])
    after_portfolios = portfolio_results(storage_root)
    new_portfolios = [
        value
        for key, value in after_portfolios.items()
        if key not in before_portfolios
    ]
    new_portfolios.sort(
        key=lambda item: (item.get("result") or {}).get("started_at", "")
    )
    if new_portfolios:
        standard = (
            new_portfolios[0].get("portfolio_result")
            or new_portfolios[0].get("result")
        )
    else:
        standard = new_attempts[0] if new_attempts else None
    llm = llm_metrics(llm_root, run_id)
    first_failure = state.get("failure_history", ())
    first_failure = first_failure[0] if first_failure else state.get("failure")
    record = {
        "evaluation_identity": identity,
        "index": index + 1,
        "repo": repo,
        "historical_cnb_status": case["historical_cnb_status"],
        "source_path": str(source_path),
        "workspace_path": str(workspace),
        "source_reference": json_value(source),
        "run_id": run_id,
        "elapsed_seconds": round(elapsed, 6),
        "error": error,
        "standard_build": standard,
        "build_attempts": new_attempts,
        "build_portfolios": new_portfolios,
        "initial_failure": json_value(first_failure) if first_failure else None,
        "final_result": json_value(result) if result else None,
        "state_metrics": {
            "phase": getattr(state.get("phase"), "value", None),
            "repeated_failure_count": state.get("repeated_failure_count", 0),
            "stop_reason": state.get("stop_reason", ""),
            "ineffective_modifications": ineffective_modifications(state),
            "duplicate_repair_plan": int(
                "duplicate repair method" in state.get("stop_reason", "")
            ),
        },
        "llm": llm,
    }
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_bytes(to_json_bytes(record))
    final_status = (
        record["final_result"]["final_status"] if record["final_result"] else "error"
    )
    print(
        f"[{index + 1:02d}] END   {repo}: status={final_status} "
        f"attempts={len(new_attempts)} llm={llm['calls']} elapsed={elapsed:.1f}s",
        flush=True,
    )
    return record


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * fraction) - 1)
    return ordered[index]


def build_duration(build_result: dict[str, Any]) -> float:
    started = datetime.fromisoformat(build_result["started_at"])
    finished = datetime.fromisoformat(build_result["finished_at"])
    return max(0.0, (finished - started).total_seconds())


def passed(record, level):
    result = (record.get("final_result") or {}).get(level)
    return bool(result and result.get("status") == VerificationStatus.PASSED.value)


def terminal_failure(record: dict[str, Any]) -> dict[str, Any]:
    final_failure = (record.get("final_result") or {}).get("failure")
    if isinstance(final_failure, dict) and final_failure:
        return final_failure
    initial_failure = record.get("initial_failure")
    return initial_failure if isinstance(initial_failure, dict) else {}


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    completed = [r for r in records if r.get("final_result")]
    total = len(records)
    standard_success = sum(
        (r.get("standard_build") or {}).get("status") == "succeeded" for r in records
    )
    entered = [r for r in completed if r["final_result"]["agent_participated"]]
    repaired = [r for r in entered if r["final_result"]["final_status"] == "succeeded"]
    final_success = sum(
        r["final_result"]["final_status"] == "succeeded" for r in completed
    )
    build_times = [
        build_duration(r["standard_build"])
        for r in records
        if r.get("standard_build")
    ]
    agent_times = [
        max(0.0, r["elapsed_seconds"] - build_duration(r["standard_build"]))
        for r in entered
        if r.get("standard_build")
    ]
    repair_rounds = [r["final_result"]["repair_attempts"] for r in entered]
    initial_categories = Counter(
        r["initial_failure"]["category"]
        for r in records
        if r.get("initial_failure")
    )
    repaired_categories = Counter(
        r["initial_failure"]["category"]
        for r in repaired
        if r.get("initial_failure")
    )
    unresolved_categories = Counter(
        terminal_failure(r).get("category", "unclassified")
        for r in records
        if not r.get("final_result")
        or r["final_result"]["final_status"] != "succeeded"
    )
    infrastructure = Counter()
    for r in records:
        failure = terminal_failure(r)
        kind = failure.get("kind")
        log = (failure.get("key_log") or "").lower()
        if kind == "git":
            infrastructure["git_download_failure"] += 1
        if kind == "docker_infrastructure":
            infrastructure["docker_failure"] += 1
        if kind == "network":
            infrastructure["network_failure"] += 1
        if kind in {"network", "docker_infrastructure"} and any(
            word in log for word in ("pull", "manifest", "resolve image", "registry")
        ):
            infrastructure["image_pull_failure"] += 1
        if any(word in log for word in ("no space left", "out of memory", "oom", "cpu")):
            infrastructure["disk_cpu_memory"] += 1
    regressions = sum(
        r["final_result"]["final_status"] == "regression"
        or (r["final_result"].get("regression_result") or {}).get("status") == "regression"
        for r in completed
    )
    return {
        "evaluation_policy": {
            "sample_size": total,
            "build_timeout_seconds": 300,
            "verification_timeout_seconds": 90,
            "agent_max_attempts": 2,
            "agent_max_total_seconds": 900,
            "source_workspaces_are_copies": True,
        },
        "build_capability": {
            "project_total": total,
            "standard_build_success": standard_success,
            "standard_build_success_rate": standard_success / total if total else 0,
            "standard_build_failure": total - standard_success,
        },
        "agent_capability": {
            "entered_agent": len(entered),
            "agent_repair_success": len(repaired),
            "agent_repair_success_rate": len(repaired) / len(entered) if entered else 0,
            "final_environment_success": final_success,
            "final_environment_success_rate": final_success / total if total else 0,
            "average_repair_rounds": statistics.mean(repair_rounds) if repair_rounds else 0,
            "max_repair_rounds": max(repair_rounds, default=0),
            "repeated_error_stops": sum(
                "same failure repeated" in r["state_metrics"]["stop_reason"] for r in records
            ),
        },
        "verification": {
            "installability_pass": sum(passed(r, "installability") for r in completed),
            "testability_pass": sum(passed(r, "testability") for r in completed),
            "runnability_pass": sum(passed(r, "runnability") for r in completed),
            "build_success_test_failure": sum(
                (r.get("standard_build") or {}).get("status") == "succeeded"
                and not passed(r, "testability")
                for r in completed
            ),
            "test_success_run_failure": sum(
                passed(r, "testability") and not passed(r, "runnability")
                for r in completed
            ),
        },
        "repair_quality": {
            "regression_count": regressions,
            "regression_rate": regressions / len(entered) if entered else 0,
            "ineffective_modifications": sum(
                r["state_metrics"]["ineffective_modifications"] for r in records
            ),
            "duplicate_repair_plans": sum(
                r["state_metrics"]["duplicate_repair_plan"] for r in records
            ),
        },
        "cost": {
            "average_standard_build_seconds": statistics.mean(build_times) if build_times else 0,
            "p50_standard_build_seconds": percentile(build_times, 0.50),
            "p95_standard_build_seconds": percentile(build_times, 0.95),
            "average_agent_seconds": statistics.mean(agent_times) if agent_times else 0,
            "llm_calls": sum(r["llm"]["calls"] for r in records),
            "llm_api_seconds": sum(r["llm"]["api_seconds"] for r in records),
            "input_tokens": sum(r["llm"]["input_tokens"] for r in records),
            "output_tokens": sum(r["llm"]["output_tokens"] for r in records),
            "total_tokens": sum(r["llm"]["total_tokens"] for r in records),
        },
        "infrastructure": dict(infrastructure),
        "failure_type_distribution": dict(initial_categories),
        "agent_repaired_failure_types": dict(repaired_categories),
        "unresolved_failure_types": dict(unresolved_categories),
        "runner_errors": sum(bool(r.get("error")) for r in records),
        "projects": [
            {
                "repo": r["repo"],
                "source_path": r["source_path"],
                "workspace_path": r["workspace_path"],
                "historical_cnb_status": r["historical_cnb_status"],
                "standard_build_status": (r.get("standard_build") or {}).get("status"),
                "final_status": (r.get("final_result") or {}).get("final_status", "error"),
                "agent_participated": (r.get("final_result") or {}).get(
                    "agent_participated", False
                ),
                "repair_attempts": (r.get("final_result") or {}).get("repair_attempts", 0),
                "initial_failure_category": (r.get("initial_failure") or {}).get("category"),
                "final_failure_category": terminal_failure(r).get("category"),
            }
            for r in records
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    parser.add_argument("--start", type=int, default=1)
    parser.add_argument("--end", type=int, default=10_000)
    parser.add_argument(
        "--indices",
        type=selected_indices,
        default=None,
        help="Comma-separated 1-based manifest indices or ranges, for example 5,8,10,12,17,23-24.",
    )
    args = parser.parse_args()
    output = args.output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    manifest_path = args.manifest.expanduser().resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    storage = LocalArtifactStorage(output / "artifacts")
    config = make_config(output)
    ensure_docker_networks(config)
    selected_cases = [
        (index, case)
        for index, case in enumerate(manifest)
        if (
            index + 1 in args.indices
            if args.indices is not None
            else args.start <= index + 1 <= args.end
        )
    ]
    poetry_tool_images = ensure_poetry_tool_images(
        config,
        required_poetry_python_versions(selected_cases, config),
        output,
    )
    current_implementation_digest = implementation_digest()
    identities: dict[int, dict[str, Any]] = {}

    def identity_for(index: int, case: dict[str, Any]) -> dict[str, Any]:
        if index not in identities:
            source_path = Path(case["source"]).resolve()
            identities[index] = evaluation_identity(
                case,
                index,
                source_path,
                load_source(source_path, case["repo"]),
                config,
                implementation_sha256=current_implementation_digest,
            )
        return identities[index]

    workflow = create_api_environment_build_workflow(storage, config)
    try:
        records = []
        for index, case in enumerate(manifest):
            record_path = output / "records" / f"{index + 1:02d}-{slug(case['repo'])}.json"
            selected = (
                index + 1 in args.indices
                if args.indices is not None
                else args.start <= index + 1 <= args.end
            )
            if selected:
                records.append(
                    run_one(
                        workflow,
                        output / "artifacts",
                        output / "llm-logs",
                        output,
                        case,
                        index,
                        config=config,
                        implementation_sha256=current_implementation_digest,
                        expected_identity=identity_for(index, case),
                    )
                )
            elif record_path.is_file():
                cached = reusable_record(record_path, identity_for(index, case))
                if cached is not None:
                    records.append(cached)
        all_records = []
        for index, case in enumerate(manifest):
            record_path = (
                output / "records" / f"{index + 1:02d}-{slug(case['repo'])}.json"
            )
            if not record_path.is_file():
                continue
            cached = reusable_record(record_path, identity_for(index, case))
            if cached is not None:
                all_records.append(cached)
        summary = summarize(all_records)
        summary["evaluation_identity"] = {
            "schema_version": EVALUATION_IDENTITY_SCHEMA,
            "implementation_sha256": current_implementation_digest,
            "policy": evaluation_policy(config),
            "valid_record_count": len(all_records),
        }
        summary["poetry_tool_images"] = poetry_tool_images
        (output / "summary.json").write_bytes(to_json_bytes(summary))
        print(
            f"SUMMARY records={len(all_records)} "
            f"standard_success={summary['build_capability']['standard_build_success']} "
            f"final_success={summary['agent_capability']['final_environment_success']}",
            flush=True,
        )
    finally:
        workflow.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Validate and summarize the frozen M0 multilingual evaluation corpus.

M0 deliberately does not build projects.  It establishes a reviewable corpus
and command ground truth before multilingual build strategies are introduced.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = Path(__file__).with_name("manifest.json")
ALLOWED_LANGUAGES = {"python", "java", "c", "cpp"}
ALLOWED_SOURCE_STATES = {"ready", "fetch_required"}
COMMAND_KINDS = {"build", "test", "run", "setup"}
FALSE_TEST_MARKERS = (
    "pip download",
    " lint",
    "lint ",
    " format",
    "format ",
    " fuzz",
    "fuzz ",
    "docs-only",
    "release",
)


class ManifestValidationError(ValueError):
    """Raised when the M0 manifest or reviewed ground truth is inconsistent."""


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ManifestValidationError(f"missing JSON file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ManifestValidationError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ManifestValidationError(f"expected a JSON object in {path}")
    return value


def _repo_path(value: str, manifest_path: Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    # Repository-relative references remain stable when the CLI is run from a
    # different working directory. Fixture manifests can use their own folder.
    repository_candidate = REPOSITORY_ROOT / path
    if repository_candidate.exists():
        return repository_candidate
    return manifest_path.parent / path


def _require(condition: bool, message: str, errors: list[str]) -> None:
    if not condition:
        errors.append(message)


def _safe_workspace(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    path = Path(value)
    return not path.is_absolute() and ".." not in path.parts


def _is_ordinary_test_command(command: str) -> bool:
    normalized = f" {command.lower()} "
    if any(marker in normalized for marker in ("pytest", "runtests.py", "ctest")):
        return True
    tokens = normalized.replace("./", " ").split()
    if "manage.py" in tokens and "test" in tokens:
        return True
    if any(token in {"mvn", "mvnw", "gradlew"} for token in tokens) and "test" in tokens:
        return True
    return "make" in tokens and any(token in {"test", "check"} for token in tokens)


def _local_git_head(path: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=path,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip().casefold() if result.returncode == 0 else ""


def validate_manifest(
    manifest: Mapping[str, Any],
    ground_truth: Mapping[str, Any],
    *,
    manifest_path: Path = DEFAULT_MANIFEST,
    strict_sources: bool = False,
) -> None:
    """Validate corpus identity, local sources, and command semantics."""

    errors: list[str] = []
    _require(manifest.get("schema_version") == 1, "manifest schema_version must be 1", errors)
    _require(ground_truth.get("schema_version") == 1, "ground truth schema_version must be 1", errors)
    _require(
        manifest.get("suite_id") == ground_truth.get("suite_id"),
        "manifest and ground truth suite_id differ",
        errors,
    )

    datasets = manifest.get("datasets")
    cases = manifest.get("cases")
    truth_cases = ground_truth.get("cases")
    _require(isinstance(datasets, dict) and bool(datasets), "datasets must be a non-empty object", errors)
    _require(isinstance(cases, list) and bool(cases), "cases must be a non-empty array", errors)
    _require(isinstance(truth_cases, list) and bool(truth_cases), "ground truth cases must be a non-empty array", errors)
    if not isinstance(datasets, dict) or not isinstance(cases, list) or not isinstance(truth_cases, list):
        raise ManifestValidationError("; ".join(errors))

    for dataset_id, dataset in datasets.items():
        if not isinstance(dataset, dict):
            errors.append(f"dataset {dataset_id!r} must be an object")
            continue
        catalog = dataset.get("catalog")
        source_root = dataset.get("source_root")
        _require(isinstance(catalog, str) and Path(catalog).is_absolute(), f"dataset {dataset_id}: catalog must be absolute", errors)
        _require(isinstance(source_root, str) and Path(source_root).is_absolute(), f"dataset {dataset_id}: source_root must be absolute", errors)
        if isinstance(catalog, str):
            _require(Path(catalog).is_file(), f"dataset {dataset_id}: catalog is missing: {catalog}", errors)

    manifest_ids: list[str] = []
    manifest_by_id: dict[str, Mapping[str, Any]] = {}
    for position, case in enumerate(cases, start=1):
        label = f"manifest case #{position}"
        if not isinstance(case, dict):
            errors.append(f"{label} must be an object")
            continue
        case_id = case.get("case_id")
        if not isinstance(case_id, str) or not case_id:
            errors.append(f"{label} has no case_id")
            continue
        label = case_id
        manifest_ids.append(case_id)
        manifest_by_id[case_id] = case
        _require(case.get("primary_language") in ALLOWED_LANGUAGES, f"{label}: unsupported primary_language", errors)
        dataset_id = case.get("dataset")
        _require(dataset_id in datasets, f"{label}: unknown dataset {dataset_id!r}", errors)
        _require(isinstance(case.get("focus"), list) and bool(case["focus"]), f"{label}: focus must be non-empty", errors)
        source = case.get("source")
        if not isinstance(source, dict):
            errors.append(f"{label}: source must be an object")
            continue
        state = source.get("state")
        path_value = source.get("path")
        _require(state in ALLOWED_SOURCE_STATES, f"{label}: invalid source state {state!r}", errors)
        _require(isinstance(path_value, str) and Path(path_value).is_absolute(), f"{label}: source path must be absolute", errors)
        dataset = datasets.get(dataset_id)
        source_root_value = dataset.get("source_root") if isinstance(dataset, dict) else None
        if isinstance(path_value, str) and isinstance(source_root_value, str):
            source_root = Path(source_root_value)
            try:
                Path(path_value).relative_to(source_root)
            except ValueError:
                errors.append(f"{label}: source path is outside dataset source_root")
        if state == "ready":
            _require(bool(source.get("revision")), f"{label}: ready source needs a revision", errors)
            if isinstance(path_value, str):
                source_path = Path(path_value)
                _require(source_path.is_dir(), f"{label}: ready source directory is missing: {path_value}", errors)
                # Dataset exports may intentionally omit .git metadata. Sources
                # fetched directly by this suite carry a URL and must match the
                # pinned local checkout during strict evaluation.
                if (
                    strict_sources
                    and source_path.is_dir()
                    and source.get("revision")
                    and source.get("url")
                ):
                    actual_revision = _local_git_head(source_path)
                    expected_revision = str(source["revision"]).casefold()
                    _require(
                        bool(actual_revision),
                        f"{label}: strict source has no readable git HEAD: {path_value}",
                        errors,
                    )
                    _require(
                        actual_revision.startswith(expected_revision),
                        (
                            f"{label}: local git HEAD {actual_revision or '<missing>'} "
                            f"does not match pinned revision {expected_revision}"
                        ),
                        errors,
                    )
        elif state == "fetch_required":
            _require(bool(source.get("url")), f"{label}: fetch_required source needs a URL", errors)
            _require(bool(source.get("revision_note")), f"{label}: unpinned source needs a revision_note", errors)
            if strict_sources:
                errors.append(f"{label}: source must be fetched and pinned before strict evaluation")

    duplicate_ids = sorted(case_id for case_id, count in Counter(manifest_ids).items() if count > 1)
    _require(not duplicate_ids, f"duplicate manifest case IDs: {duplicate_ids}", errors)

    truth_ids: list[str] = []
    for position, case in enumerate(truth_cases, start=1):
        if not isinstance(case, dict):
            errors.append(f"ground truth case #{position} must be an object")
            continue
        case_id = case.get("case_id")
        if not isinstance(case_id, str) or not case_id:
            errors.append(f"ground truth case #{position} has no case_id")
            continue
        truth_ids.append(case_id)
        manifest_case = manifest_by_id.get(case_id)
        _require(manifest_case is not None, f"{case_id}: no matching manifest case", errors)
        _require(_safe_workspace(case.get("workspace")), f"{case_id}: workspace must be a safe relative path", errors)
        commands = case.get("commands")
        if not isinstance(commands, dict):
            errors.append(f"{case_id}: commands must be an object")
            continue
        _require(set(commands) == COMMAND_KINDS, f"{case_id}: commands must contain exactly {sorted(COMMAND_KINDS)}", errors)
        for kind in COMMAND_KINDS:
            values = commands.get(kind)
            _require(
                isinstance(values, list) and all(isinstance(value, str) and value.strip() for value in values),
                f"{case_id}: commands.{kind} must be an array of non-empty strings",
                errors,
            )
        tests = commands.get("test")
        if isinstance(tests, list):
            _require(bool(tests), f"{case_id}: ordinary test command is required", errors)
            for command in tests:
                if not isinstance(command, str):
                    continue
                normalized = f" {command.lower()} "
                _require(
                    _is_ordinary_test_command(command),
                    f"{case_id}: test command has no recognized ordinary test runner: {command}",
                    errors,
                )
                _require(
                    not any(marker in normalized for marker in FALSE_TEST_MARKERS),
                    f"{case_id}: false/special target classified as test: {command}",
                    errors,
                )
        manifest_source = manifest_case.get("source") if manifest_case is not None else None
        if isinstance(manifest_source, dict) and manifest_source.get("state") in ALLOWED_SOURCE_STATES:
            source_state = manifest_source["state"]
            expected_review = "reviewed" if source_state == "ready" else "provisional_source_missing"
            _require(case.get("review_state") == expected_review, f"{case_id}: review_state must be {expected_review}", errors)

    duplicate_truth_ids = sorted(case_id for case_id, count in Counter(truth_ids).items() if count > 1)
    _require(not duplicate_truth_ids, f"duplicate ground truth case IDs: {duplicate_truth_ids}", errors)
    _require(set(manifest_ids) == set(truth_ids), "manifest and ground truth case ID sets differ", errors)

    if errors:
        raise ManifestValidationError("\n".join(f"- {error}" for error in errors))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_report(
    manifest: Mapping[str, Any],
    ground_truth: Mapping[str, Any],
    *,
    manifest_path: Path = DEFAULT_MANIFEST,
    ground_truth_path: Path,
) -> dict[str, Any]:
    """Return a deterministic, read-only readiness report."""

    cases = manifest["cases"]
    truth_cases = ground_truth["cases"]
    language_counts = Counter(case["primary_language"] for case in cases)
    dataset_counts = Counter(case["dataset"] for case in cases)
    source_counts = Counter(case["source"]["state"] for case in cases)
    tier_counts = Counter(case["tier"] for case in cases)
    review_counts = Counter(case["review_state"] for case in truth_cases)
    services = Counter(
        service
        for case in truth_cases
        for service in case.get("test_contract", {}).get("services", [])
    )
    executables = Counter(
        executable
        for case in truth_cases
        for executable in case.get("test_contract", {}).get("system_executables", [])
    )
    return {
        "schema_version": 1,
        "suite_id": manifest["suite_id"],
        "validated": True,
        "execution_performed": False,
        "manifest_sha256": _sha256(manifest_path),
        "ground_truth_sha256": _sha256(ground_truth_path),
        "legacy_python_baseline": manifest["legacy_python_baseline"],
        "case_count": len(cases),
        "languages": dict(sorted(language_counts.items())),
        "datasets": dict(sorted(dataset_counts.items())),
        "source_states": dict(sorted(source_counts.items())),
        "tiers": dict(sorted(tier_counts.items())),
        "ground_truth_review": dict(sorted(review_counts.items())),
        "required_services": dict(sorted(services.items())),
        "required_system_executables": dict(sorted(executables.items())),
        "ready_case_ids": [case["case_id"] for case in cases if case["source"]["state"] == "ready"],
        "fetch_required_case_ids": [case["case_id"] for case in cases if case["source"]["state"] == "fetch_required"],
    }


def load_and_validate(
    manifest_path: Path = DEFAULT_MANIFEST,
    *,
    strict_sources: bool = False,
) -> tuple[dict[str, Any], dict[str, Any], Path]:
    manifest_path = manifest_path.resolve()
    manifest = _load_json(manifest_path)
    ground_value = manifest.get("ground_truth")
    if not isinstance(ground_value, str):
        raise ManifestValidationError("manifest ground_truth must be a path string")
    ground_truth_path = _repo_path(ground_value, manifest_path).resolve()
    ground_truth = _load_json(ground_truth_path)
    validate_manifest(
        manifest,
        ground_truth,
        manifest_path=manifest_path,
        strict_sources=strict_sources,
    )
    return manifest, ground_truth, ground_truth_path


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate M0 multilingual metadata and print a readiness report; no project commands are run."
    )
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--strict-sources",
        action="store_true",
        help="Fail while any source is marked fetch_required or is not pinned.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Explicitly write the JSON report here; source trees are always read-only.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        manifest, ground_truth, ground_truth_path = load_and_validate(
            args.manifest,
            strict_sources=args.strict_sources,
        )
        report = build_report(
            manifest,
            ground_truth,
            manifest_path=args.manifest.resolve(),
            ground_truth_path=ground_truth_path,
        )
    except ManifestValidationError as exc:
        print(f"multilang manifest validation failed:\n{exc}", file=sys.stderr)
        return 2
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

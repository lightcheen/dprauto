#!/usr/bin/env python3
"""Validate the sealed holdout without exposing its identity to production code."""

from __future__ import annotations

import argparse
import ast
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Any, Iterable


HERE = Path(__file__).resolve().parent
REPOSITORY_ROOT = HERE.parents[1]
COMMITMENT = HERE / "commitment.json"
DEFAULT_PRIVATE_MANIFEST = HERE / "private" / "manifest.json"
FULL_REVISION = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
REPOSITORY = re.compile(r"^[^/\s]+/[^/\s]+$")
ORACLE_TOKENS = ("evaluations/", "hidden-oracles", "hidden_oracles")
REPOSITORY_KEYS = {"repo", "repository", "project_repo_url"}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tracked_files(repository_root: Path = REPOSITORY_ROOT) -> tuple[Path, ...]:
    result = subprocess.run(
        (
            "git",
            "-c",
            f"safe.directory={repository_root}",
            "ls-files",
            "-z",
        ),
        cwd=repository_root,
        check=True,
        capture_output=True,
    )
    return tuple(
        repository_root / value.decode("utf-8")
        for value in result.stdout.split(b"\0")
        if value
    )


def repositories_in(value: Any, key: str = "") -> set[str]:
    repositories: set[str] = set()
    if isinstance(value, dict):
        for nested_key, nested_value in value.items():
            repositories.update(repositories_in(nested_value, nested_key))
    elif isinstance(value, list):
        for nested_value in value:
            repositories.update(repositories_in(nested_value, key))
    elif isinstance(value, str) and key in REPOSITORY_KEYS:
        normalized = value.removeprefix("https://github.com/").removesuffix(".git")
        if REPOSITORY.fullmatch(normalized):
            repositories.add(normalized.casefold())
    return repositories


def development_repositories(files: Iterable[Path]) -> set[str]:
    repositories: set[str] = set()
    for path in files:
        try:
            relative = path.relative_to(REPOSITORY_ROOT)
        except ValueError:
            continue
        if path.suffix != ".json" or relative.parts[:2] == ("evaluations", "holdout"):
            continue
        if path.name == "myapi.json":
            continue
        try:
            repositories.update(repositories_in(json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
    return repositories


def production_oracle_violations(source_root: Path) -> list[str]:
    violations: list[str] = []
    for path in sorted(source_root.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        lowered = source.casefold()
        for token in ORACLE_TOKENS:
            if token in lowered:
                violations.append(f"production source references evaluator path: {path}:{token}")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            modules: tuple[str, ...] = ()
            if isinstance(node, ast.Import):
                modules = tuple(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = (node.module,)
            imports_evaluator = any(
                module == "evaluations" or module.startswith("evaluations.")
                for module in modules
            )
            if imports_evaluator:
                violations.append(f"production source imports evaluator module: {path}")
    return violations


def identity_leaks(cases: list[dict[str, Any]], files: Iterable[Path]) -> list[str]:
    tokens = {
        str(case[field]).casefold()
        for case in cases
        for field in ("repository", "url", "revision")
    }
    leaks: list[str] = []
    for path in files:
        if not path.is_file() or path.name == "myapi.json":
            continue
        try:
            content = path.read_bytes().decode("utf-8", errors="ignore").casefold()
        except OSError:
            continue
        matched = sorted(token for token in tokens if token in content)
        if matched:
            leaks.append(f"holdout identity leaked into tracked file: {path}")
    return leaks


def validate_private_manifest(
    commitment: dict[str, Any],
    manifest: dict[str, Any],
    *,
    manifest_path: Path,
    files: tuple[Path, ...],
) -> list[str]:
    errors: list[str] = []
    if sha256(manifest_path) != commitment.get("private_manifest_sha256"):
        errors.append("private manifest does not match the committed SHA-256")
    if manifest.get("schema_version") != 1:
        errors.append("unsupported private manifest schema")
    if manifest.get("suite_id") != commitment.get("suite_id"):
        errors.append("private manifest suite_id differs from commitment")
    if manifest.get("visibility") != "evaluator_only":
        errors.append("private manifest must be evaluator_only")

    cases = manifest.get("cases", [])
    if len(cases) != commitment.get("case_count"):
        errors.append("private manifest case count differs from commitment")
    case_ids = [case.get("case_id") for case in cases]
    repositories = [str(case.get("repository", "")).casefold() for case in cases]
    if len(case_ids) != len(set(case_ids)):
        errors.append("private manifest contains duplicate case IDs")
    if len(repositories) != len(set(repositories)):
        errors.append("private manifest contains duplicate repositories")

    for case in cases:
        case_id = case.get("case_id", "unknown")
        repository = str(case.get("repository", ""))
        revision = str(case.get("revision", ""))
        expected_url = f"https://github.com/{repository}.git"
        if not REPOSITORY.fullmatch(repository):
            errors.append(f"invalid repository: {case_id}")
        if not FULL_REVISION.fullmatch(revision):
            errors.append(f"revision is not a full commit: {case_id}")
        if str(case.get("url", "")).casefold() != expected_url.casefold():
            errors.append(f"repository URL mismatch: {case_id}")
        if not case.get("ecosystem") or not case.get("source_origin"):
            errors.append(f"missing selection metadata: {case_id}")

    counts = dict(sorted(Counter(case.get("ecosystem") for case in cases).items()))
    if counts != commitment.get("ecosystem_counts"):
        errors.append("private manifest ecosystem counts differ from commitment")

    overlap = sorted(set(repositories) & development_repositories(files))
    if overlap:
        errors.append(f"holdout overlaps development history: {len(overlap)} repositories")
    errors.extend(identity_leaks(cases, files))
    return errors


def validate(
    *,
    private_manifest: Path = DEFAULT_PRIVATE_MANIFEST,
    require_private: bool = False,
) -> tuple[dict[str, Any], bool]:
    commitment = json.loads(COMMITMENT.read_text(encoding="utf-8"))
    errors: list[str] = []
    if commitment.get("schema_version") != 1:
        errors.append("unsupported commitment schema")
    if commitment.get("state") != "sealed_not_run":
        errors.append("holdout is not in sealed_not_run state")
    if not SHA256.fullmatch(str(commitment.get("private_manifest_sha256", ""))):
        errors.append("commitment does not contain a SHA-256 digest")
    if sum(commitment.get("ecosystem_counts", {}).values()) != commitment.get("case_count"):
        errors.append("public ecosystem counts do not sum to case count")

    files = tracked_files()
    errors.extend(production_oracle_violations(REPOSITORY_ROOT / "src"))
    private_present = private_manifest.is_file()
    if private_present:
        manifest = json.loads(private_manifest.read_text(encoding="utf-8"))
        errors.extend(
            validate_private_manifest(
                commitment,
                manifest,
                manifest_path=private_manifest,
                files=files,
            )
        )
    elif require_private:
        errors.append(f"required private manifest is missing: {private_manifest}")

    if errors:
        raise ValueError("holdout validation failed:\n- " + "\n- ".join(errors))
    return commitment, private_present


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--private-manifest", type=Path, default=DEFAULT_PRIVATE_MANIFEST)
    parser.add_argument("--require-private", action="store_true")
    args = parser.parse_args()
    commitment, private_present = validate(
        private_manifest=args.private_manifest,
        require_private=args.require_private,
    )
    print(
        f"validated sealed holdout commitment: cases={commitment['case_count']} "
        f"ecosystems={len(commitment['ecosystem_counts'])} private={private_present}"
    )


if __name__ == "__main__":
    main()

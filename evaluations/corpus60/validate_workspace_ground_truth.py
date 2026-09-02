#!/usr/bin/env python3
"""Validate workspace ground truth independently from DPRAuto parser output."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Any


HERE = Path(__file__).resolve().parent
GROUND_TRUTH = HERE / "hidden-oracles" / "workspaces.json"
ALLOWED_ROLES = {"primary", "dependency", "binding", "tool", "test", "example"}
ALLOWED_ENTRY_ROLES = {"primary", "alternative", "supporting"}
ALLOWED_REVIEW_STATUSES = {"agent_reviewed", "human_approved"}


def safe_relative(value: str) -> bool:
    path = PurePosixPath(value)
    return not path.is_absolute() and ".." not in path.parts and value not in {"", "/"}


def validate_document(
    manifest: dict[str, Any],
    document: dict[str, Any],
    *,
    corpus_root: Path,
    require_sources: bool,
) -> list[str]:
    errors: list[str] = []
    if document.get("schema_version") != 1:
        errors.append("unsupported workspace ground-truth schema")
    if document.get("suite_id") != manifest.get("suite_id"):
        errors.append("workspace ground truth suite_id does not match manifest")
    if document.get("visibility") != "evaluator_only":
        errors.append("workspace ground truth must be evaluator_only")

    manifest_cases = {case["case_id"]: case for case in manifest["cases"]}
    oracle_cases = document.get("cases", [])
    oracle_ids = [case.get("case_id") for case in oracle_cases]
    if len(oracle_ids) != len(set(oracle_ids)):
        errors.append("duplicate workspace ground-truth case IDs")
    if set(oracle_ids) != set(manifest_cases):
        errors.append("workspace ground-truth case IDs differ from manifest")

    for oracle in oracle_cases:
        case_id = oracle.get("case_id")
        manifest_case = manifest_cases.get(case_id)
        if manifest_case is None:
            continue
        if oracle.get("repository") != manifest_case["repository"]:
            errors.append(f"repository mismatch: {case_id}")
        if oracle.get("revision") != manifest_case["revision"]:
            errors.append(f"revision mismatch: {case_id}")
        if oracle.get("review_status") not in ALLOWED_REVIEW_STATUSES:
            errors.append(f"invalid review status: {case_id}")

        components = oracle.get("components", [])
        component_ids = [component.get("component_id") for component in components]
        if not components or len(component_ids) != len(set(component_ids)):
            errors.append(f"components must be non-empty and unique: {case_id}")
            continue
        primary_ids = oracle.get("primary_component_ids", [])
        if not primary_ids or not set(primary_ids) <= set(component_ids):
            errors.append(f"invalid primary component IDs: {case_id}")

        source_root = corpus_root / manifest_case["local_path"]
        for item in components:
            component_id = item.get("component_id", "")
            root = item.get("root", "")
            if root != "." and not safe_relative(root):
                errors.append(f"unsafe component root: {case_id}/{component_id}")
            if item.get("role") not in ALLOWED_ROLES:
                errors.append(f"invalid component role: {case_id}/{component_id}")
            if not item.get("languages"):
                errors.append(f"component has no languages: {case_id}/{component_id}")
            if require_sources and not (source_root / root).is_dir():
                errors.append(f"component root does not exist: {case_id}/{root}")

            entries = item.get("build_entries", [])
            if not entries or not any(entry.get("role") == "primary" for entry in entries):
                errors.append(f"component has no primary build entry: {case_id}/{component_id}")
            for build_entry in entries:
                path = build_entry.get("path", "")
                if not safe_relative(path):
                    errors.append(f"unsafe build entry: {case_id}/{path}")
                if build_entry.get("role") not in ALLOWED_ENTRY_ROLES:
                    errors.append(f"invalid build-entry role: {case_id}/{path}")
                if not build_entry.get("build_system"):
                    errors.append(f"missing build system: {case_id}/{path}")
                if require_sources and not (source_root / path).is_file():
                    errors.append(f"build entry does not exist: {case_id}/{path}")

            evidence_items = item.get("evidence", [])
            if not evidence_items:
                errors.append(f"component has no evidence: {case_id}/{component_id}")
            for evidence_item in evidence_items:
                path = evidence_item.get("path", "")
                if not safe_relative(path):
                    errors.append(f"unsafe evidence path: {case_id}/{path}")
                if require_sources and not (source_root / path).is_file():
                    errors.append(f"evidence path does not exist: {case_id}/{path}")

            dependencies = item.get("depends_on", [])
            if not set(dependencies) <= set(component_ids):
                errors.append(f"unknown component dependency: {case_id}/{component_id}")
            if component_id in dependencies:
                errors.append(f"component depends on itself: {case_id}/{component_id}")
    return errors


def load_and_validate(*, require_sources: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest = json.loads((HERE / "manifest.json").read_text(encoding="utf-8"))
    document = json.loads(GROUND_TRUTH.read_text(encoding="utf-8"))
    errors = validate_document(
        manifest,
        document,
        corpus_root=HERE,
        require_sources=require_sources,
    )
    if errors:
        raise ValueError("workspace ground truth invalid:\n- " + "\n- ".join(errors))
    return manifest, document


def main() -> None:
    _, document = load_and_validate(require_sources=True)
    component_count = sum(len(case["components"]) for case in document["cases"])
    approved = sum(case["review_status"] == "human_approved" for case in document["cases"])
    print(
        f"validated {len(document['cases'])} workspace oracles and {component_count} components; "
        f"human-approved={approved}"
    )


if __name__ == "__main__":
    main()

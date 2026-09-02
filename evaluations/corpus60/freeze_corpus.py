#!/usr/bin/env python3
"""Create and validate the immutable identity of the corpus60 development set."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
FREEZE_PATH = "freeze-lock.json"
BASELINE_PROBE_PATH = "baselines/static-probe-a31a05847e4a.json"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_entries(root: Path) -> list[tuple[str, str, int, str]]:
    """Return stable entries without following links outside the snapshot."""

    entries: list[tuple[str, str, int, str]] = []
    for current_root, directory_names, file_names in os.walk(root, followlinks=False):
        current = Path(current_root)
        retained_directories: list[str] = []
        for name in sorted(directory_names):
            path = current / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                entries.append(("link", relative, 0, os.readlink(path)))
            else:
                mode = stat.S_IMODE(path.stat().st_mode)
                entries.append(("directory", relative, mode, ""))
                retained_directories.append(name)
        directory_names[:] = retained_directories

        for name in sorted(file_names):
            path = current / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                entries.append(("link", relative, 0, os.readlink(path)))
                continue
            file_stat = path.stat()
            entries.append(
                (
                    "file",
                    relative,
                    stat.S_IMODE(file_stat.st_mode),
                    sha256(path),
                )
            )
    return sorted(entries, key=lambda item: (item[1], item[0]))


def snapshot_identity(root: Path) -> dict[str, Any]:
    if not root.is_dir():
        raise FileNotFoundError(f"snapshot directory does not exist: {root}")

    digest = hashlib.sha256()
    file_count = 0
    directory_count = 0
    symlink_count = 0
    total_file_bytes = 0
    for kind, relative, mode, content_identity in _tree_entries(root):
        encoded = json.dumps(
            [kind, relative, mode, content_identity],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        digest.update(encoded)
        digest.update(b"\n")
        if kind == "file":
            file_count += 1
            total_file_bytes += (root / relative).stat().st_size
        elif kind == "directory":
            directory_count += 1
        else:
            symlink_count += 1
    return {
        "tree_sha256": digest.hexdigest(),
        "file_count": file_count,
        "directory_count": directory_count,
        "symlink_count": symlink_count,
        "total_file_bytes": total_file_bytes,
    }


def create_freeze(
    corpus_root: Path,
    *,
    definition_revision: str,
    probe_path: Path,
) -> dict[str, Any]:
    freeze_path = corpus_root / FREEZE_PATH
    baseline_probe_path = corpus_root / BASELINE_PROBE_PATH
    if freeze_path.exists() or baseline_probe_path.exists():
        raise FileExistsError(
            "corpus freeze already exists; immutable baselines must not be overwritten"
        )

    manifest_path = corpus_root / "manifest.json"
    exclusions_path = corpus_root / "exclusions.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    probe = json.loads(probe_path.read_text(encoding="utf-8"))
    if probe.get("suite_id") != manifest.get("suite_id"):
        raise ValueError("probe suite_id does not match manifest")
    if probe.get("dprauto_revision") != manifest.get("dprauto_revision"):
        raise ValueError("probe implementation revision does not match manifest baseline")

    source_cases: list[dict[str, Any]] = []
    for case in manifest["cases"]:
        identity = snapshot_identity(corpus_root / case["local_path"])
        source_cases.append(
            {
                "case_id": case["case_id"],
                "repository": case["repository"],
                "revision": case["revision"],
                "local_path": case["local_path"],
                **identity,
            }
        )

    baseline_probe_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_probe_path.write_bytes(probe_path.read_bytes())
    document = {
        "schema_version": 1,
        "suite_id": manifest["suite_id"],
        "frozen_on": "2026-09-02",
        "definition_revision": definition_revision,
        "baseline_implementation_revision": manifest["dprauto_revision"],
        "manifest": {
            "path": "manifest.json",
            "sha256": sha256(manifest_path),
        },
        "exclusions": {
            "path": "exclusions.json",
            "sha256": sha256(exclusions_path),
        },
        "baseline_probe": {
            "path": BASELINE_PROBE_PATH,
            "sha256": sha256(baseline_probe_path),
            "scope": probe.get("scope", ""),
            "counts": probe.get("counts", {}),
        },
        "source_snapshot_policy": (
            "Digest regular-file content and modes, directory modes, and symlink targets; "
            "never follow symlinks. Git history is not part of downloaded snapshots."
        ),
        "cases": source_cases,
    }
    freeze_path.write_text(
        json.dumps(document, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return document


def validate_freeze(corpus_root: Path, *, require_sources: bool) -> list[str]:
    freeze_path = corpus_root / FREEZE_PATH
    if not freeze_path.is_file():
        return [f"missing corpus freeze: {freeze_path}"]
    document = json.loads(freeze_path.read_text(encoding="utf-8"))
    errors: list[str] = []

    for field in ("manifest", "exclusions", "baseline_probe"):
        identity = document.get(field, {})
        path = corpus_root / identity.get("path", "")
        if not path.is_file():
            errors.append(f"missing frozen {field}: {path}")
        elif sha256(path) != identity.get("sha256"):
            errors.append(f"frozen {field} digest changed")

    manifest_path = corpus_root / "manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if document.get("suite_id") != manifest.get("suite_id"):
            errors.append("freeze suite_id does not match manifest")
        if document.get("baseline_implementation_revision") != manifest.get(
            "dprauto_revision"
        ):
            errors.append("freeze implementation revision does not match manifest")

    frozen_cases = document.get("cases", [])
    if len(frozen_cases) != 60:
        errors.append(f"freeze must contain 60 cases, found {len(frozen_cases)}")
    if require_sources:
        for case in frozen_cases:
            root = corpus_root / case["local_path"]
            try:
                current = snapshot_identity(root)
            except FileNotFoundError:
                errors.append(f"missing frozen snapshot: {case['repository']}")
                continue
            for field in (
                "tree_sha256",
                "file_count",
                "directory_count",
                "symlink_count",
                "total_file_bytes",
            ):
                if current[field] != case.get(field):
                    errors.append(
                        f"frozen snapshot {field} changed: {case['repository']}"
                    )
    return errors


def main() -> None:
    parser = argparse.ArgumentParser()
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--create", action="store_true")
    action.add_argument("--check", action="store_true")
    parser.add_argument("--definition-revision")
    parser.add_argument("--probe", type=Path, default=HERE / "probe-results.json")
    parser.add_argument("--require-sources", action="store_true")
    args = parser.parse_args()

    if args.create:
        if not args.definition_revision:
            parser.error("--create requires --definition-revision")
        document = create_freeze(
            HERE,
            definition_revision=args.definition_revision,
            probe_path=args.probe,
        )
        print(
            f"froze {len(document['cases'])} corpus cases at "
            f"{document['definition_revision']}"
        )
        return

    errors = validate_freeze(HERE, require_sources=args.require_sources)
    if errors:
        print("corpus freeze validation failed:")
        for error in errors:
            print(f"- {error}")
        raise SystemExit(1)
    source_suffix = " and source snapshots" if args.require_sources else ""
    print(f"validated immutable corpus identity{source_suffix}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Validate corpus structure, source membership, exclusions, and local snapshots."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import Counter
from pathlib import Path


HERE = Path(__file__).resolve().parent
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalize_repo(value: str) -> str:
    return value.removeprefix("https://github.com/").removesuffix(".git").strip().lower()


def cxx_members(path: Path) -> set[str]:
    with path.open(newline="", encoding="utf-8") as handle:
        return {
            normalize_repo(row["path"])
            for row in csv.DictReader(handle)
            if row["path"].startswith("https://github.com/")
        }


def revision_members(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if len(fields) >= 2 and not fields[0].startswith("#"):
            result[fields[0].lower()] = fields[1]
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--require-sources", action="store_true")
    args = parser.parse_args()

    manifest = json.loads((HERE / "manifest.json").read_text(encoding="utf-8"))
    exclusions_doc = json.loads((HERE / "exclusions.json").read_text(encoding="utf-8"))
    exclusions = {item["repository"] for item in exclusions_doc["repositories"]}
    cases = manifest["cases"]
    errors: list[str] = []

    counts = Counter(case["language"] for case in cases)
    if counts != Counter({"cpp": 20, "python": 20, "java": 20}):
        errors.append(f"unexpected language counts: {dict(counts)}")
    repositories = [case["repository"].lower() for case in cases]
    if len(repositories) != len(set(repositories)):
        errors.append("duplicate repositories found")
    overlap = sorted(set(repositories) & exclusions)
    if overlap:
        errors.append(f"history overlap: {overlap}")

    catalogs = manifest["catalogs"]
    for name, catalog in catalogs.items():
        path = Path(catalog["path"])
        if not path.is_file():
            errors.append(f"missing catalog {name}: {path}")
        elif sha256(path) != catalog["sha256"]:
            errors.append(f"catalog digest changed: {name}")

    cxx = cxx_members(Path(catalogs["cxxcrafter_top100"]["path"]))
    python = revision_members(Path(catalogs["heragent_envbench_python"]["path"]))
    java = revision_members(Path(catalogs["envbench_jvm"]["path"]))

    for case in cases:
        repo = case["repository"].lower()
        revision = case["revision"]
        if not SHA_RE.fullmatch(revision):
            errors.append(f"invalid revision: {repo} {revision}")
        if case["snapshot_archive_kib"] > 20_000:
            errors.append(f"snapshot too large: {repo}")
        if case["language"] == "cpp":
            if repo not in cxx:
                errors.append(f"not in CXXCrafter Top100: {repo}")
            if case["cpp_translation_units"] < 1:
                errors.append(f"no C++ translation unit: {repo}")
        else:
            expected = python if case["language"] == "python" else java
            if expected.get(repo) != revision:
                errors.append(f"benchmark revision mismatch: {repo}")
            if not 500 <= case["source_code_lines"] <= 30_000:
                errors.append(f"code line bound violated: {repo}")
            if case["source_repository_kib"] > 20_000:
                errors.append(f"repository size bound violated: {repo}")

        if args.require_sources:
            root = HERE / case["local_path"]
            if not root.is_dir():
                errors.append(f"missing local snapshot: {repo}")
            for marker in case["build_markers"]:
                if root.is_dir() and not (root / marker).exists():
                    errors.append(f"missing build marker {marker}: {repo}")

    if errors:
        print("corpus validation failed:")
        for error in errors:
            print(f"- {error}")
        raise SystemExit(1)
    suffix = " including local snapshots" if args.require_sources else ""
    print(
        f"validated {len(cases)} unique cases{suffix}: "
        f"cpp={counts['cpp']} python={counts['python']} java={counts['java']}; "
        f"history exclusions={len(exclusions)}"
    )


if __name__ == "__main__":
    main()


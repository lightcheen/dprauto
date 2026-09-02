#!/usr/bin/env python3
"""Generate the source-audited workspace/component oracle for corpus60."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Any


HERE = Path(__file__).resolve().parent
OUTPUT = HERE / "hidden-oracles" / "workspaces.json"


def entry(path: str, build_system: str, role: str) -> dict[str, str]:
    return {"path": path, "build_system": build_system, "role": role}


def evidence(path: str, kind: str, locator: str = "") -> dict[str, str]:
    result = {"path": path, "kind": kind}
    if locator:
        result["locator"] = locator
    return result


def component(
    component_id: str,
    root: str,
    role: str,
    languages: list[str],
    entries: list[dict[str, str]],
    evidence_items: list[dict[str, str]],
    *,
    depends_on: list[str] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "component_id": component_id,
        "root": root,
        "role": role,
        "languages": languages,
        "build_entries": entries,
        "evidence": evidence_items,
    }
    if depends_on:
        result["depends_on"] = depends_on
    return result


def build_system(path: str, *, language: str, markers: list[str]) -> str:
    name = PurePosixPath(path).name
    if name in {"CMakeLists.txt", "CMakePresets.json"}:
        return "cmake"
    if name == "meson.build":
        return "meson"
    if name in {"configure", "configure.ac", "configure.in"}:
        return "autotools"
    if name == "Makefile":
        return "make"
    if name == "premake5.lua":
        return "premake"
    if name == "pom.xml" or name == "mvnw":
        return "maven"
    if name.startswith("build.gradle") or name.startswith("settings.gradle") or name == "gradlew":
        return "gradle"
    if language == "python" and name == "pyproject.toml":
        return "poetry" if "poetry.lock" in markers else "python-packaging"
    if language == "python" and name in {"setup.py", "setup.cfg"}:
        return "setuptools"
    raise ValueError(f"unreviewed build marker type: {path}")


def default_entries(case: dict[str, Any]) -> list[dict[str, str]]:
    language = case["language"]
    markers = case["build_markers"]
    ignored = {"poetry.lock", "tox.ini"}
    candidates = [marker for marker in markers if PurePosixPath(marker).name not in ignored]
    if language == "java":
        primary_names = {"pom.xml", "build.gradle", "build.gradle.kts"}
    elif language == "python":
        primary_names = {"pyproject.toml", "setup.py", "setup.cfg"}
    else:
        primary_names = {
            "CMakeLists.txt",
            "meson.build",
            "configure",
            "configure.ac",
            "Makefile",
        }

    result: list[dict[str, str]] = []
    primary_assigned = False
    for marker in candidates:
        name = PurePosixPath(marker).name
        if not primary_assigned and name in primary_names:
            role = "primary"
            primary_assigned = True
        elif name in {"mvnw", "gradlew", "settings.gradle", "settings.gradle.kts"}:
            role = "supporting"
        else:
            role = "alternative"
        result.append(entry(marker, build_system(marker, language=language, markers=markers), role))
    if not primary_assigned:
        raise ValueError(f"no reviewed primary build entry for {case['case_id']}")
    return result


def default_case(case: dict[str, Any]) -> dict[str, Any]:
    entries = default_entries(case)
    language = {"cpp": "C++", "python": "Python", "java": "Java"}[case["language"]]
    return {
        "case_id": case["case_id"],
        "repository": case["repository"],
        "revision": case["revision"],
        "review_status": "agent_reviewed",
        "primary_component_ids": ["primary"],
        "components": [
            component(
                "primary",
                ".",
                "primary",
                [language],
                entries,
                [evidence(item["path"], "build_file") for item in entries],
            )
        ],
    }


def vireo_case(case: dict[str, Any]) -> dict[str, Any]:
    return {
        "case_id": case["case_id"],
        "repository": case["repository"],
        "revision": case["revision"],
        "review_status": "agent_reviewed",
        "primary_component_ids": ["vireo-library"],
        "components": [
            component(
                "vireo-library",
                "vireo",
                "primary",
                ["C++", "C"],
                [
                    entry("vireo/configure", "autotools", "primary"),
                    entry("vireo/configure.ac", "autotools", "supporting"),
                ],
                [
                    evidence("vireo/configure", "build_file"),
                    evidence(
                        "README.md",
                        "documentation",
                        "How to Build Vireo and Tools: cd vireo; ./configure; make",
                    ),
                    evidence(
                        "vireo/configure.ac",
                        "dependency_declaration",
                        "AC_CONFIG_SUBDIRS([../imagecore])",
                    ),
                ],
                depends_on=["imagecore-library"],
            ),
            component(
                "imagecore-library",
                "imagecore",
                "dependency",
                ["C++", "C"],
                [entry("imagecore/configure", "autotools", "primary")],
                [evidence("imagecore/configure.ac", "build_file")],
            ),
            component(
                "imagetool-cli",
                "imagetool",
                "tool",
                ["C++"],
                [entry("imagetool/configure", "autotools", "primary")],
                [evidence("imagetool/configure.ac", "build_file")],
                depends_on=["imagecore-library"],
            ),
        ],
    }


def stockfish_case(case: dict[str, Any]) -> dict[str, Any]:
    return {
        "case_id": case["case_id"],
        "repository": case["repository"],
        "revision": case["revision"],
        "review_status": "agent_reviewed",
        "primary_component_ids": ["stockfish-engine"],
        "components": [
            component(
                "stockfish-engine",
                "src",
                "primary",
                ["C++"],
                [entry("src/Makefile", "make", "primary")],
                [
                    evidence("src/Makefile", "build_file"),
                    evidence(
                        "README.md",
                        "documentation",
                        "Compiling Stockfish: cd src; make -j profile-build",
                    ),
                ],
            )
        ],
    }


def pistache_case(case: dict[str, Any]) -> dict[str, Any]:
    result = default_case(case)
    primary = result["components"][0]
    primary["build_entries"] = [
        entry("meson.build", "meson", "primary"),
        entry("CMakeLists.txt", "cmake", "alternative"),
    ]
    primary["evidence"] = [
        evidence("meson.build", "build_file"),
        evidence(
            "README.md",
            "documentation",
            "Building from source: meson setup build",
        ),
        evidence("CMakeLists.txt", "build_file"),
    ]
    return result


def openalpr_case(case: dict[str, Any]) -> dict[str, Any]:
    return {
        "case_id": case["case_id"],
        "repository": case["repository"],
        "revision": case["revision"],
        "review_status": "agent_reviewed",
        "primary_component_ids": ["openalpr-native"],
        "components": [
            component(
                "openalpr-native",
                "src",
                "primary",
                ["C++", "C"],
                [entry("src/CMakeLists.txt", "cmake", "primary")],
                [
                    evidence("src/CMakeLists.txt", "build_file"),
                    evidence(
                        ".travis.yml",
                        "ci",
                        "mkdir ./src/build; cd ./src/build; cmake ..; make",
                    ),
                    evidence(
                        "Dockerfile",
                        "container_recipe",
                        "WORKDIR /srv/openalpr/src/build; cmake ..",
                    ),
                ],
            ),
            component(
                "python-binding",
                "src/bindings/python",
                "binding",
                ["Python", "C++"],
                [
                    entry("src/bindings/python/setup.py", "setuptools", "primary"),
                    entry("src/bindings/python/CMakeLists.txt", "cmake", "supporting"),
                ],
                [evidence("src/bindings/python/setup.py", "build_file")],
                depends_on=["openalpr-native"],
            ),
            component(
                "java-binding",
                "src/bindings/java",
                "binding",
                ["Java", "C++"],
                [entry("src/bindings/java/CMakeLists.txt", "cmake", "primary")],
                [evidence("src/bindings/java/CMakeLists.txt", "build_file")],
                depends_on=["openalpr-native"],
            ),
            component(
                "go-binding",
                "src/bindings/go",
                "binding",
                ["Go", "C++"],
                [entry("src/bindings/go/CMakeLists.txt", "cmake", "primary")],
                [evidence("src/bindings/go/CMakeLists.txt", "build_file")],
                depends_on=["openalpr-native"],
            ),
        ],
    }


SPECIAL_CASES = {
    "cpp-twitter--vireo": vireo_case,
    "cpp-pistacheio--pistache": pistache_case,
    "cpp-official-stockfish--stockfish": stockfish_case,
    "cpp-openalpr--openalpr": openalpr_case,
}


def build_document(manifest: dict[str, Any]) -> dict[str, Any]:
    cases = []
    for case in manifest["cases"]:
        builder = SPECIAL_CASES.get(case["case_id"], default_case)
        cases.append(builder(case))
    return {
        "schema_version": 1,
        "suite_id": manifest["suite_id"],
        "visibility": "evaluator_only",
        "reviewed_on": "2026-09-02",
        "review_policy": (
            "Every expected path is checked against the frozen source. Source-audited overrides "
            "include documentation or CI evidence. Records are agent-reviewed and "
            "must not be represented as human-approved until a person signs them off."
        ),
        "cases": cases,
    }


def main() -> None:
    manifest = json.loads((HERE / "manifest.json").read_text(encoding="utf-8"))
    document = build_document(manifest)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps(document, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"wrote workspace ground truth for {len(document['cases'])} cases")


if __name__ == "__main__":
    main()

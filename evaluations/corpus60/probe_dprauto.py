#!/usr/bin/env python3
"""Run DPRAuto's production parser and deterministic plan generation over corpus60."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from dprauto.adapters.multilang.parser import MultiLanguageProjectParser
from dprauto.domain.models import SourceReference
from dprauto.strategies.multilang import JVMTemplateStrategy, NativeTemplateStrategy
from dprauto.strategies.template import TemplateStrategy


HERE = Path(__file__).resolve().parent


def probe(case: dict[str, Any], parser: MultiLanguageProjectParser) -> dict[str, Any]:
    root = HERE / case["local_path"]
    record: dict[str, Any] = {
        "case_id": case["case_id"],
        "language": case["language"],
        "repository": case["repository"],
        "revision": case["revision"],
    }
    try:
        profile = parser.parse(SourceReference(case["url"], case["revision"]), root)
        record.update(
            {
                "status": "parsed",
                "parser": profile.metadata.get("parser_registry_selection", ""),
                "detected_languages": list(profile.languages),
                "package_managers": list(profile.package_managers),
                "primary_build_system": profile.metadata.get("primary_build_system", ""),
                "build_files": list(profile.build_files),
                "command_count": len(profile.commands),
            }
        )
    except Exception as exc:
        record.update(
            {
                "status": "unsupported",
                "error_stage": "parse",
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
        )
        return record

    try:
        strategy = {
            "python": TemplateStrategy,
            "java": JVMTemplateStrategy,
            "cpp": NativeTemplateStrategy,
        }[case["language"]](None)
        plan = strategy.create_plan(profile)
        record.update(
            {
                "status": "planned",
                "strategy": plan.strategy,
                "generated_files": [item.path for item in plan.generated_files],
            }
        )
    except Exception as exc:
        record.update(
            {
                "status": "parsed_not_planned",
                "error_stage": "plan",
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
        )
    return record


def main() -> None:
    argument_parser = argparse.ArgumentParser()
    argument_parser.add_argument("--strict", action="store_true")
    args = argument_parser.parse_args()
    manifest = json.loads((HERE / "manifest.json").read_text(encoding="utf-8"))
    parser = MultiLanguageProjectParser()
    records = [probe(case, parser) for case in manifest["cases"]]
    counts = Counter((record["language"], record["status"]) for record in records)
    output = {
        "schema_version": 1,
        "suite_id": manifest["suite_id"],
        "dprauto_revision": manifest["dprauto_revision"],
        "scope": "production parser plus deterministic plan generation; no Docker build",
        "counts": {
            language: {
                "planned": counts[(language, "planned")],
                "parsed_not_planned": counts[(language, "parsed_not_planned")],
                "unsupported": counts[(language, "unsupported")],
            }
            for language in ("cpp", "python", "java")
        },
        "records": records,
    }
    (HERE / "probe-results.json").write_text(
        json.dumps(output, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    for language, values in output["counts"].items():
        print(
            f"{language}: planned={values['planned']} "
            f"parsed_not_planned={values['parsed_not_planned']} "
            f"unsupported={values['unsupported']}"
        )
    failures = [record for record in records if record["status"] != "planned"]
    for record in failures:
        print(
            f"{record['status']}\t{record['repository']}\t"
            f"{record['error_stage']}\t{record['error']}"
        )
    if args.strict and failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

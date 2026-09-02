#!/usr/bin/env python3
"""Build the frozen DPRAuto 60-project capability corpus manifest."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]

HISTORY_INPUTS = (
    "evaluations/baselines/python-21-20260818.json",
    "evaluations/baselines/python-21-m9-20260824.json",
    "evaluations/multilang/ground-truth/cases.json",
    "evaluations/multilang/manifest.json",
    "evaluations/prompt12/manifest.json",
    "evaluations/prompt13/audited-summary.json",
    "evaluations/prompt13/comparison.json",
)

CATALOGS = {
    "cxxcrafter_top100": Path(
        "/home/master/auto-build/CXXCrafter/"
        "CXXCrafter-Community-Edition/data/top100_dataset.csv"
    ),
    "heragent_envbench_python": Path(
        "/home/master/auto-build/HerAgent-main/projects/envbench-python.txt"
    ),
    "envbench_jvm": Path(
        "/home/master/auto-build/HerAgent-main/projects/envbench/envbench-jvm.txt"
    ),
}

# repo|revision|archive KiB|C++ translation units|build marker(s)
CPP_ROWS = """
skypjack/entt|85c6bba014049b5de8fad49d25424df2f1f6a8c1|932|136|CMakeLists.txt
google/guetzli|214f2bb42abf5a577c079d00add5d6cc470620d3|287|23|Makefile,premake5.lua
sqlitebrowser/sqlitebrowser|da45c7d528c38986a271fa7be887f95f5b68facb|4916|277|CMakeLists.txt
strukturag/libde265|c45b2da8f4b6e2b7bf9df1197e5633fbe6adc928|310|51|CMakeLists.txt
treefrogframework/treefrog-framework|f54f60220aa4b10ed69d541a3a3547259fc695c4|10634|317|configure
ipkn/crow|2b43d3cd6a9a9cdbc99dfef9b86ff3f3027f3d1f|86|9|CMakeLists.txt
drogonframework/drogon|acb39fa39d37474142b80c888dc72a0953dc79c1|666|237|CMakeLists.txt
oatpp/oatpp|f83d648fd82dc222ef88aabbafb68efbd7d7bf50|315|197|CMakeLists.txt
wjakob/nanogui|e9ec8a1a9861cf578d9c6e85a6420080aa715c03|2705|49|CMakeLists.txt
mikke89/RmlUi|b7b4a0688262832eacf3b9abb41f8bbe73868af8|3932|414|CMakeLists.txt
flameshot-org/flameshot|24ce091c0f95d0fdfe15cb9c085be88be8195b4f|16044|113|CMakeLists.txt
polybar/polybar|f99e0b1c7a5b094f5a04b14101899d0cb4ece69d|359|121|CMakeLists.txt
twitter/vireo|7f0bc1ad50f5eaf30a09de209f0172f2def36abd|1961|112|imagecore/configure
pistacheio/pistache|a7b0dc9ec01b96b95bde55331d83e27a9bf800b9|822|79|CMakeLists.txt,meson.build
exaloop/seq|625acb39fe9365a2d67bfec1642e87f9954fe29f|344|10|CMakeLists.txt
aseprite/aseprite|b9d291bff7afb8e78a9fb3d38fcdf57a80951dc4|1851|800|CMakeLists.txt
official-stockfish/Stockfish|3f6f417b87c0e80ee30914b6b539b4ab7d3b2a5b|284|30|src/Makefile
jacobdufault/cquery|9b80917cbf7d26b78ec62b409442ecf96f72daf9|251|215|CMakeLists.txt
cppit/jucipp|89c9fd742c287ffde5fa23fc14f6590541b63611|557|59|CMakeLists.txt
openalpr/openalpr|736ab0e608cf9b20d92f36a873bb1152240daa98|1767|99|src/CMakeLists.txt
""".strip()

# repo|revision|repository KiB|code lines|archive KiB|build marker(s)
PYTHON_ROWS = """
eylles/pywal16|ab1f5c87f1a8b8423d66cab3cb7d0b4df9f47636|833|2948|73|setup.py
rstcheck/rstcheck|b8ddb006f1f49a9bdaf40e45f0bc0b070f460513|924|3091|39|pyproject.toml,tox.ini
astropy/extension-helpers|e6996c540c5bf23e89b45f170e89bd8c9f22a86e|980|1437|25|pyproject.toml,tox.ini
cpcloud/protoletariat|5c85c8d27fb6535108a1f2846f4f0bd820472541|1052|2770|46|poetry.lock,pyproject.toml
zostera/django-bootstrap4|79f4c059f33d3ad11b66c305f295eb42d71f95da|1126|3067|42|pyproject.toml,tox.ini
jaraco/inflect|1f42293f30caacb0637f7eb30a483b44489f4812|1184|6410|61|pyproject.toml,tox.ini
asdf-format/asdf-standard|c367d14fae4b5155679e2c40b531140586e25daf|1188|10247|128|pyproject.toml,tox.ini
rollbar/pyrollbar|8493ac03c3468c2349c968726adffa5fd5661d0e|1347|8854|81|setup.py
smarr/rebench|1de0e22fb3e9578b3aeb7a994481ead300fa977f|1358|8702|111|setup.py
python/importlib_metadata|f3901686abc47853523f3b211873fc2b9e0c5ab5|1382|2815|46|pyproject.toml,tox.ini
wolph/python-progressbar|5d53d59a37247dd9e4663dd86ca79b16cbc92d9b|1414|9107|89|pyproject.toml,tox.ini
m-o-a-t/moat-mqtt|fcd0f9bf07548604d19d159ffb5fc543e78ce597|1467|7766|80|pyproject.toml,tox.ini
pypa/twine|6fbf880ee60915cf1666348c4bdd78a10415f2ac|1485|5439|216|pyproject.toml,tox.ini
mollie/mollie-api-python|957b44fddb9d4d833508120022c7f291f5ca8d06|1508|12224|87|pyproject.toml,setup.py
mov-cli/mov-cli|32f8af4f15804dd6ce88d8f781a12b918119e64c|1555|3706|75|pyproject.toml
piskvorky/smart_open|5a826134eb23c86087d06db3b78b25484e7e2e6d|1616|9323|136|pyproject.toml,setup.py
microsoftgraph/msgraph-sdk-python-core|e36c17424c9d443bdfeafc32f32682529ce25f95|1696|2323|39|pyproject.toml
psf/pyperf|7b9a23adb3ceeb182f58a03e453158dbe081a7e7|1783|14675|217|pyproject.toml,tox.ini
fatal1ty/mashumaro|e945ee4319db49da9f7b8ede614e988cc8c8956b|1893|23405|151|pyproject.toml,setup.py
ubernostrum/django-registration|dc80d4e3f90d64d12c35a6c494aa1ec9b36a1705|1918|4893|80|pyproject.toml
""".strip()

JAVA_ROWS = """
prisma-capacity/cryptoshred|4a22ea14f5bc38a5b773213ca34cae84a084a6db|1144|2680|35|pom.xml
liquibase/liquibase-hibernate|3fd0fbca8f627837cf8d143efa85eddc5a5a7cf9|1304|5160|46|pom.xml
camunda-community-hub/zeebe-test-container|d228f0c267a8f70b81e4fa1594b9ec4d364c156a|1418|8729|99|pom.xml
zalando/problem-spring-web|da31acb922e486cebeff37d9fbd96322d70e4fc8|1547|8461|73|mvnw,pom.xml
AxonIQ/axonserver-connector-java|ac25a6fedcb1fecdb5a8cdb08ab96d0f28c11953|1585|13815|186|mvnw,pom.xml
dunctebot/skybot-lavalink-plugin|197f25fdb8591400b8dd53634d0fae3e32904c63|1620|3293|89|build.gradle.kts,gradlew,settings.gradle.kts
openrewrite/rewrite-maven-plugin|dd2645af1a6a3617bf13f0bd930eeade89180e94|1753|5902|103|gradlew,mvnw,pom.xml
eclipse/microprofile-fault-tolerance|3543ebcc367a89adcba04351c55ffc9360a0cbaf|1842|18401|176|pom.xml
sigstore/sigstore-java|27a57c87ea6c5cf6be4f64d824cd07f5656c67ac|2181|18158|361|build.gradle.kts,gradlew,settings.gradle.kts
jenkinsci/plugin-compat-tester|3027df385a49c976f87e2ae974f148afdd663822|2283|3928|42|pom.xml
mrniko/netty-socketio|82433154956ac60709357e7157affc8dde371af7|2285|7931|73|pom.xml
micronaut-projects/micronaut-platform|102825e1037abf00069583f6e388292cbfae7b86|2517|2087|74|build.gradle,gradlew,settings.gradle
jenkinsci/docker-plugin|16e30a603830d0b685ac34d544154fc7ffb84600|2565|16086|190|pom.xml
avaje/avaje-inject|9ca8d1af5d0059b46c85e6046aa740d288df78c4|2567|22525|251|pom.xml
grapheneos/camera|26e9d4120add6a4e1bd551c2f1be996ee756174a|2581|18744|331|build.gradle.kts,gradlew,settings.gradle.kts
scribejava/scribejava|8970e8eeb0aff840d0b223890e9c392ee19218f7|2594|16891|128|pom.xml
braisdom/objectivesql|f49e6eba6e7b08748c9945fbbaaa7728239b264e|2600|21286|190|pom.xml
fortnoxab/reactive-wizard|1f3a26dd6b4667b26f2e8a6426c094afc65d2fa8|2857|26936|225|pom.xml
grapheneos/attestationserver|e9699f9dcd3b736853f674f6678431d561cb4bb2|2917|10439|293|build.gradle.kts,gradlew
gradle/native-platform|25830184c8d04bca9c80b26df69c5936431f808c|3046|15063|211|build.gradle.kts,gradlew,settings.gradle.kts
""".strip()


def normalize_repo(value: str) -> str:
    return value.removeprefix("https://github.com/").removesuffix(".git").strip().lower()


def collect_history() -> dict[str, list[str]]:
    origins: dict[str, set[str]] = {}

    def walk(value: Any, source: str, key: str = "") -> None:
        if isinstance(value, dict):
            for nested_key, nested_value in value.items():
                walk(nested_value, source, nested_key)
        elif isinstance(value, list):
            for nested_value in value:
                walk(nested_value, source, key)
        elif isinstance(value, str) and key in {"repo", "repository", "project_repo_url"}:
            repo = normalize_repo(value)
            if repo.count("/") == 1 and not repo.startswith("/"):
                origins.setdefault(repo, set()).add(source)

    for relative in HISTORY_INPUTS:
        path = REPO_ROOT / relative
        walk(json.loads(path.read_text(encoding="utf-8")), relative)
    return {repo: sorted(files) for repo, files in sorted(origins.items())}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def case_id(language: str, repo: str) -> str:
    return f"{language}-{repo.lower().replace('/', '--').replace('_', '-')}"


def cases() -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in CPP_ROWS.splitlines():
        repo, revision, archive_kib, cpp_files, markers = row.split("|")
        result.append(
            {
                "case_id": case_id("cpp", repo),
                "language": "cpp",
                "repository": repo,
                "url": f"https://github.com/{repo}.git",
                "revision": revision,
                "dataset": "cxxcrafter_top100",
                "snapshot_archive_kib": int(archive_kib),
                "cpp_translation_units": int(cpp_files),
                "build_markers": markers.split(","),
                "local_path": f"sources/cpp/{repo.lower().replace('/', '__')}",
            }
        )
    for language, dataset, rows in (
        ("python", "heragent_envbench_python", PYTHON_ROWS),
        ("java", "envbench_jvm", JAVA_ROWS),
    ):
        for row in rows.splitlines():
            repo, revision, repo_kib, code_lines, archive_kib, markers = row.split("|")
            result.append(
                {
                    "case_id": case_id(language, repo),
                    "language": language,
                    "repository": repo,
                    "url": f"https://github.com/{repo}.git",
                    "revision": revision,
                    "dataset": dataset,
                    "source_repository_kib": int(repo_kib),
                    "source_code_lines": int(code_lines),
                    "snapshot_archive_kib": int(archive_kib),
                    "build_markers": markers.split(","),
                    "local_path": f"sources/{language}/{repo.lower().replace('/', '__')}",
                }
            )
    return result


def main() -> None:
    history = collect_history()
    selected = cases()
    overlap = sorted({case["repository"].lower() for case in selected} & set(history))
    if overlap:
        raise SystemExit(f"selected repositories overlap DPRAuto history: {overlap}")

    exclusions = {
        "schema_version": 1,
        "generated_on": "2026-09-02",
        "meaning": "Repositories found in frozen DPRAuto evaluation JSON before corpus60.",
        "history_inputs": list(HISTORY_INPUTS),
        "repositories": [
            {"repository": repo, "evidence_files": files} for repo, files in history.items()
        ],
    }
    (HERE / "exclusions.json").write_text(
        json.dumps(exclusions, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    manifest = {
        "schema_version": 1,
        "suite_id": "dprauto-crosslang-60-20260902",
        "created_on": "2026-09-02",
        "dprauto_revision": "a31a05847e4a62cbb58c16913de05dd9a7a98fe7",
        "purpose": "New small-project corpus for measuring DPRAuto environment construction.",
        "counts": {"total": 60, "cpp": 20, "python": 20, "java": 20},
        "selection_policy": {
            "history_definition": (
                "Exclude every repository named in the frozen DPRAuto evaluation JSON listed "
                "in exclusions.json. Source-benchmark membership is intentionally not excluded."
            ),
            "python_java_bounds": {
                "main_language_must_match": True,
                "source_repository_kib_max": 20000,
                "source_code_lines_min": 500,
                "source_code_lines_max": 30000,
                "archived_or_disabled": False,
            },
            "cpp_bounds": {
                "snapshot_archive_kib_max": 20000,
                "cpp_translation_units_min": 1,
            },
            "revision_policy": (
                "Use benchmark revisions for Python/JVM. CXXCrafter Top100 has no revisions, "
                "so pin the reachable HEAD observed on 2026-09-02."
            ),
        },
        "catalogs": {
            "cxxcrafter_top100": {
                "project": "CXXCrafter Community Edition",
                "path": str(CATALOGS["cxxcrafter_top100"]),
                "sha256": digest(CATALOGS["cxxcrafter_top100"]),
                "upstream": "https://github.com/seclab-fudan/CXXCrafter-Community-Edition",
            },
            "heragent_envbench_python": {
                "project": "HerAgent",
                "path": str(CATALOGS["heragent_envbench_python"]),
                "sha256": digest(CATALOGS["heragent_envbench_python"]),
                "benchmark": "EnvBench-Python",
                "upstream_metadata": "https://huggingface.co/datasets/JetBrains-Research/EnvBench",
            },
            "envbench_jvm": {
                "project": "EnvBench",
                "path": str(CATALOGS["envbench_jvm"]),
                "sha256": digest(CATALOGS["envbench_jvm"]),
                "upstream": "https://huggingface.co/datasets/JetBrains-Research/EnvBench",
            },
        },
        "cases": selected,
    }
    (HERE / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"wrote {len(selected)} cases and {len(history)} historical exclusions")


if __name__ == "__main__":
    main()

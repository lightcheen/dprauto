# DPRAuto multilingual evaluation corpus (M0)

This directory freezes the corpus and command semantics used by the
multilingual roadmap. M0 performs metadata validation only: it does not build,
test, fetch, or modify any candidate repository.

Run the validator from the DPRAuto repository root:

```bash
python3 evaluations/multilang/run_evaluation.py
```

`--strict-sources` is the execution gate. Since M9 it also compares directly
fetched sources' local Git HEAD with the pinned manifest revision and passes
for all 21 cases. `--output PATH` writes the deterministic readiness report
only when an output path is explicitly provided.

## Test project directories

The original 17 local dataset snapshots below are ready for evaluation; the
four CXXCrafter sources added in M9 are listed immediately afterward.

| Language | Repository | Dataset | Source directory |
|---|---|---|---|
| Python | yubico/yubikey-manager | EnvBench-Python | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/envbench-python-paper-yubico-yubikey-manager-fbdae2bc12ba` |
| Python | dagshub/client | EnvBench-Python | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/envbench-python-paper-dagshub-client-f8d89c53c733` |
| Python | piccolo-orm/piccolo | EnvBench-Python | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/envbench-python-paper-piccolo-orm-piccolo-17c0a8859c19` |
| Python | compserv/hknweb | EnvBench-Python | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/envbench-python-paper-compserv-hknweb-422acacc4b1a` |
| Python | gamesdonequick/donation-tracker | EnvBench-Python | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/envbench-python-paper-gamesdonequick-donation-tracker-63411a9fd9d8` |
| Python | tmux-python/tmuxp | EnvBench-Python | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/envbench-python-paper-tmux-python-tmuxp-3e0fec3596cc` |
| Python | tiangolo/fastapi | Installamatic | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/installamatic-tiangolo-fastapi-212fd5e` |
| Python | django/django | ExecutionAgent | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/executionagent-django-django-e95468ed97b1` |
| Java | apache/commons-csv | ExecutionAgent | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/executionagent-apache-commons-csv-2d44689ec75e` |
| Java | mybatis/mybatis-3 | ExecutionAgent | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/executionagent-mybatis-mybatis-3-58e2d5e9b035` |
| Java | ReactiveX/RxJava | ExecutionAgent | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/executionagent-reactivex-rxjava-6b28009fd085` |
| Java | spring-projects/spring-security | ExecutionAgent | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/executionagent-spring-projects-spring-security-0d5f42f8529c` |
| C++ | ccache/ccache | ExecutionAgent | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/executionagent-ccache-ccache-7f3e822efb1b` |
| C | json-c/json-c | ExecutionAgent | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/executionagent-json-c-json-c-a1249bfda0f6` |
| C++ | nlohmann/json | ExecutionAgent | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/executionagent-nlohmann-json-55f93686c015` |
| C | libevent/libevent | ExecutionAgent | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/executionagent-libevent-libevent-a994a52d5373` |
| C | distcc/distcc | ExecutionAgent | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/executionagent-distcc-distcc-a627b26f08cd` |

M9 fetched the four CXXCrafter repositories into the directories reserved in M0:

| Language | Repository | Dataset row | Reserved directory |
|---|---|---|---|
| C | rui314/8cc | `top100_dataset.csv` | `/home/master/auto-build/CXXCrafter/datasets/top100-src/8cc` |
| C++ | rui314/mold | `top100_dataset.csv` | `/home/master/auto-build/CXXCrafter/datasets/top100-src/mold` |
| C++ | google/leveldb | `top100_dataset.csv` | `/home/master/auto-build/CXXCrafter/datasets/top100-src/leveldb` |
| C++ | simdjson/simdjson | `top100_dataset.csv` | `/home/master/auto-build/CXXCrafter/datasets/top100-src/simdjson` |

Dataset catalogs are recorded as absolute, auditable paths in `manifest.json`.
Each source now has a pinned revision. Because the CXXCrafter CSV provides
repository URLs but no revisions, M9 resolved each remote HEAD once, recorded
the full commit ID in `manifest.json`, and initialized LevelDB's test submodules
at the commits pinned by that revision. Strict source validation therefore
covers all 21 cases without floating branches.

## Ground-truth rules

Every case declares separate `build`, `test`, `run`, and `setup` commands. A
test command must use an ordinary repository test runner. Download, lint,
format, fuzz, docs, and release commands cannot satisfy Testability. Service,
framework, executable, and process prerequisites are part of the test
contract rather than implicit repair hints.

## M1 parser baseline

M1 replaces the production composition root's Python-only parser with a
priority-ordered multilingual parser registry. All 17 locally ready M0 source
snapshots parse without executing project code: 8 select `python-rules-v1`, 4
select `jvm-rules-v1`, and 5 select `native-rules-v1`.

JVM profiles cover Maven/Gradle wrappers, Java toolchains, modules, and real
working directories. Native profiles cover CMake, Meson, Autotools, Make,
language standards, subdirectories, ordered build pipelines, and ordinary
test entrypoints. Command candidates are capped per semantic purpose; common
inferred commands remain first so a large CI matrix cannot dominate the Agent
search space.

M1 is parsing and command intelligence only. It does not yet provide
Java/C/C++ container strategies, Tree-sitter AST indexing, Neo4j persistence,
semantic embeddings, or service orchestration.

## M2 Tree-sitter and repository knowledge graph

M2 adds a bounded, backend-neutral repository graph with directory/file,
Tree-sitter named AST node, declaration/import, and configuration/document text
nodes. The production adapter loads 16 Tree-sitter grammars, while the storage
port supports both an in-memory implementation and isolated, transactional
Neo4j persistence.

Real syntax parsing is exercised against these M0 directories:

- Python: `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/envbench-python-paper-yubico-yubikey-manager-fbdae2bc12ba`
- Java: `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/executionagent-apache-commons-csv-2d44689ec75e`
- C++: `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/executionagent-ccache-ccache-7f3e822efb1b`

See `M2_TREE_SITTER_KNOWLEDGE_GRAPH_2026-08-25.md` for limits, measured graph
sizes, dependency compatibility, and the explicit boundary between structured
graph retrieval and later embedding/multi-turn retrieval work.

## M3 offline semantic and multi-turn retrieval

M3 combines code-aware sparse vectors, literal term evidence, graph-neighbor
propagation, structured filters, and per-path diversification. It adds a
replaceable `SemanticEncoder` port, bounded multi-turn sessions, and the
read-only `query_repository_context` Agent investigation tool.

The real-data probes include the M0 Django, Apache Commons CSV, and ccache
directories plus the CNB corpus's Testfixtures snapshot for its actual
`conftest.py` Sybil imports. All five long-tail probes place an expected file at
rank 1. See `M3_SEMANTIC_CONTEXT_RETRIEVAL_2026-08-25.md` for exact directories,
graph sizes, scores, limits, and current persistence boundaries.

## M4 deterministic JVM and native builds

M4 adds parser-owned Maven/Gradle and CMake/Meson/Autotools/Make container strategies,
language-neutral dependency contracts, compiled-library runtime probes, ordinary test-command
preference, and monorepo-aware Docker working directories. All nine ready Java/C/C++ snapshots
produce deterministic plans. json-c completes a real build plus Installability, CTest Testability,
and Runnability; the Java execution probe is explicitly infrastructure-blocked by container DNS.
See `M4_JVM_NATIVE_BUILD_VERIFICATION_2026-08-25.md` for every source directory and exact evidence.

## M5 service and framework prerequisites

M5 adds bounded PostgreSQL/Redis lifecycle orchestration, Django settings and repository-runner
selection, allowlisted initialization commands, and system executable contracts such as tmux.
Services are started only when the selected test command has an explicit binding. See
`M5_SERVICE_FRAMEWORK_ORCHESTRATION_2026-08-25.md` for the real dataset directories and Docker
service probes.

## M6 minimal selected-test dependency closure

M6 statically closes a bounded pytest slice over its imports, ancestor `conftest.py` files, local
modules, fixtures, and pytest configuration, then slices broad dev/qa requirements to the exact
declared constraints needed by that slice. Dagshub's real pipeline excludes FiftyOne and datasets,
installs four test requirements, and passes 108 tests. See
`M6_MINIMAL_TEST_DEPENDENCY_CLOSURE_2026-08-26.md` for all audited directories and fallback limits.

## M7 bounded repair search and execution feedback

M7 narrows mutation tools from classified failure evidence before LLM planning, feeds policy and
duplicate-method rejection back into a bounded same-round replan, and carries structured real
preflight/build/test outcomes into later rounds. Replayed M9/M10 failures expose only the causal
Testability overlay for sybil, Piccolo's PostgreSQL extra, aiohttp, requests-cache, and
python-multipart; deterministic service/executable/runner-contract cases remain outside the LLM
space. See `M7_BOUNDED_REPAIR_SEARCH_FEEDBACK_2026-08-27.md` for exact source and run directories.

## M8 CAS-protected build-script mutation

M8 requires whole-file replacements to cite a complete, prompt-visible `read_file` result and its
source SHA-256. Paged or context-truncated files use `patch_build_script`, which replaces one exact
observed fragment under the same SHA precondition. Structured mutations share the CAS writer, and
candidate promotion verifies both the accepted workspace's before digest and the candidate's after
digest. The isolated real-data probe uses ccache's
`dockerfiles/ubuntu-24.04/Dockerfile`; see `M8_CAS_BUILD_SCRIPT_MUTATION_2026-08-27.md` for its exact
source directory, digest, diff and stale-write rejection.

## M9 pinned CXXCrafter native long-tail validation

M9 fetches the four reserved CXXCrafter C/C++ sources at full pinned commits and makes strict
evaluation compare each local Git HEAD with its manifest revision. Real 8cc, mold, LevelDB and
simdjson builds drive CLI-versus-library command semantics, root-only native Dockerfile selection,
generated-Dockerfile ignore policy, bounded CTest parallelism, minimal CMake test targets and
test-driver-reachable shebang executable contracts. See
`M9_CXXCRAFTER_NATIVE_LONGTAIL_2026-08-27.md` for the exact source/submodule directories, commands,
timings, pass/fail evidence and remaining Python 2/platform boundary.

## M10 fixed-21 deterministic multilingual execution

M10 adds an implementation-aware, atomically checkpointed execution runner and runs all 21 pinned
Python, Java, C and C++ cases through production deterministic build and layered verification with
zero Agent/LLM calls. Standard builds pass for 15/21 and strict build/install/test/run passes for
5/21. The complete records expose command-role false targets, four systematic JVM environment
failures, incomplete selected-test prerequisite closure, and over-budget ordinary suites. See
`M10_FULL21_DETERMINISTIC_RESULTS_2026-08-28.md` for the exact 21 source directories, commands,
per-project evidence, timing, failure clusters, and next implementation order.

## M11 command-semantics firewall

M11 shares one role-aware run selector between generated images and verification, requires positive
server behavior for Web commands, rejects optional/tests/docs entrypoints, retries explicit CLI
help probes, separates CMake tests from image builds, completes Autotools configure ordering, and
fixes Web probing under Docker host-network configuration. Repository-evidenced Django migrations
are executed before `runserver`, while proxy responses and HTTP 5xx are rejected. On the five pinned
false-target cases, build success moves from 3/5 to 5/5 and strict success from 0/5 to 2/5 without
an Agent or LLM.
See `M11_COMMAND_SEMANTICS_RESULTS_2026-08-28.md` for exact directories, implementation identity,
commands, layer transitions, and the remaining dependency/user/process failures.

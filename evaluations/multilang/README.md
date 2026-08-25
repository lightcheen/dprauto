# DPRAuto multilingual evaluation corpus (M0)

This directory freezes the corpus and command semantics used by the
multilingual roadmap. M0 performs metadata validation only: it does not build,
test, fetch, or modify any candidate repository.

Run the validator from the DPRAuto repository root:

```bash
python3 evaluations/multilang/run_evaluation.py
```

`--strict-sources` is the gate for later execution milestones. It currently
fails by design because four CXXCrafter repositories have not been fetched or
pinned. `--output PATH` writes the deterministic readiness report only when an
output path is explicitly provided.

## Test project directories

The 17 local snapshots below are ready for later evaluation:

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

The four reserved CXXCrafter source directories are deliberately absent in M0:

| Language | Repository | Dataset row | Reserved directory |
|---|---|---|---|
| C | rui314/8cc | `top100_dataset.csv` | `/home/master/auto-build/CXXCrafter/datasets/top100-src/8cc` |
| C++ | rui314/mold | `top100_dataset.csv` | `/home/master/auto-build/CXXCrafter/datasets/top100-src/mold` |
| C++ | google/leveldb | `top100_dataset.csv` | `/home/master/auto-build/CXXCrafter/datasets/top100-src/leveldb` |
| C++ | simdjson/simdjson | `top100_dataset.csv` | `/home/master/auto-build/CXXCrafter/datasets/top100-src/simdjson` |

Dataset catalogs are recorded as absolute, auditable paths in `manifest.json`.
Each ready source has a pinned dataset revision. The CXXCrafter CSV does not
provide commit revisions, so these cases remain `fetch_required` and their
ground truth remains provisional until a later explicit fetch-and-pin step.

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

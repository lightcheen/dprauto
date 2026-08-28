# M11 command-semantics firewall and stage separation

Date: 2026-08-28

M11 fixes the false build, test, CLI, and web goals exposed by the M10 fixed-21 run. The work is
deterministic and uses no Agent or LLM. It concentrates on five pinned cases whose failures made a
command-role defect directly observable.

## Exact validation sources

| Case | Source directory |
|---|---|
| dagshub/client | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/envbench-python-paper-dagshub-client-f8d89c53c733` |
| compserv/hknweb | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/envbench-python-paper-compserv-hknweb-422acacc4b1a` |
| tiangolo/fastapi | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/installamatic-tiangolo-fastapi-212fd5e` |
| ccache/ccache | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/executionagent-ccache-ccache-7f3e822efb1b` |
| distcc/distcc | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/executionagent-distcc-distcc-a627b26f08cd` |

The final same-implementation records are stored at:

`/home/master/dprauto/evaluations/multilang/runs/m11-command-semantics-20260828`

Their implementation SHA-256 is
`5acccd053df88d02d863152df31218e1af740afc285f15440b4331b92b1298e0`.

The final reproduction command is:

```bash
PYTHONPATH=src python3 evaluations/multilang/run_execution.py \
  --output evaluations/multilang/runs/m11-command-semantics-20260828 \
  --indices 2,4,7,13,17 \
  --docker-network host \
  --native-base-image detect-penetration-repair-ai-system-api:latest \
  --maven-base-image 'maven:3.9-eclipse-temurin-{version}' \
  --gradle-base-image 'maven:3.9-eclipse-temurin-{version}'
```

## Implemented semantic contract

### Shared run-role selection

Build templates and Runnability now use one role-aware selector. A Web command must contain
positive evidence of a persistent server such as `runserver`, Uvicorn, Gunicorn, Flask run,
Streamlit, Compose up, or a JVM server task. Finite validation commands such as
`python manage.py check` cannot be used as Web entrypoints. Django, Flask, Uvicorn and Hypercorn
commands are normalized to bind `0.0.0.0` when necessary.

For Django Web applications, an exact repository-evidenced `manage.py migrate` setup command is
prefixed with `--noinput` before `runserver`. The selector does not invent migrations when that
command is absent from the parsed repository evidence.

CLI verification retries an evidence-backed `--help` form when a bare CLI prints usage but exits
non-zero, and records whether the fallback followed an initial failure or empty output. This makes
the Dagshub result explicit rather than silently accepting any non-zero command.

### Application-versus-library evidence

Python project typing no longer treats these as application entrypoints:

- Web-looking files under tests, documentation, or examples;
- `tests/main.py` and similar test/docs/example script names;
- console scripts whose implementation catches `ImportError` and explicitly requires an optional
  extra;
- `python -m package` shims that only forward to such an optional console script.

FastAPI therefore becomes a Library for the installed core package instead of a fake Web
application or optional CLI.

### Build/test stage separation

Native CMake image builds no longer execute targets named `check`, `tests`, or `all_tests` as a
Buildability criterion. Test compilation may still occur as part of the default build graph, but
ordinary CTest execution happens only in Testability. ccache now builds successfully and its
`remote_http` failure is correctly reported by the Testability layer.

### Autotools pipeline completion

When `autogen.sh` exists, the generated build checks for a resulting Makefile and runs
`./configure` when needed before `make`. The bounded Autotools dependency scanner also maps direct
`PKG_CHECK_MODULES(POPT, ...)` evidence to `libpopt-dev`. distcc now completes configure and build,
then runs the real `./distcc --version` probe.

### Web probe networking

Docker discards published ports under host-network mode. Web verification now uses an isolated
bridge/published random port for that mode while preserving explicitly named networks. This removes
the structural Hknweb false failure without reserving a fixed host port. Local HTTP probing bypasses
environment proxies, retries transient connection resets within the startup deadline, and accepts
only reachable HTTP statuses below 500. A proxy response or an application server error therefore
cannot satisfy Runnability.

## Results

| Metric on the five cases | M10 | M11 |
|---|---:|---:|
| Standard build success | 3/5 | 5/5 |
| Installability passed | 3/5 | 5/5 |
| Runnability passed | 0/5 | 4/5 |
| Strict build/install/test/run success | 0/5 | 2/5 |
| Observed total case time | 770.7 s | 398.7 s |

The timing comparison is observational because Docker caches were warm in M11. The status and
layer transitions, not the elapsed-time reduction, are the authoritative result.

| Repository | M10 outcome | M11 outcome | Decisive M11 evidence |
|---|---|---|---|
| dagshub/client | Runnability failed | Strict success | Minimal closure still passes 108 tests; bare `dagshub` exits 2, then `dagshub --help` exits 0 with usage output. |
| compserv/hknweb | Runnability failed | Strict success | `manage.py test` passes 134 tests; repository-evidenced migrations run before the normalized server, which remains alive and returns HTTP 200. |
| tiangolo/fastapi | Testability + fake Web run failed | Testability + Library import failed | No tests/docs script or optional CLI is executed. The library probe now exposes that the built image lacks core `starlette`; the selected tests independently retain the multipart warnings-as-errors conflict. |
| ccache/ccache | Build failed | Build and run pass; Testability fails | Build contains no test target; real `build/ccache --version` passes. Independent CTest reports only `test.remote_http` failed because its local server was unreachable. |
| distcc/distcc | Build failed | Build and run pass; Testability fails | Autogen/configure/make and `./distcc --version` pass. `make check` fails after distccd drops root to `nobody` and cannot write its root-owned test directory. |

The final M11 aggregate is 5/5 builds, 2/5 strict successes, and zero Agent/LLM calls. The three
remaining failures are no longer false command stages:

- FastAPI: core installation and compatible selected-test dependency contract;
- ccache: local HTTP test-process orchestration;
- distcc: non-root test user and multi-process directory ownership.

## Next step

M12 should repair the four systematic JVM environment failures from M10: exclude benchmark-only
sentinel files from generated contexts, isolate Maven Wrapper configuration from the base image,
prefetch/cache Gradle Wrapper distributions with bounded timeout evidence, and add deterministic
failure classification for Apache RAT, Maven lifecycle, and Gradle download failures. After that,
the fixed 21 corpus should be rerun under one new implementation identity before expanding the
Agent repair space.

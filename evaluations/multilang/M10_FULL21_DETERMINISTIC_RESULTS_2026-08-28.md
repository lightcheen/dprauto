# M10 fixed-21 deterministic multilingual execution

Date: 2026-08-28

M10 is the first execution of the complete pinned multilingual corpus through the production
parser, deterministic build portfolio, and layered verifier. It does not construct an Agent or
an LLM client. Every record is bound to the source revision, corpus files, production
implementation digest, and execution policy, so results from an older implementation cannot be
silently reused.

## Reproduction contract

The complete run is stored at:

`/home/master/dprauto/evaluations/multilang/runs/m10-deterministic-full21-20260828`

Its implementation SHA-256 is
`7dd9c377f8e0010b4166a91177907b0f1c8a521d5a766b5b39e9f2a5d7fb0a1e`.
The source workspaces were validated against their pinned Git revisions before execution and were
treated as read-only. Completed case records were written atomically and reused only when the
full evaluation identity matched.

The three resumable batches used the same policy:

```bash
PYTHONPATH=src python3 evaluations/multilang/run_execution.py \
  --output evaluations/multilang/runs/m10-deterministic-full21-20260828 \
  --indices 13-21 \
  --docker-network host \
  --native-base-image detect-penetration-repair-ai-system-api:latest \
  --maven-base-image 'maven:3.9-eclipse-temurin-{version}' \
  --gradle-base-image 'maven:3.9-eclipse-temurin-{version}'
```

The other batches changed only `--indices` to `9-12` and `1-8`. The per-build timeout was 3,600
seconds, each verification command was bounded to 300 seconds, each case shared a 4,800-second
deadline, native build parallelism was four, and at most eight Python test files were selected for
a dependency-closed slice.

## Exact test project directories

The first 17 cases come from the CNB benchmark material under the other projects. The final four
are pinned CXXCrafter rows. These are the exact source directories used by M10:

| # | Language | Repository | Dataset | Source directory |
|---:|---|---|---|---|
| 1 | Python | yubico/yubikey-manager | EnvBench-Python | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/envbench-python-paper-yubico-yubikey-manager-fbdae2bc12ba` |
| 2 | Python | dagshub/client | EnvBench-Python | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/envbench-python-paper-dagshub-client-f8d89c53c733` |
| 3 | Python | piccolo-orm/piccolo | EnvBench-Python | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/envbench-python-paper-piccolo-orm-piccolo-17c0a8859c19` |
| 4 | Python | compserv/hknweb | EnvBench-Python | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/envbench-python-paper-compserv-hknweb-422acacc4b1a` |
| 5 | Python | gamesdonequick/donation-tracker | EnvBench-Python | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/envbench-python-paper-gamesdonequick-donation-tracker-63411a9fd9d8` |
| 6 | Python | tmux-python/tmuxp | EnvBench-Python | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/envbench-python-paper-tmux-python-tmuxp-3e0fec3596cc` |
| 7 | Python | tiangolo/fastapi | Installamatic | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/installamatic-tiangolo-fastapi-212fd5e` |
| 8 | Python | django/django | ExecutionAgent | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/executionagent-django-django-e95468ed97b1` |
| 9 | Java | apache/commons-csv | ExecutionAgent | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/executionagent-apache-commons-csv-2d44689ec75e` |
| 10 | Java | mybatis/mybatis-3 | ExecutionAgent | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/executionagent-mybatis-mybatis-3-58e2d5e9b035` |
| 11 | Java | ReactiveX/RxJava | ExecutionAgent | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/executionagent-reactivex-rxjava-6b28009fd085` |
| 12 | Java | spring-projects/spring-security | ExecutionAgent | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/executionagent-spring-projects-spring-security-0d5f42f8529c` |
| 13 | C++ | ccache/ccache | ExecutionAgent | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/executionagent-ccache-ccache-7f3e822efb1b` |
| 14 | C | json-c/json-c | ExecutionAgent | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/executionagent-json-c-json-c-a1249bfda0f6` |
| 15 | C++ | nlohmann/json | ExecutionAgent | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/executionagent-nlohmann-json-55f93686c015` |
| 16 | C | libevent/libevent | ExecutionAgent | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/executionagent-libevent-libevent-a994a52d5373` |
| 17 | C | distcc/distcc | ExecutionAgent | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/executionagent-distcc-distcc-a627b26f08cd` |
| 18 | C | rui314/8cc | CXXCrafter `top100_dataset.csv` | `/home/master/auto-build/CXXCrafter/datasets/top100-src/8cc` |
| 19 | C++ | rui314/mold | CXXCrafter `top100_dataset.csv` | `/home/master/auto-build/CXXCrafter/datasets/top100-src/mold` |
| 20 | C++ | google/leveldb | CXXCrafter `top100_dataset.csv` | `/home/master/auto-build/CXXCrafter/datasets/top100-src/leveldb` |
| 21 | C++ | simdjson/simdjson | CXXCrafter `top100_dataset.csv` | `/home/master/auto-build/CXXCrafter/datasets/top100-src/simdjson` |

## Aggregate result

| Metric | Result |
|---|---:|
| Complete records | 21/21 |
| Standard build success | 15/21 (71.4%) |
| Installability passed | 15/21 (71.4%) |
| Testability passed | 7/21 (33.3%) |
| Runnability passed | 12/21 (57.1%) |
| Environment success | 5/21 (23.8%) |
| Strict build + install + ordinary test + run success | 5/21 (23.8%) |
| Total case time | 3,491.4 seconds |
| P50 / P95 / maximum | 93.7 / 515.6 / 619.1 seconds |
| Agent / LLM calls | 0 / 0 |

Environment success and strict success happen to be equal in this run. They remain separate
metrics because environment verification can, by policy, accept a skipped Testability layer,
whereas strict success requires all three layers to pass.

| Language | Cases | Build | Environment | Strict |
|---|---:|---:|---:|---:|
| Python | 8 | 8 | 1 | 1 |
| Java | 4 | 0 | 0 | 0 |
| C | 4 | 3 | 1 | 1 |
| C++ | 5 | 4 | 3 | 3 |

## Per-project result

`B`, `I`, `T`, and `R` mean Build, Installability, Testability, and Runnability. A dash means that
verification did not run because the build failed.

| # | Repository | B | I | T | R | Seconds | Decisive evidence |
|---:|---|---|---|---|---|---:|---|
| 1 | yubico/yubikey-manager | pass | pass | pass | pass | 115.0 | Ordinary bounded pytest slice: 13 passed, 59 skipped; `ykman` runs. |
| 2 | dagshub/client | pass | pass | pass | fail | 37.9 | Minimal closure installed four test packages and 108 tests passed; bare `dagshub` printed help but exited 2. |
| 3 | piccolo-orm/piccolo | pass | pass | fail | pass | 16.6 | Collection requires the PostgreSQL driver through lazy `PostgresEngine`; the slice did not install `piccolo[postgres]`. |
| 4 | compserv/hknweb | pass | pass | pass | fail | 177.4 | `manage.py test` passed 134 tests; generated image command was `manage.py check`, which exits and is not a web server. |
| 5 | gamesdonequick/donation-tracker | pass | pass | fail | pass | 22.3 | Repository `runtests.py` immediately failed because system executable `yarn` was absent. |
| 6 | tmux-python/tmuxp | pass | pass | fail | pass | 112.5 | `tmux` was present; 91 passed, 2 skipped, and one real tmux window test failed. |
| 7 | tiangolo/fastapi | pass | pass | fail | fail | 93.7 | Eight selected files failed collection on incompatible `multipart` warning policy; bare `fastapi` was also a false application entrypoint and was not installed in the production group. |
| 8 | django/django | pass | pass | fail | pass | 407.2 | Full runner exceeded 300 seconds; output also showed root-only permission semantics and missing `tblib` for parallel tracebacks. |
| 9 | apache/commons-csv | fail | - | - | - | 103.8 | Apache RAT rejected dataset sentinel `/.cnb-benchmark-source-ready` as an unapproved file. |
| 10 | mybatis/mybatis-3 | fail | - | - | - | 78.7 | Maven Wrapper treated inherited `/root/.m2` as an unknown lifecycle phase. |
| 11 | ReactiveX/RxJava | fail | - | - | - | 18.3 | Gradle Wrapper download of 8.14 exceeded its fixed 10,000 ms timeout. |
| 12 | spring-projects/spring-security | fail | - | - | - | 21.6 | The same Gradle 8.14 Wrapper download timed out at 10,000 ms. |
| 13 | ccache/ccache | fail | - | - | - | 429.7 | 49/50 tests passed and `remote_http` failed, but the `check` target was embedded in the image build and misreported as a build failure. |
| 14 | json-c/json-c | pass | pass | pass | pass | 40.5 | 25/25 CTest tests passed. |
| 15 | nlohmann/json | pass | pass | fail | pass | 619.1 | CTest reached 100/101 before the 300-second verification timeout. |
| 16 | libevent/libevent | pass | pass | fail | pass | 438.4 | CTest reached 56/84 before the 300-second verification timeout. |
| 17 | distcc/distcc | fail | - | - | - | 31.9 | Template ran `make -j4` before generating a Makefile with Autotools `configure`. |
| 18 | rui314/8cc | pass | pass | fail | pass | 11.5 | Build and real CLI run passed; ordinary `make test` requires Python 2, which is unavailable. |
| 19 | rui314/mold | pass | pass | pass | pass | 71.4 | 445 passed, 41 skipped, 0 failed. |
| 20 | google/leveldb | pass | pass | pass | pass | 128.2 | 3/3 CTest targets passed. |
| 21 | simdjson/simdjson | pass | pass | pass | pass | 515.6 | 131/131 CTest tests passed. |

## What M10 proves

The multilingual path is real rather than metadata-only. Four native repositories with very
different CMake layouts pass strict build, test, and runtime verification. Python also shows two
important partial wins: Dagshub's dependency-closed slice avoids FiftyOne/PyArrow and passes 108
tests, while Hknweb performs Django initialization and passes all 134 tests. YubiKey Manager now
uses a genuine pytest command and a genuine `ykman` entrypoint; the old CI `pip download` false
target is not selected.

The low final success rate is not primarily a lack of LLM repair. The records expose deterministic
contract defects that an LLM should not have to rediscover on each attempt.

## Remaining defect clusters

### 1. Command roles are still not enforced end to end

- ccache's `check` target is a test but is executed inside the build layer. This both enlarges the
  build search space and turns one test failure into `build_failed`.
- bare `dagshub` is a valid CLI that prints usage and exits 2, but Runnability treats it as failed
  instead of using the evidence-backed `--help` or `version` form.
- Hknweb's `manage.py check` is a finite validation command, not a long-running web entrypoint.
- FastAPI is a framework/library repository, not an application whose entrypoint is bare
  `fastapi`.
- distcc is correctly identified as a CLI and now has `./distcc --version` ground truth, but its
  Autotools build pipeline never runs `configure`, so the runtime command cannot be reached.

These are the same class of false goals exposed by the earlier YubiKey Manager `pip download`
case. Command candidates need a typed contract whose execution behavior is checked: build commands
produce artifacts, test commands execute an ordinary test runner, CLI commands produce help or
version output, and web commands must remain alive and bind a port.

### 2. JVM support exists but its environment contract is not usable yet

All four Java projects fail before verification. The failures are deterministic:

- benchmark-only sentinel files must be excluded from the Docker context seen by license checks;
- Maven Wrapper execution must neutralize the base image's incompatible `MAVEN_CONFIG` value;
- Gradle distributions need a cached/prefetched wrapper path or a policy-controlled wrapper
  timeout greater than 10 seconds.

The failure classifier also labels all four as `unknown`, despite containing recognizable Apache
RAT, Maven lifecycle, and Gradle download-timeout signatures.

### 3. The selected-test dependency and prerequisite closure remains incomplete

- Piccolo reaches a lazy PostgreSQL import from `piccolo_conf.py`, but the closure misses the
  `postgres` extra/driver and does not bind the inferred service.
- Donation Tracker's repository runner invokes `yarn`; executable analysis currently does not
  close over subprocess calls inside the selected runner.
- FastAPI installs `requirements-tests.txt`, but the resolved multipart combination conflicts with
  the repository's warnings-as-errors policy. The closure must honor the project's compatible
  lock/constraint source, not merely the broad declared file.
- Django's parallel runner needs `tblib`, while running as root invalidates a permission test. The
  environment contract needs both optional test dependencies and user semantics.

tmuxp demonstrates that executable installation itself now works: `tmux` is present and 91 tests
pass. Its remaining one-test failure is a platform/test-isolation issue rather than the old
"executable missing" defect.

### 4. Stable feedback needs a budget-aware ordinary test policy

nlohmann/json and libevent use genuine CTest commands but exceed the fixed five-minute command
budget. Django similarly runs a genuine repository runner but selects the entire suite. The next
policy must prefer an ordinary, locally stable unit-test target or bounded deterministic slice,
record why it is representative, and never substitute lint, download, docs, fuzz, release, or
device-only commands.

### 5. Failure feedback is too coarse for bounded repair

Six build failures are classified as `unknown`, including recognizable missing configure output,
Gradle network timeout, Maven lifecycle misuse, Apache RAT rejection, and a test failure hidden in
the build layer. This prevents the M7 repair-space narrowing from selecting the right deterministic
repair family before consulting an Agent.

## Next implementation order

1. Add a command-semantics firewall and stage separation: never execute `test`, `check`, or CTest
   targets as image-build success criteria; validate CLI and web behavior according to their role;
   and complete Autotools configure/build ordering.
2. Repair the JVM environment contract: ignore benchmark sentinel files, isolate Maven Wrapper
   configuration, and cache/prefetch Gradle distributions with bounded retry/timeout evidence.
3. Extend prerequisite closure through lazy imports, configuration modules, and subprocess calls;
   add PostgreSQL extras/service bindings, Yarn, `tblib`, compatible constraints, and non-root test
   execution where the selected test requires it.
4. Introduce a budget-aware ordinary-test selector for large CTest/Django suites, with explicit
   stable-target evidence and no special-command substitution.
5. Add deterministic failure rules for the observed signatures, then rerun the same fixed 21 cases
   without an LLM. Only unresolved failures should enter the bounded Agent repair loop.

The raw `summary.json`, 21 records, generated plans, and command/verification logs under the M10
run directory are the audit source for every value above.

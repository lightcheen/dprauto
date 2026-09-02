# DPRAuto existing-capability baseline (2026-09-02)

## Outcome first

At revision `a31a05847e4a62cbb58c16913de05dd9a7a98fe7`, DPRAuto is a working
Python/JVM/native environment builder with bounded Agent repair, but it is not yet an
"arbitrary-language, arbitrary-layout" environment construction Agent.

On the new corpus, its production parser and deterministic Docker plan generators handle 57/60:

| Language | Parsed and planned | Parsed but not planned | Not parsed |
|---|---:|---:|---:|
| Python | 20/20 | 0 | 0 |
| Java | 20/20 | 0 | 0 |
| C++ | 17/20 | 2/20 | 1/20 |
| Total | 57/60 | 2/60 | 1/60 |

This is a static capability probe, not a claim that 57 Docker images have built or passed their
project tests.

## What exists now

- The production parser registry supports Python, Maven/Gradle JVM, and C/C++ roots. Its language
  suffix detector understands more languages for indexing, but those extra languages do not have
  production environment-building strategies.
- Build selection supports an existing repository Dockerfile, generated JVM and native
  Dockerfiles, a generated Python Dockerfile/setup script, and optional Python CNB/Pack fallback.
- The top-level LangGraph runs parse, standard build, failure classification, bounded repair,
  preflight, rebuild, layered verification, regression evaluation, and final environment diff.
- JVM planning covers Maven/Gradle and wrappers. Native planning covers CMake, Meson, Autotools,
  and Make. Python planning covers common manifests and package managers with extensive test
  command/dependency selection rules.
- Verification distinguishes installability, testability, and runnability, and includes bounded
  PostgreSQL/Redis service orchestration and system-executable checks.
- Repair is constrained to environment/build artifacts and uses typed failure classification,
  risk gates, preflight checks, bounded attempts, and regression checks.

## Current evidence

- The current full unit/integration discovery run executed 396 tests in 258.822 seconds: 390
  passed, 5 skipped, and one PostgreSQL service orchestration integration test timed out. The same
  failed test passed alone in about 23 seconds, so this is evidence of suite-level flakiness, not a
  clean full-suite result.
- The focused multilingual parser and build-strategy regression run passed 50/50 tests in 63.171
  seconds after this corpus was created.
- The older fixed-21 M10 execution reached 15/21 deterministic builds and 5/21 strict
  build/install/test/run successes. Later targeted evidence reached 4/4 JVM builds and 5/5 C++
  builds, but those targeted results must not be combined into a fictional new 21-project global
  success rate.
- This corpus probe plans all 20 Python cases, all 20 Java cases (14 Maven, 6 Gradle), and 17 C++
  cases (15 CMake, 1 Make, 1 Autotools).

## Defects exposed by the new corpus

1. Workspace discovery is root-centric. Vireo has native build entrypoints below the root and is
   rejected by every production parser.
2. Parser priority plus root markers can misclassify mixed repositories. Stockfish and OpenALPR
   contain nested native entrypoints but root-visible Python artifacts; both are classified as
   Python and therefore cannot produce a native plan.
3. The goal says "any language", while production construction supports only Python, JVM, and
   C/C++. Tree-sitter coverage is not equivalent to build support.
4. `pyproject.toml` exposes no installed CLI entrypoint. The reusable application composition
   exists in Python, but there is no simple `dprauto build <repo>` product interface yet.
5. The main README still contains older statements saying JVM/native deterministic builds are
   future work, contradicting the current implementation and newer evaluation reports.
6. The new 60-project corpus has not yet been run through full Docker build and layered
   verification. Static plan success must remain separate from executable environment success.

## Recommended next measurement

First fix or explicitly configure subdirectory workspace discovery, then run the 60 cases in three
separate stages: parse/plan, Docker build, and strict install/test/run verification. Record timeout,
infrastructure failure, project test failure, deterministic repair, Agent repair, and final image
reference separately. This preserves a meaningful denominator and identifies whether future work
improves project understanding, environment construction, or verification.

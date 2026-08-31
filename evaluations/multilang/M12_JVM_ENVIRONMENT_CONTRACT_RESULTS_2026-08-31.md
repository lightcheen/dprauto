# M12 deterministic JVM environment and stable-test contract

Date: 2026-08-31

M12 repairs the four systematic JVM failures exposed by M10 and validates the result through the
production parser, deterministic build strategy, and layered verifier. No Agent or LLM was used.
All four final records share one implementation, corpus, and execution-policy identity.

## Exact validation sources

| # | Repository | Dataset | Source directory |
|---:|---|---|---|
| 9 | apache/commons-csv | ExecutionAgent | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/executionagent-apache-commons-csv-2d44689ec75e` |
| 10 | mybatis/mybatis-3 | ExecutionAgent | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/executionagent-mybatis-mybatis-3-58e2d5e9b035` |
| 11 | ReactiveX/RxJava | ExecutionAgent | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/executionagent-reactivex-rxjava-6b28009fd085` |
| 12 | spring-projects/spring-security | ExecutionAgent | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/executionagent-spring-projects-spring-security-0d5f42f8529c` |

The final records and raw command logs are stored at:

`/home/master/dprauto/evaluations/multilang/runs/m12-jvm-environment-20260828`

Their implementation SHA-256 is
`ae296a52325c8c09c54597a0f005e21eecf114034e2bafaf97923ba5f33e5a8f`.
The final resumable reproduction command is:

```bash
PYTHONPATH=src python3 evaluations/multilang/run_execution.py \
  --output evaluations/multilang/runs/m12-jvm-environment-20260828 \
  --indices 9,10,11,12 \
  --docker-network host \
  --image-repository dprauto/multilang-m12 \
  --native-base-image detect-penetration-repair-ai-system-api:latest \
  --maven-base-image 'maven:3.9-eclipse-temurin-{version}' \
  --gradle-base-image 'maven:3.9-eclipse-temurin-{version}'
```

## Implemented contracts

### Generated-context and Maven Wrapper isolation

Generated Docker contexts now exclude `.cnb-benchmark-source-ready` as well as `.git` and
`.dprauto`, so evaluation-only metadata cannot become a false Apache RAT target. Maven Wrapper
images clear the base image's `MAVEN_CONFIG`; older repository wrappers therefore cannot expand
`/root/.m2` into a lifecycle argument.

Maven policy options are evidence-bound. `-Dlicense.skip=true` is copied only from a repository
command that already uses it. A POM-bound `git-build-hook-maven-plugin` install goal adds the
plugin's official `-Dgitbuildhook.install.skip=true` switch because generated contexts deliberately
omit `.git`. Optional CI profile variables such as MyBatis's test-containers profile are removed
only when the workflow explicitly defines them as optional.

### Gradle Wrapper network and proxy contract

Repository wrapper properties receive a 120,000 ms distribution timeout. The wrapper is prefetched
with three bounded attempts into a BuildKit cache retained across image builds. A generated launcher
translates container `HTTP_PROXY`/`HTTPS_PROXY` values into JVM proxy properties without baking
credentials into the image. Both image build and Testability use this launcher and the same retained
Gradle user home.

### Launcher JDK versus compilation toolchain

The parser now separates the JDK needed to launch Gradle from the repository's compilation
toolchain. RxJava uses the CI-evidenced Java 11 launcher while a Temurin 8 JDK is copied from a
separate image stage. Its location is persisted in the image's `gradle.properties`, so assemble and
later test containers resolve the same vendor/version contract.

### Stable ordinary JVM tests and real monorepo projects

JVM Testability keeps ordinary `mvn test` or `gradlew test` semantics. For a large Gradle suite it
builds a bounded `--tests` slice from real `src/test/java`, `src/test/kotlin`, or
`src/test/groovy` classes. Files with explicit integration, stress, network, concurrency, sleep, or
latch evidence are excluded from the stable candidate set. The selected paths, class names, source
command, and reason are recorded in the result.

When a build script changes behavior under `System.getenv("CI")`, the test environment receives
`CI=true`; this avoids treating a container as an unconstrained developer workstation. JVM tests
have a separate configurable 900-second ceiling, while the general command timeout remains 300
seconds.

Gradle subproject tasks use a directory-to-project mapping derived from actual build files. Spring
Security's `access/` directory is therefore executed as `:spring-security-access:test`, not the
nonexistent `:access:test`. This closes the distinction between a monorepo directory, its build
file, and its real Gradle project name.

### Deterministic failure feedback

The rule classifier now recognizes the observed benchmark-sentinel policy failure, Maven Wrapper
configuration leak, Maven license/Git-hook metadata requirements, Gradle wrapper download timeout,
missing JVM toolchain, and too-old build-launcher JVM. Network wrapper failures are retryable and
infrastructure-related; policy, toolchain, and runtime-version failures remain project-environment
failures. BuildKit stack tails do not replace the causal line.

## Final same-identity results

| Metric | M10 JVM | M12 JVM |
|---|---:|---:|
| Standard build success | 0/4 | 4/4 |
| Installability passed | 0/4 | 4/4 |
| Testability passed | 0/4 | 4/4 |
| Runnability passed | 0/4 | 4/4 |
| Strict build/install/test/run success | 0/4 | 4/4 |
| Agent / LLM calls | 0 / 0 | 0 / 0 |

| Repository | Build command | Ordinary test evidence | Artifact evidence | Seconds |
|---|---|---|---|---:|
| apache/commons-csv | `mvn -B -DskipTests package` | `mvn -B test`: 923 run, 0 failures, 0 errors, 11 skipped | 20 classes | 204.2 |
| mybatis/mybatis-3 | `./mvnw -B -DskipTests -Dlicense.skip=true -Dgitbuildhook.install.skip=true package` | Repository CI test command: 1,991 run, 0 failures, 0 errors, 19 skipped | 1,077 classes | 422.7 |
| ReactiveX/RxJava | Java 11 Gradle launcher plus Temurin 8 toolchain; `assemble` | Eight recorded stable repository test classes through standard `gradlew test --tests` | 1,759 classes | 285.5 |
| spring-projects/spring-security | Java 17 Gradle Wrapper `assemble` | Eight recorded `spring-security-access` test classes through `:spring-security-access:test --tests` | 108 classes | 375.8 |

The four observed case times total 1,288.1 seconds; P50 is 285.5 seconds and the maximum is 422.7
seconds. Timing is observational because Docker and wrapper caches were warm during the final
same-identity rerun. Layer status and command evidence are authoritative.

## Boundaries and next step

M12 closes the fixed four-case JVM environment cluster, not every possible JVM topology. It does
not yet provision arbitrary vendor-specific JDK distributions, remote Gradle toolchain repositories,
Android SDKs, application servers, or integration-test services. Maven large-suite slicing is also
left conservative because `-Dtest` behavior differs across multi-module Surefire configurations;
the two Maven corpus suites pass their ordinary full commands within the JVM budget.

The next full-corpus step should retain this implementation identity's behavior and return to the
remaining Python/native prerequisite failures: compatible locked test dependencies, subprocess
executables, non-root ownership, and local multi-process service orchestration. Those deterministic
contracts should be exhausted before widening the Agent/LLM repair space.

# M4 JVM 与 C/C++ 确定性构建及验证结果

日期：2026-08-25（Asia/Shanghai）

## 阶段结论

M4 将 DPRAuto 的确定性单容器路径从 Python 扩展到 Java、Kotlin、Groovy、C 和 C++。
新增策略不会执行 README 或 CI 中任意抽取出的 build 命令，而是只根据 M1 解析器确认的根构建
系统构造 Maven、Gradle、CMake、Meson、Autotools 或 Make 流水线。项目自带 Dockerfile 仍有
最高优先级；之后依次尝试 JVM/Native/Python 模板和可用的 CNB fallback。

本阶段对 9 个来自其他项目数据集的本地 Java/C/C++ 快照全部完成真实解析、计划生成、依赖
契约和普通测试命令选择检查。json-c 进一步完成了真实容器构建及三层验证。Apache Commons
CSV 的真实 Maven 构建受当前环境 DNS 阻塞，不能计为项目成功，也没有交给 LLM 猜测修复。

## 新增确定性能力

- Maven 使用 wrapper（存在时）或固定 Maven 工具镜像，执行批处理 package；Gradle 使用 wrapper
  或固定 Gradle 工具镜像，执行 `--no-daemon assemble`。
- JVM 解析分别记录编译目标版本与构建 JDK。MyBatis 的目标版本为 11，但 POM/CI 构建下限为
  17，因此容器选择 JDK 17；Commons CSV、RxJava、Spring Security 分别选择 8、11、17。
- JVM 依赖缓存保留在最终构建镜像层中，Testability 不需要重建另一套 Maven/Gradle 环境。
- Native 使用最多 2 个并行编译 job；系统包仅来自固定工具链和解析器白名单证据。
- CMake 根据配置名称开启普通测试：ccache 得到 `ENABLE_TESTING=ON` 和 `DEPS=DOWNLOAD`，
  nlohmann/json 得到 `JSON_BuildTests=ON`，libevent 仅开启 tests/regress，不会误开 coverage、
  benchmark 或平台专用检查。
- 固定基础镜像已具有完整白名单工具链时可跳过 apt；否则执行有重试和超时的最小 apt 安装。
  两条分支都写入 BuildPlan，不属于隐式环境变更。
- Installability 使用语言无关的 `dependency_installation_commands` 计划契约；Runnability 对 JVM
  检查非 sources/javadoc JAR 和 class 数，对 Native 检查编译产物并结合项目测试结果。
- Docker Testability/Runnability 现在把安全相对 `CommandSpec.cwd` 映射为
  `/workspace/<cwd>`，monorepo 命令不再总是在仓库根执行。
- 测试选择器优先普通 Maven/Gradle/CTest/Meson 命令，拒绝未解析的 `$VAR`、`${VAR}`、
  GitHub `${{ ... }}` 和 Windows `%VAR%`。ccache 的 Windows MSVC pytest 命令因此不再覆盖 CTest。

## 真实测试项目目录

以下 9 个 ready 快照来自 `/home/master/auto-build` 中 HerAgent/CNB 使用的 ExecutionAgent 数据集；
CXXCrafter 的 4 个预留源码仍未获取，M4 没有伪造其执行结果。

| 项目 | 语言/系统 | 测试项目目录 |
|---|---|---|
| apache/commons-csv | Java / Maven | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/executionagent-apache-commons-csv-2d44689ec75e` |
| mybatis/mybatis-3 | Java / Maven | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/executionagent-mybatis-mybatis-3-58e2d5e9b035` |
| ReactiveX/RxJava | Java / Gradle | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/executionagent-reactivex-rxjava-6b28009fd085` |
| spring-projects/spring-security | Java / Gradle | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/executionagent-spring-projects-spring-security-0d5f42f8529c` |
| ccache/ccache | C++ / CMake | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/executionagent-ccache-ccache-7f3e822efb1b` |
| json-c/json-c | C / CMake | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/executionagent-json-c-json-c-a1249bfda0f6` |
| nlohmann/json | C++ / CMake | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/executionagent-nlohmann-json-55f93686c015` |
| libevent/libevent | C / CMake | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/executionagent-libevent-libevent-a994a52d5373` |
| distcc/distcc | C / Autotools | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/executionagent-distcc-distcc-a627b26f08cd` |

## 9 项真实计划检查

| case | 构建环境 | 确定性构建命令 | 选定 Testability 命令 |
|---|---|---|---|
| java-commons-csv | Maven / JDK 8 | `mvn -B -DskipTests package` | `mvn -B test` |
| java-mybatis | Maven wrapper / JDK 17，target 11 | `./mvnw -B -DskipTests package` | `./mvnw -B test` |
| java-rxjava | Gradle wrapper / JDK 11 | `./gradlew --no-daemon assemble` | `./gradlew test` |
| java-spring-security | Gradle wrapper / JDK 17 | `./gradlew --no-daemon assemble` | `./gradlew test` |
| cpp-ccache | CMake | `cmake ... -DENABLE_TESTING=ON -DDEPS=DOWNLOAD` | `ctest --test-dir build --output-on-failure` |
| c-json-c | CMake | `cmake -S . -B build` | `ctest --test-dir build --output-on-failure` |
| cpp-nlohmann-json | CMake | `cmake ... -DJSON_BuildTests=ON` | `ctest --test-dir build --output-on-failure` |
| c-libevent | CMake | `cmake ... -DEVENT__DISABLE_TESTS=OFF -DEVENT__DISABLE_REGRESS=OFF` | `ctest --test-dir build --output-on-failure` |
| c-distcc | Autotools | `./autogen.sh`、`make -j2` | `make check` |

上述 9 项均生成 `jvm-template` 或 `native-template` BuildPlan，依赖安装命令、来源、运行探针和
镜像引用均进入稳定 plan ID。所有选定测试命令都是普通项目测试入口；没有 download、lint、
format、fuzz、docs、release 或未解析平台变量命令。

## 真实容器结果

### json-c：完整成功

- 数据目录：上表 `c-json-c` 目录；
- 构建基础镜像覆盖：本地固定镜像
  `detect-penetration-repair-ai-system-api:latest`，执行时镜像 ID 为
  `sha256:5feb42f9fa5739b2102d356ec154655e5c35c794fa39b231eff6ada89a9dce52`；
- 构建：`cmake -S . -B build`、`cmake --build build --parallel 2`，退出码 0；
- Installability：passed；
- Testability：`ctest --test-dir build --output-on-failure`，passed；
- Runnability：Native artifact probe，passed，观察到 38 个可执行/库产物；
- 聚合结果：passed。

这里的基础镜像覆盖用于隔离当前 Docker Hub/Debian 网络故障；Native 策略本身仍使用同一份
确定性 Dockerfile 契约，且显式清空了继承镜像的 ENTRYPOINT。

### Apache Commons CSV：基础设施阻塞，不计成功

默认 `maven:3.9.9-eclipse-temurin-8` 首次拉取无进展，独立
`docker manifest inspect` 在 20 秒后超时。改用已有的
`maven:3.9-eclipse-temurin-8`（镜像 ID
`sha256:e9c49db31b1853c19ccc62d9d1aca0174aa98bcb78e0127a62b34099cde43246`）后，Maven 正常
启动，但解析 `org.apache.commons:commons-parent:pom:93` 时失败：
`repo.maven.apache.org: Temporary failure in name resolution`。

因此本次只能证明 Java 计划、固定 JDK、Maven 入口和失败分类边界，不能声称 Java 真实构建已
成功。恢复容器 DNS 后应重跑 4 个 Java 快照的 build/test/runnability gate。

## 自动化验证

- M4 聚焦测试：91 passed；
- 系统 Python 全仓库：323 passed、5 skipped；跳过项是未安装 Tree-sitter/Neo4j runtime；
- 独立临时依赖环境 `/tmp/dprauto-m4-deps.lu72L1`：323 passed、0 skipped；
- 两次全量回归仅出现既有 OpenLane fixture 的 1 条 `DeprecationWarning`；
- `git diff --check` 通过；未修改 `myapi.json`。

## 当前边界与下一阶段

M4 解决单容器 JVM/Native package 的确定性构建和验证契约，但没有完成 PostgreSQL、Redis、
Django 初始化、Docker Compose、多进程 fixture、设备/GPU 等服务编排。这些属于下一阶段的
服务/框架前置条件计划，不应把 M4 的 json-c 成功外推为复杂服务项目已经解决。

# DPRAuto 当前对话完整操作与结果交接总结

更新时间：2026-08-31（Asia/Shanghai）

## 1. 文档目的和继承资料

本文交接本次连续对话中对 `/home/master/dprauto` 完成的项目审计、参考项目对比、M1–M12
实施、真实数据集验证、测试和 Git 状态。

本次工作阅读并继承了：

- `/home/master/dprauto/HANDOFF_SUMMARY_2026-08-13.md`
- `/home/master/dprauto/CONVERSATION_PROGRESS_SUMMARY_2026-08-18.md`
- `/home/master/dprauto/CONVERSATION_HANDOFF_SUMMARY_2026-08-24.md`
- `/home/master/dprauto/CONVERSATION_HANDOFF_SUMMARY_2026-08-25.md`

同时检查了 DPRAuto 生产代码，并对比 `/home/master/auto-build` 下三个参考项目/数据来源：

- CNB：真实 benchmark、固定源码快照、确定性容器构建及 ExecutionAgent/EnvBench 数据；
- CXXCrafter：C/C++ 构建搜索空间、编译反馈及原生项目数据集；
- HerAgent：Tree-sitter、多语言解析、知识图谱、语义/多轮检索和通用环境脚本。

没有读取或修改 `myapi.json`，没有 push 到远端。

## 2. 用户目标和初始缺陷

用户要求 DPRAuto 继承上述项目优点，不能局限于 Python 单容器 package，至少稳定支持 Python、
Java、C 和 C++，并使用参考项目中的真实数据集。

初始对比确认的主要缺陷：

1. 缺少 Tree-sitter AST、全仓库图、代码/配置/文档语义检索和多轮上下文；
2. Python 以外的解析、构建、测试和运行契约不完整；
3. 测试 import、Django settings/init、CI 安装步骤和所选测试缺少因果闭包；
4. build/test/run 命令易混淆，YubiKey Manager 曾把 `pip download` 当运行目标；
5. monorepo 目录、真实子项目名和工作目录不能可靠对应；
6. PostgreSQL、Redis、tmux、migration、fixture、多进程等前置条件缺少编排；
7. Testability 会安装过宽 dev requirements，缺少最小测试依赖闭包；
8. Agent 搜索空间过宽、反馈轮次不足，修改缺少严格证据和 CAS；
9. Maven/Gradle Wrapper、JDK toolchain、许可证/VCS 元数据没有可复用合同；
10. 大型普通测试套件固定 300 秒且无稳定代表切片。

据此按 M1–M12 顺序实施：先扩展仓库理解和多语言确定性能力，再补服务、测试依赖、Agent
反馈与写入安全，最后用固定真实语料暴露并修复长尾。

## 3. M1–M3：多语言仓库理解

### M1 多语言项目画像

提交：`c536e3b feat: add multilingual project intelligence`

- 建立优先级 parser registry；
- Python、Maven/Gradle JVM、CMake/Meson/Autotools/Make 统一输出 `ProjectProfile`；
- 提取语言、构建系统、依赖、wrapper、模块、工作目录和 typed commands；
- 命令候选有界，避免 CI 大矩阵压过常用本地命令。

真实验证：当时 17 个就绪语料全部正确路由，Python 8/8、JVM 4/4、C/C++ 5/5。

### M2 Tree-sitter 和仓库知识图谱

提交：`bcc696e feat: add Tree-sitter repository knowledge graph`

- 增加十余种语言（生产 adapter 为 16 种）的 Tree-sitter 解析；
- 保存目录、文件、AST named node、声明、import、配置和文档节点；
- 提供内存图和事务化 Neo4j adapter；
- 支持 schema/index、作用域替换和图邻居查询。

### M3 语义检索与多轮上下文

提交：`355b71c feat: add semantic repository context retrieval`

- 代码感知稀疏向量、字面证据、图邻居传播、过滤和路径多样化；
- 可替换 `SemanticEncoder` port；
- 有界多轮 session 和 Agent 只读 `query_repository_context` 工具；
- 同时检索代码、配置和文档。

Sybil import、Django settings、CI 安装步骤和工作目录等五个真实探针目标均为 rank 1。当前
边界是默认语义层仍为离线实现，尚未使用 Neo4j vector index。

## 4. M4–M6：JVM/Native、服务和测试依赖

### M4 JVM 与 Native 确定性构建

提交：`b0cd143 feat: add deterministic JVM and native builds`

- Maven/Gradle、CMake/Meson/Autotools/Make 容器策略；
- Java toolchain、wrapper、native 标准和子项目解析；
- 编译产物探针，library/CLI 分离；
- 普通 JVM/CTest 命令进入 Testability，而非 smoke 代替测试。

九个 Java/C/C++ 项目均能生成确定性计划；json-c 完成真实 build、Installability、CTest 和
Runnability。

### M5 服务、框架和 executable 前置条件

提交：`7c58cb7 feat: orchestrate test service prerequisites`

- PostgreSQL/Redis 隔离服务、健康检查、网络 alias 和清理；
- Django settings、仓库 runner 和 migration/loaddata/check 白名单初始化；
- `tmux` 等系统 executable 合同；
- 仅在所选测试有明确绑定时启动前置条件。

真实 Docker 服务探针通过；当时全仓库回归 335 项通过、5 项跳过。

### M6 所选测试的最小依赖闭包

提交：`57e609b feat: minimize selected test dependencies`

- 从选中 pytest 文件闭包到 import、本地模块、祖先 `conftest.py`、fixture 和 pytest 配置；
- 将宽 dev/qa requirements 切成所选测试需要的声明约束；
- 排除 docs、lint、FiftyOne、PyArrow 等无关依赖；
- 对非 pytest 仓库 runner 使用保守 fallback。

Dagshub 真实环境只安装四个测试 requirements，108 项测试通过，0 LLM。

## 5. M7–M9：Agent 反馈、安全写入和 CXXCrafter

### M7 有界搜索空间与反馈

提交：`afff9c2 feat: bound repair search and feedback`

- 结构化失败先缩小修复工具集合；
- policy 拒绝、重复方法和 preflight/build/test 反馈进入同轮有界重规划；
- 仅 unresolved failure 进入 LLM；
- FastAPI 真实 Testability 为 22 passed、4 skipped。

### M8 CAS 保护构建脚本修改

提交：`ef2a41f feat: guard build script mutations with CAS`

- whole-file replacement 必须引用完整可见的 `read_file` 和源 SHA-256；
- 大文件使用精确 fragment patch；
- 所有写入最终再次 compare-and-swap；
- 候选提升校验前后 digest，过期证据拒绝写入。

### M9 CXXCrafter 原生长尾

提交：`0cc5145 feat: validate pinned CXXCrafter native projects`

新增并固定：

- `/home/master/auto-build/CXXCrafter/datasets/top100-src/8cc`
- `/home/master/auto-build/CXXCrafter/datasets/top100-src/mold`
- `/home/master/auto-build/CXXCrafter/datasets/top100-src/leveldb`
- `/home/master/auto-build/CXXCrafter/datasets/top100-src/simdjson`

完成固定 commit/submodule、root-only Dockerfile、最小 CMake test target、CTest 并行、测试驱动
shebang 闭包和 CLI/library 分离。mold、LevelDB、simdjson 严格成功；8cc 保留 Python 2 遗留
平台边界，没有跳过真实测试伪造成功。21 个语料均 ready/reviewed。

## 6. M10：固定 21 项真实全量执行

提交：`5e31805 eval: run deterministic multilingual full corpus`

运行目录：
`/home/master/dprauto/evaluations/multilang/runs/m10-deterministic-full21-20260828`

M10 用生产 parser/build/verifier 执行 21 个固定项目，增加实现、策略、源码 revision 感知的
evaluation identity 和原子 checkpoint，Agent/LLM 为 0。

| 指标 | M10 |
|---|---:|
| 完整记录 | 21/21 |
| 标准构建成功 | 15/21（71.4%） |
| Testability passed | 7/21（33.3%） |
| Runnability passed | 12/21（57.1%） |
| 严格 build/install/test/run 成功 | 5/21（23.8%） |

关键暴露：四个 Java 项目全在验证前失败；ccache test 混入 build；Hknweb 把有限检查当 server；
Dagshub/FastAPI 命令角色错误；大型套件超时；Python 依赖、用户和多进程前置条件仍不完整。

报告：`evaluations/multilang/M10_FULL21_DETERMINISTIC_RESULTS_2026-08-28.md`。

## 7. M11：命令语义防火墙

提交：`530036a feat: enforce command role semantics`

- build 与 Runnability 共用 role-aware selector；
- Web 必须持续运行并绑定端口，`manage.py check` 不能满足 Runnability；
- CLI 裸命令非零时可尝试有证据的 `--help`；
- tests/docs/examples/可选 CLI 不再把 library 误判应用；
- CMake test/check target 与 build 分离；
- Autotools 完成 autogen/configure/make；
- host-network 下 Web probe 使用可发布端口的隔离网络并拒绝代理假响应/HTTP 5xx。

五个定向项目 build 从 3/5 到 5/5，strict 从 0/5 到 2/5，Agent/LLM 为 0。

报告：`evaluations/multilang/M11_COMMAND_SEMANTICS_RESULTS_2026-08-28.md`。

## 8. M12：最终完成的 JVM 环境合同

提交：`146b8cf feat: harden jvm environment contracts`

实现 SHA-256：
`ae296a52325c8c09c54597a0f005e21eecf114034e2bafaf97923ba5f33e5a8f`

运行目录：
`/home/master/dprauto/evaluations/multilang/runs/m12-jvm-environment-20260828`

报告：
`/home/master/dprauto/evaluations/multilang/M12_JVM_ENVIRONMENT_CONTRACT_RESULTS_2026-08-31.md`

### 8.1 实施内容

1. Docker context 排除 `.cnb-benchmark-source-ready`；
2. Maven Wrapper 镜像清空基础镜像 `MAVEN_CONFIG=/root/.m2`；
3. 只在仓库有证据时加入 `-Dlicense.skip=true`；
4. POM 绑定 Git hook install 时加入官方 `-Dgitbuildhook.install.skip=true`；
5. 只移除 CI 明确定义的可选 test profile；
6. Gradle Wrapper timeout 为 120,000 ms、最多三次预取并使用 BuildKit cache；
7. proxy launcher 将运行时 proxy 转为 JVM property，不把凭据写进镜像；
8. 分开 Gradle launcher JDK 和编译 toolchain：RxJava 为 Java 11 + Temurin 8；
9. toolchain 路径持久写入镜像内 `gradle.properties`；
10. 构建脚本依赖 `System.getenv("CI")` 时给测试注入 `CI=true`；
11. JVM 测试独立 900 秒上限，一般命令仍为 300 秒；
12. 从真实 JVM test source 发现类，过滤 integration/stress/network/async/sleep/latch 风险，用
    标准 Gradle `--tests` 生成最多八类的可审计切片；
13. 从真实 build 文件建立目录→Gradle project 映射；
14. 新增 RAT、Maven Wrapper、license/Git hook、Gradle 网络、JVM toolchain/runtime 分类。

### 8.2 调试中解决的真实问题

- Commons CSV：Apache RAT 拒绝评测根目录哨兵，排除后通过；
- MyBatis：依次解决 `MAVEN_CONFIG` 生命周期参数、license Git 元数据、Git hook `.git` 需求、
  可选 test-containers profile 和 CI 测试选择；
- RxJava：解决 10 秒 wrapper timeout、JVM 不读取 proxy、Java 11 launcher + Temurin 8 toolchain、
  daemon 不继承 `GRADLE_OPTS` toolchain path；全量 923 类在容器中出现时序失败和 900 秒超时，
  最终从 739 个安全类中选八个普通类；
- Spring Security：首次冷构建约 20 分钟；测试失败实际是错误 `:access:test`，根据真实 build
  文件映射为 `:spring-security-access:test` 后成功。

### 8.3 M12 测试目录和最终结果

| 仓库 | 目录 | 最终测试/产物 | 总耗时 |
|---|---|---|---:|
| Commons CSV | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/executionagent-apache-commons-csv-2d44689ec75e` | 923 run，0 failures/errors，11 skipped；20 classes | 204.2 s |
| MyBatis | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/executionagent-mybatis-mybatis-3-58e2d5e9b035` | 1,991 run，0 failures/errors，19 skipped；1,077 classes | 422.7 s |
| RxJava | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/executionagent-reactivex-rxjava-6b28009fd085` | 8 个稳定真实测试类；1,759 classes | 285.5 s |
| Spring Security | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/executionagent-spring-projects-spring-security-0d5f42f8529c` | `spring-security-access` 8 个真实类；108 classes | 375.8 s |

M10 四个 JVM 项目 build/strict 为 0/4；M12 同一身份最终为：build 4/4、Installability 4/4、
Testability 4/4、Runnability 4/4、strict 4/4、Agent/LLM 0/0。四例总耗时 1,288.1 秒。

## 9. 最终质量门和 Git 状态

已执行：

- `PYTHONPATH=src python3 -m unittest discover -s tests`
  - 396 passed，5 skipped，0 failures/errors；
- `PYTHONPATH=src python3 evaluations/multilang/run_evaluation.py --strict-sources`
  - 21/21 source ready；
  - 21/21 ground truth reviewed；
  - 本地 Git HEAD 与 manifest revision 一致；
- `python3 -m compileall -q src tests evaluations/multilang` 通过；
- `git diff --check` 和 staged diff check 通过。

当前环境未安装 Ruff，本轮没有声称执行 Ruff。写本文前：分支 `main`，核心 HEAD 为 `146b8cf`，
M12 核心提交后工作区干净，没有 push。

## 10. 主要生产文件

- `src/dprauto/adapters/multilang/jvm.py`：version/toolchain、真实测试类、CI、hook、project 映射；
- `src/dprauto/strategies/multilang.py`：Wrapper cache/proxy、双 JDK、Docker 环境和 artifact probe；
- `src/dprauto/verification/commands.py`：命令语义、profile、Gradle 测试切片和真实 task；
- `src/dprauto/verification/testability.py`：JVM 测试预算；
- `src/dprauto/adapters/failure/rules.py`：JVM 环境故障分类；
- `src/dprauto/command_semantics.py`：smoke/help/version 全命令语义；
- `src/dprauto/config.py`：`VERIFICATION_JVM_COMMAND_TIMEOUT_SECONDS`；
- `evaluations/multilang/ground-truth/cases.json`：MyBatis/RxJava 合同；
- `evaluations/multilang/M12_JVM_ENVIRONMENT_CONTRACT_RESULTS_2026-08-31.md`：完整 M12 结果；
- `tests/test_multilang_parser.py`、`test_build_strategies.py`、`test_layered_verification.py`、
  `test_build_failure_classifier.py`、`test_config.py`：回归覆盖。

## 11. 主要提交链

```text
c536e3b feat: add multilingual project intelligence
bcc696e feat: add Tree-sitter repository knowledge graph
355b71c feat: add semantic repository context retrieval
b0cd143 feat: add deterministic JVM and native builds
7c58cb7 feat: orchestrate test service prerequisites
57e609b feat: minimize selected test dependencies
afff9c2 feat: bound repair search and feedback
ef2a41f feat: guard build script mutations with CAS
0cc5145 feat: validate pinned CXXCrafter native projects
5e31805 eval: run deterministic multilingual full corpus
530036a feat: enforce command role semantics
146b8cf feat: harden jvm environment contracts
```

## 12. 必须明确的剩余边界

1. M12 的 4/4 是四个 JVM 项目的同身份结果，**尚未在 `146b8cf` 身份下重跑全部 21 项**，
   不能直接当作全语料成功率；
2. Maven 大型多模块测试未贸然通用化 `-Dtest` 切片，两个 Maven 样本当前全量测试在预算内；
3. 尚未支持任意 vendor JDK、Android SDK、远程 toolchain repository、GPU/设备；
4. Python/native 剩余问题包括兼容 lock/constraint、subprocess executable、非 root ownership、
   本地 HTTP/多进程服务和 Python 2；
5. Neo4j adapter 已实现，但默认语义检索不是 Neo4j vector index；
6. Compose、复杂 fixture 和多容器应用初始化仍需更多真实样本；
7. 必须继续“先耗尽确定性合同，再进入有界 Agent/LLM”，不能用 LLM 掩盖系统缺陷。

## 13. 建议下一步（M13）

1. 用当前生产实现和新 evaluation identity 重跑固定 21 项；
2. 与 M10（15/21 build、5/21 strict）比较，得到正式当前全局成功率；
3. 聚类剩余失败：锁/约束、executable、服务/进程、用户权限、平台/遗留运行时；
4. 优先实现静态证据可确定的最小合同并做真实定向复测；
5. 只有确定性策略无法解决的失败才进入 M7 的 Agent 调查/修复循环；
6. 每阶段保存 identity、源码 revision、真实命令、分层结果和 LLM 调用数，独立提交；除非用户
   明确要求，否则不 push。

## 14. 常用复现命令

M12 四个 JVM 项目：

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

质量门：

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
PYTHONPATH=src python3 evaluations/multilang/run_evaluation.py --strict-sources
python3 -m compileall -q src tests evaluations/multilang
git -c safe.directory=/home/master/dprauto diff --check
```

本文件应与 M10、M11、M12 报告一起阅读。原始 run records 和日志是成功率、耗时、命令和
失败结论的最终审计来源。

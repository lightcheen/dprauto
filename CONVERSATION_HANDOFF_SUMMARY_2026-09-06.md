# DPRAuto 对话、改进与评测交接总结

更新时间：2026-09-06（Asia/Shanghai）
项目目录：`/home/master/dprauto`

## 1. 一句话结论

DPRAuto 当前已经从“按少量固定规则生成 Dockerfile 并做较弱验证”，演进为一个面向
Python、Java、C/C++ 的、证据驱动的自动环境构建原型：它能够识别根目录或嵌套构建入口，
生成并执行 Docker 构建计划，分类失败，按策略重试，在独立入口中调用有界 LLM 修复，并使用
Installability、Testability、Runnability 三层验证判断最终环境。

但是当前还不能宣称“任意项目都能自动得到可运行 Docker 环境”，也不能宣称当前代码在新增
30 项上的成功率已经达到某个新数字。最后一次完整 30 项同批结果仍是 M16 的构建 `14/30`
和严格三层成功 `5/30`；M19 只是 3 项定向修复验证，结果为 `2/3`，不能外推为 30 项成功率。

当前工作进行到原计划的第六阶段“改进三层验证”的定向验证后半段：M19 已完成，WireMock 和
Rich 的问题已由通用规则解决，Stockfish 的旧假阳性也已消除，但真实构建进一步暴露出 NNUE
外部构建资产没有执行前置获取命令的问题。因此第六阶段尚未正式收口。

## 2. 当前版本与工作区状态

- Git HEAD 已回退并保持在：`c9c629c9040034001816ea9a604774b0b9b0b94c`，提交说明为
  `dprauto升级前`。
- 当前目录不是该提交的干净原样。后续所有通用能力改进都保留为未提交工作区修改。
- 写本文前，Git 状态为 36 个已跟踪文件有修改、15 个未跟踪路径；本文又新增一个未跟踪文件。
- 已跟踪差异约为新增 5190 行、删除 391 行。不得使用 `git clean`、`git reset --hard` 或覆盖式
  checkout 清理，否则会丢失本轮成果及用户原有内容。
- M19 记录的生产实现 SHA-256 为：
  `8f5337f2f2ab0fb4bb407ddcbcf4d298ecf4ffdac74ee2979d3a74419b828560`。
- `myapi.json` 当前存在，但本文不记录任何密钥、URL 或其他敏感内容。
- 撰写本文时没有多语言评测、下载或 LLM runner 在后台运行。

这意味着“回退到 c9c629c”只描述 Git 基线；“当前 DPRAuto”应理解为：

```text
c9c629c 基线 + 当前未提交的通用改进
```

## 3. 整个对话的任务演进

本轮对话依次完成或讨论了以下工作：

1. 将仓库 HEAD 回退到 `c9c629c`，核对该版本与原 21 项多语言数据集的关系；
2. 复测原 21 项，确认 21 项 Docker 构建全部成功，但严格三层验证不是 21/21；
3. 将 21 项实际使用的 Installability、Testability、Runnability 命令整理为中文报告；
4. 解释三层验证分别测什么、为什么不同项目不会只使用同一条命令、何时允许候选命令回退；
5. 说明如何手动用 ReactiveX/RxJava 尝试环境自动构建；
6. 从 EnvBench JVM、HerAgent Python、CXXCrafter C/C++ 数据中另选 30 项，与原 21 项去重；
7. 首版候选过大后暂停下载、清理未完成下载，并重新筛选高 Star、中小规模项目；
8. 将最终 30 项下载到各自数据集原有源码目录并固定 revision；
9. 增加支持后台启动、状态查看和断点续跑的 30 项 runner，避免需要人工持续盯守；
10. 完成 M14 初始 30 项基线，分析 14 项构建成功、11 项验证失败的原因；
11. 对照 CNB、CXXCrafter、HerAgent、EnvBench 的思想，制定并逐步实施通用改进，不写仓库名特判；
12. 完成 M15、M16 多轮确定性复测和 Native 恢复测试；
13. 在第五阶段接入有界 LLM 修复 runner，先遇到 API HTTP 402，额度恢复后完成真实 LLM canary；
14. 开始第六阶段，强化三层验证的命令真实性、测试数量证据和运行语义；
15. 完成 M18 三项目 canary，发现 WireMock 零测试假通过、Rich 工具自身依赖污染、Stockfish
    任意可执行脚本被误当产物三个问题；
16. 使用通用规则修复上述问题并运行 M19；
17. M19 完成后运行当前完整代码测试套件，并生成本文作为下一轮交接入口。

2026-09-02 之前的详细历史仍保存在：

- `CONVERSATION_HANDOFF_SUMMARY_2026-09-02.md`
- `CONVERSATION_HANDOFF_SUMMARY_2026-08-31.md`
- `CONVERSATION_HANDOFF_SUMMARY_2026-08-25.md`
- `CONVERSATION_HANDOFF_SUMMARY_2026-08-24.md`
- `HANDOFF_SUMMARY_2026-08-13.md`

本文是当前最新状态，应优先阅读；旧文档用于追溯早期设计和证据，不能覆盖本文中的新结果。

## 4. 数据集与源码位置

### 4.1 原 21 项

权威清单为：

`/home/master/dprauto/evaluations/multilang/manifest.json`

其中包含 Python 8 项、Java 4 项、C 4 项、C++ 5 项。清单内的 `dataset` 和 `source.path` 是
来源与实际源码位置的权威记录。Python 项主要标记为 EnvBench Python，Java 和 Native 项中有
ExecutionAgent 数据；实际固定源码快照大多位于 CNB benchmark 对 HerAgent/ExecutionAgent 数据
的本地镜像目录。因此不能只凭父目录名称判断原始数据集。

M13 是回退到 c9c629c 后针对这 21 项的完整同批运行：

`/home/master/dprauto/evaluations/multilang/runs/m13-full21-c9c629c-20260903/`

三阶段逐项目中文命令报告为：

`/home/master/dprauto/evaluations/multilang/runs/m13-full21-c9c629c-20260903/PHASE_COMMANDS.md`

### 4.2 新增 30 项

最终清单为 Python、Java、C/C++ 各 10 项，与原 21 项无重复：

- 机器可读 manifest：
  `/home/master/dprauto/evaluations/multilang/manifest-high-star-30-20260903.json`
- 项目目录、GitHub 链接、Star、用途和 revision：
  `/home/master/dprauto/evaluations/multilang/HIGH_STAR_30_20260903.md`
- CSV：
  `/home/master/dprauto/evaluations/multilang/high-star-30-20260903.csv`

数据源和存放位置：

- Java 10 项：EnvBench，位于 `/home/master/auto-build/EnvBench-main/data/repo_data/`；
- Python 10 项：HerAgent 各 benchmark 清单，对应源码位于
  `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/`；
- C/C++ 10 项：CXXCrafter Top100，位于
  `/home/master/auto-build/CXXCrafter/datasets/top100-src/`。

最终 30 项源码均已就绪。下载阶段已经结束，没有残留下载进程；早先首版名单里已完整下载但后来
排除的项目没有被误删，只有未完成的下载被停止和清理。

当前两个 manifest 的 SHA-256：

```text
5f967606a7cdccd42d822773e9c5856d6eaf8e49ceb2a84110bbfee6a9740afb  manifest.json
67bae7f94e0a9d3e7fead47ddf6ce5d6960b2d60292541e2abe733bf05d09a2b  manifest-high-star-30-20260903.json
```

## 5. Installability、Testability、Runnability 的当前含义

三层不是所有项目执行同一条命令。系统先从项目 manifest、构建脚本、源码、CI 和 README 提取
证据，再按语言、构建系统和项目类型选择有界命令。

### 5.1 Installability

验证“构建结果是否形成健康、可使用的已安装环境”，包括：

- Docker 构建命令是否成功；
- 计划要求的依赖安装合同是否存在；
- 是否记录目标镜像；
- 镜像是否真实存在；
- Python 包依赖、JVM runtime 或 Native 动态链接等安装健康检查。

对 Poetry、PDM、Pipenv、uv 等临时保留在镜像中的构建工具，如果只有工具自身的全局依赖冲突，
而项目包本身没有冲突，当前会保留原始输出并标记为有限证据，不再把项目误判为不可安装。

### 5.2 Testability

验证“项目自己的测试是否真实执行并通过”，而不是只看测试命令退出码：

- 最多保存并尝试 3 个来源不同的候选命令；
- Python/JVM 测试默认选取最多 8 个安全、稳定的测试文件或测试类，限制成本；
- 只有命令无效、目标不存在、没有收集到测试或验证环境错误等可替换失败才尝试下一候选；
- 测试断言失败、超时、真实依赖失败不会被更简单的后备命令掩盖；
- pytest 输出和 Gradle JUnit XML 会被解析为实际测试数量；退出码为 0 但执行 0 项测试不再算通过；
- 必需硬件、密钥或付费外部服务无法安全提供时可以明确 SKIPPED，但不能伪造 PASSED。

### 5.3 Runnability

验证“环境中有与项目类型相符的运行行为”，并记录证据强度：

- Web：进程存活、端口开放、HTTP 响应；
- CLI：安全的 `--help`、`--version` 或有 CI/README 依据的有界直接调用；
- Script：退出码及可观察输出/副作用；
- Python library：模块加载及公开 API；
- JVM/Native library：真实编译产物内容，并结合项目测试提升证据强度；
- Native 可执行程序：调用真实二进制，而不是把任意带执行位的脚本当成构建产物。

Runnability 当前输出 `runtime_contract`、`runtime_outcome_category`、
`runtime_evidence_strength`、`runtime_evidence_scope`、`runtime_probe_source` 和
`runtime_semantically_proven` 等字段。旧汇总中的简单 `passed` 不能等同于新语义下的真实可运行。

## 6. 已实施的通用改进

以下是按实际落地能力归纳的六个阶段。阶段标题用于交接，不依赖某个仓库名称。

### 第一阶段：仓库理解与构建入口发现

- 对根目录和两层以内的安全子目录生成构建候选并评分；
- 结合构建清单、匹配源码数、目录风险选择主构建根；
- 避免辅助 Python 文件抢占更强的 JVM/Native 主项目证据；
- 从 CI、README、manifest 和构建脚本提取命令、工作目录和来源；
- 将证据写入稳定的 `repository_evidence` 元数据。

### 第二阶段：统一依赖合同

- 为 Python、JVM、Native 统一表达 runtime、toolchain、项目依赖、测试依赖和系统依赖；
- Native 只根据明确的 CMake/Autoconf/header/tool 证据，从安全映射表选择系统包；
- 增加最小测试依赖闭包，避免无边界安装全部开发依赖；
- 识别 VCS 构建消费者并按需保留 `.git`。

### 第三阶段：构建策略与失败恢复

- 支持项目 Dockerfile、Python/JVM/Native 模板及条件允许时的 CNB 组合；
- 可恢复的项目构建错误可进入下一兼容策略；
- 临时网络故障在相同计划内有限重试，重复 fingerprint 不无限重跑；
- 网络、Docker 基础设施、超时和项目构建错误被分开统计；
- JVM toolchain、Native 基础镜像、并发构建数和总截止时间进入显式配置。

### 第四阶段：验证命令与失败语义

- build/test/run/install 命令按用途分类，避免把安装命令当测试、把测试命令当运行；
- Testability 使用多个有界候选，并限制允许回退的失败类别；
- 增加 PostgreSQL/Redis 等有限服务编排、测试前置检查和超时策略；
- Installability、Testability、Runnability 与构建共用更细的失败分类。

### 第五阶段：有界 LLM 修复

- 新增独立的 Agent canary runner；
- 先做确定性构建，失败且满足门控条件后才进入 LLM；
- 原始源码快照只读，修复在隔离副本中执行；
- 默认最多 2 轮，限制调查动作、上下文、总时间和重复失败；
- 仅允许修改 Dockerfile、setup 脚本和 `.dprauto` 验证依赖文件，不允许业务源码修改；
- 修改后必须重新构建并通过分层验证，LLM 自己声称成功不算成功。

### 第六阶段：严格三层验证

- Installability 增加安装健康探针，并隔离构建工具自身污染；
- Testability 增加零测试拒绝、实际测试数量、候选尝试和证据强度；
- Gradle 通过 JUnit XML 汇总 `DPRAUTO_TEST_COUNT`；
- Runnability 引入 runtime contract、证据范围、强度和语义成功字段；
- Native library 只认 `.a`、`.so`、`.dylib` 等真实库产物；
- Make CLI 可从稳定变量、`main()`、目标依赖和 CI 直接调用证据识别真实二进制；
- 汇总报告可重新解释旧记录，减少历史假阳性。

所有这些规则都按生态、证据类型和安全约束实现，没有在生产代码中添加 WireMock、Rich、
Stockfish 等仓库名称特判。

## 7. 项目级评测结果与成功率

### 7.1 统计口径

- 构建成功：`docker build` 成功并记录镜像；
- 环境成功：该次评测允许的验证条件总体成功，某些版本允许 Testability 为 SKIPPED；
- 严格三层成功：Installability、Testability、Runnability 都是 PASSED；
- 语义 Runnability：不仅返回 0，还满足当前 runtime contract 的语义证据；
- 单元测试通过率与项目环境构建成功率是两种不同指标，不能互相替代。

### 7.2 历次关键运行

| 运行 | 样本与目的 | Docker 构建成功 | 环境成功 | 严格三层成功 | 说明 |
|---|---|---:|---:|---:|---|
| M13 | 原 21 项、c9c629c 回退复测 | 21/21，100% | 11/21，52.4% | 11/21，52.4% | 证明 21 项都构建过镜像，但不是都通过测试 |
| M14 | 新 30 项初始基线 | 14/30，46.7% | 3/30，10.0% | 3/30，10.0% | 16 项未构建成功，11 项构建后验证失败 |
| M15 | 第一轮通用修复 | 16/30，53.3% | 5/30，16.7% | 5/30，16.7% | 比 M14 增加 2 个构建和 2 个最终成功 |
| M16 | 30 项确定性阶段复测 | 14/30，46.7% | 5/30，16.7% | 5/30，16.7% | 实现和语义已变化，不能只取历史最好值 |
| M16 Native | 新 30 项中的 Native 10 项、Bookworm 恢复 | 6/10，60.0% | 1/10，10.0% | 0/10，0% | 10 项已跑完；summary 的 expected=30 导致 complete=false 是旧 runner 口径问题 |
| M17 | 3 个 Native 失败项的 LLM canary | 0/3 | 0/3 | 0/3 | 首轮 LLM 请求均遇到 HTTP 402，未产生修复轮次 |
| M17 单项复跑 | Drogon、额度恢复后的真实 LLM 修复 | 初始 0/1 | 0/1 | 0/1 | 一次因两轮上限停止；一次修到 build/install/test 通过但 run 失败 |
| M18 | 三层运行语义 canary | 3/3，100% | 2/3，66.7% | 1/3，33.3% | 发现三个验证假象/边界问题 |
| M19 | M18 通用修复验证 | 2/3，66.7% | 2/3，66.7% | 2/3，66.7% | WireMock、Rich 解决；Stockfish 真实构建失败 |

M13、M14、M15、M16 是不同实现身份和验证口径，不能拼接分子分母，也不能选择其中最好结果当作
当前成功率。

### 7.3 当前成功率应如何表述

对外最准确的表述是：

1. 在原 21 项固定集上，c9c629c 回退复测的 Docker 构建成功率为 `100%`，严格环境成功率为
   `52.4%`；该数据集已经被反复用于开发，不是未见集。
2. 在新增 30 项上，初始基线构建成功率为 `46.7%`、严格环境成功率为 `10.0%`；阶段性最好同批
   记录为构建 `53.3%`、严格环境 `16.7%`。
3. 最近一次完整 30 项同批记录 M16 为构建 `46.7%`、严格环境 `16.7%`。
4. 当前最终代码只做了 M19 三项定向验证：严格成功 `2/3`。这个 `66.7%` 只代表三个问题样本，
   绝不能写成 DPRAuto 的总体成功率。
5. 因为 M19 之后尚未以新语义重跑全部 30 项，所以“当前代码在 30 项上的正式成功率”目前未知。

## 8. M18 到 M19 的修复是否有效

### WireMock：有效

- M18 中 Gradle 返回 BUILD SUCCESSFUL，但旧记录没有测试数量，存在零测试假通过风险；
- M19 从 JUnit XML 汇总出 `DPRAUTO_TEST_COUNT=94`；
- 8 个选中测试类实际执行 94 项测试，Testability 为 strong；
- 编译 JAR 含 703 个 class，结合已通过的项目测试，Runnability 为 strong；
- 最终 Installability/Testability/Runnability 全部通过。

### Rich：有效

- M18 的项目安装本身正常，但全局 `pip check` 报 Poetry 1.8.5 缺少 pexpect，导致项目被误判；
- M19 保留该冲突原文，但识别为构建工具自身冲突，安装健康标记为有限证据；
- Rich 项目自己的依赖没有被忽略；
- 实际执行 83 项 pytest 并全部通过；`python -m rich --help` 运行探针通过；
- 最终三层全部通过。

### Stockfish：假阳性被修复，但项目尚未成功

- M18 的 `make` 默认目标没有生成 stockfish 二进制；
- 旧 Native 产物探针却把 `universal/patch_x86_slice.sh` 这类已有执行位脚本计为产物，形成假阳性；
- M19 正确选择嵌套 `src` 构建根、CLI 类型、`make -j4 all` 以及来自 CI 的
  `./stockfish bench 16 1 6` 运行命令；
- 新探针不再把任意可执行脚本算作 Native library 产物；
- 真正执行 `make -j4 all` 后，链接阶段报
  `file not found: nn-1a298aa575a0.nnue`，因此构建失败，没有进入三层验证；
- 仓库 Makefile 明确提供 `make net`，CI 也在构建前执行该命令。当前系统提取到了运行命令，
  但尚未把有证据的“构建前资产准备命令”纳入结构化计划；
- 失败目前被分类为 compilation，也应进一步泛化为“缺少外部构建资产/前置生成步骤”。

所以 M19 的结论不是“Stockfish 修复退化”，而是“旧成功是假的，新规则正确暴露了下一层真实
问题”。下一步仍应做通用的 pre-build artifact/asset 发现与安全执行，不能为 Stockfish 写特例。

## 9. 当前 DPRAuto 实际变成了什么

### 9.1 已具备的生产能力

当前主流程可以概括为：

```text
本地固定源码/隔离副本
→ 仓库与嵌套工作区证据扫描
→ Python/JVM/Native profile 与依赖合同
→ Dockerfile/构建计划生成
→ Docker 构建策略组合与失败分类
→（专用 Agent 入口中）有界 LLM 调查和修复
→ 重建
→ Installability / Testability / Runnability
→ 结构化 JSON、日志、镜像引用与证据强度
```

语言与生态范围：

- Python：pip、requirements、Poetry、PDM 等常见项目；
- Java：Maven、Gradle、wrapper、JDK 约束、多模块和有限测试切片；
- C/C++：CMake、Meson、Autotools、Make，带有限系统依赖推断和嵌套入口；
- 其他语言尚没有等价生产 Provider，不能称为任意语言支持。

### 9.2 Docker 交付现状

成功构建会得到一个包含源码、依赖和产物的 Docker 镜像，并记录镜像引用和默认运行合同。
评测是否保留镜像取决于 runner 参数；常规全量 runner 为节省磁盘可删除镜像，M18/M19 canary 使用
`--retain-images`。

M19 当前仍存在的两个成功镜像：

```text
dprauto/multilang-m19-phase6-fix-validation-c9c629c-20260906/wiremock-2755c2fd5088d68a9d11426c71afbcc565c3913f:beb036623f6c
sha256:41bc0ffeb393d5c9e625b3bc88ee8363c508aac4d2854a2d6f9db4301cf65ff9

dprauto/multilang-m19-phase6-fix-validation-c9c629c-20260906/rich-d0de442:6bf886a654ff
sha256:8be7893eed6a07bc91315e4918deb8cffb042b16bd249edefa38b738e463f726
```

Rich 镜像的默认命令是 `python -m rich`，属于可直接执行的 CLI/demo 环境。WireMock 在本次 profile
中按编译库处理，默认命令是 JAR/class 产物探针，不是启动 WireMock 服务。因此“有可执行 Docker
环境”在当前实现中可能表示库可加载/产物可用，不一定表示所有项目都有一个面向最终用户的服务启动
命令。Stockfish M18 的旧镜像来自假阳性路径，不应作为有效交付物使用。

### 9.3 LLM 当前何时启用

- 普通 `run_execution.py`、`high_star_30_runner.sh` 和 `phase6_canary_runner.sh` 默认不调用 LLM；
- `high_star_30_agent_runner.sh`/`run_agent_canary.py` 才走生产 Agent 工作流；
- 只有确定性构建失败、失败类别满足门控且预算允许时才调用 LLM；
- LLM 不需要人工盯着才能工作。runner 是否在后台只影响进程生命周期，不影响 LLM 调用逻辑；
- 当前只证明了 LLM 可以实际调查、修改、重建并推进 Drogon 到更后面的验证阶段，尚未取得最终
  environment repair success，不能宣称 LLM 修复成功率已经建立。

## 10. 自动化代码回归状态

2026-09-06 在 M19 完成后执行：

```bash
cd /home/master/dprauto
PYTHONPATH=src python3 -m pytest -q
```

结果：

```text
445 passed, 14 skipped, 2 warnings, 204 subtests passed in 102.61s
```

两个 warning 均为已有的 Python 正则/转义 DeprecationWarning，没有测试失败。此前 M18 修复后的
聚焦回归为 `175 passed, 80 subtests passed`；当前完整结果已覆盖并替代该局部证据。

代码回归全绿只说明实现内部契约没有被已知单元/集成测试破坏，不等于 30 个真实仓库全部能构建。

## 11. 当前停在哪一步

当前阶段状态如下：

| 阶段 | 状态 | 交接结论 |
|---|---|---|
| 1. 仓库/入口理解 | 已实施 | 支持有界嵌套入口和证据评分，仍需更完整 monorepo/provider 模型 |
| 2. 依赖合同 | 已实施第一版 | 常见生态有效，外部构建资产和更多 Native 包映射仍不足 |
| 3. 构建恢复 | 已实施第一版 | 策略组合、失败分类和重试已落地，30 项构建率仍有限 |
| 4. 验证命令语义 | 已实施 | 多候选、失败门控、服务与测试依赖已落地 |
| 5. 有界 LLM 修复 | 已接入并 canary | LLM 真实运行过，但还没有最终环境修复成功样本 |
| 6. 改进三层验证 | M19 已完成，尚未收口 | 两项问题解决，一个 Native 前置资产问题待通用修复 |

第六阶段不要求等待 30 个项目全部成功才可结束。合理的完成条件是：

- 三层成功/失败语义正确；
- 已知假阳性被消除；
- 关键合同有回归测试和至少一个真实项目证据；
- 剩余构建能力缺口被正确分类并进入后续 backlog。

当前阻碍第六阶段收口的不是“必须 30/30”，而是 Stockfish 暴露的前置资产步骤还没有通用建模，
并且当前最终代码还没有执行新的完整 30 项统一评测。

## 12. 建议的下一步

### 下一步 A：补齐通用 pre-build 资产/生成步骤

优先实现以下通用能力：

1. 从 Make/CMake、CI、README 中识别有明确依赖关系或高置信来源的 pre-build 命令；
2. 建模为独立 `pre_build_steps`，不要混入 apt/pip 依赖，也不要直接执行任意 CI shell；
3. 只允许简单、可审计、项目内声明的目标，例如 `make <target>`，禁止管道、重定向和宿主操作；
4. 检查目标是否真实存在，记录网络需求、产物文件、hash/大小以及命令来源；
5. 让缺少已声明构建资产的错误归入专门类别，而不是笼统 compilation；
6. 加单元测试后，用新的 run id 定向复测 Stockfish。

这应借鉴 HerAgent 的 CI 证据提取、CXXCrafter 的 Native 反馈闭环、CNB 的显式 build phase 思想，
但继续使用 DPRAuto 的安全命令约束。

### 下一步 B：结束第六阶段

如果新的 Stockfish canary 能做到：真实获取构建资产、生成二进制、执行 benchmark，并且现有
WireMock/Rich 不回退，就可以将第六阶段标记完成。无需等待新增 30 项全部成功。

### 下一步 C：用当前最终代码统一重跑 30 项

阶段收口后使用新的 run id 重跑全部 30 项，生成真正可比较的当前成功率。必须同时报告：

- 解析率；
- Docker 构建成功率；
- Installability；
- Testability PASSED/SKIPPED/FAILED；
- Runnability 原始通过率与语义通过率；
- 严格三层成功率；
- 语言/构建系统分组；
- 基础设施失败；
- LLM 前确定性结果和 LLM 后结果。

这 30 项已经用于调试，不能再称为真正 holdout。完成后应从 corpus60 或其他来源冻结一批未用于
规则开发的新项目，验证泛化能力。

### 下一步 D：扩大 LLM 修复评测

在确定性和三层验证口径稳定后，再选择可修复的真实构建失败做小批 Agent canary。不要让 LLM
用于掩盖真实项目测试失败，也不要在一次评测中边改生产代码边继续累计同一成功率。

## 13. 继续工作常用命令

查看已经完成的 M19：

```bash
cd /home/master/dprauto
DPRAUTO_RUN_ID=m19-phase6-fix-validation-c9c629c-20260906 \
  evaluations/multilang/phase6_canary_runner.sh status
```

查看 M19 汇总和逐项目结果：

```bash
jq . evaluations/multilang/runs/m19-phase6-fix-validation-c9c629c-20260906/summary.json
jq . evaluations/multilang/runs/m19-phase6-fix-validation-c9c629c-20260906/records/28-cpp-stockfish.json
```

修改后启动新的三项目 canary，必须更换 run id，不能复用 M19 旧记录：

```bash
cd /home/master/dprauto
DPRAUTO_RUN_ID=m20-phase6-prebuild-validation-c9c629c-20260906 \
DPRAUTO_INDICES=10,14,28 \
  evaluations/multilang/phase6_canary_runner.sh start
```

查看新 canary：

```bash
DPRAUTO_RUN_ID=m20-phase6-prebuild-validation-c9c629c-20260906 \
DPRAUTO_INDICES=10,14,28 \
  evaluations/multilang/phase6_canary_runner.sh status
```

阶段收口后启动新的完整 30 项确定性评测：

```bash
cd /home/master/dprauto
DPRAUTO_RUN_ID=m21-full30-current-c9c629c-20260906 \
DPRAUTO_INDICES=1-30 \
DPRAUTO_NATIVE_BASE_IMAGE='docker.io/library/debian:bookworm-slim@sha256:88200866dfff7ea7f5cbcb6ec7c8a701889efe6fe859fe64d6990e4b07ea4171' \
  bash evaluations/multilang/high_star_30_runner.sh start
```

查看完整 30 项进度：

```bash
DPRAUTO_RUN_ID=m21-full30-current-c9c629c-20260906 \
DPRAUTO_INDICES=1-30 \
  bash evaluations/multilang/high_star_30_runner.sh status
```

运行完整代码回归：

```bash
cd /home/master/dprauto
PYTHONPATH=src python3 -m pytest -q
```

## 14. 关键文件索引

- 最新交接：`/home/master/dprauto/CONVERSATION_HANDOFF_SUMMARY_2026-09-06.md`
- 上一份完整交接：`/home/master/dprauto/CONVERSATION_HANDOFF_SUMMARY_2026-09-02.md`
- 原 21 项 manifest：`/home/master/dprauto/evaluations/multilang/manifest.json`
- 新 30 项 manifest：
  `/home/master/dprauto/evaluations/multilang/manifest-high-star-30-20260903.json`
- 新 30 项说明：`/home/master/dprauto/evaluations/multilang/HIGH_STAR_30_20260903.md`
- 21 项中文阶段命令：
  `/home/master/dprauto/evaluations/multilang/runs/m13-full21-c9c629c-20260903/PHASE_COMMANDS.md`
- 确定性 runner：`/home/master/dprauto/evaluations/multilang/high_star_30_runner.sh`
- LLM Agent runner：`/home/master/dprauto/evaluations/multilang/high_star_30_agent_runner.sh`
- 第六阶段 canary runner：`/home/master/dprauto/evaluations/multilang/phase6_canary_runner.sh`
- M14 初始 30 项：
  `/home/master/dprauto/evaluations/multilang/runs/m14-highstar30-c9c629c-20260903/summary.json`
- M15 通用验证：
  `/home/master/dprauto/evaluations/multilang/runs/m15-generalized-verification-c9c629c-20260904/summary.json`
- M16 完整确定性结果：
  `/home/master/dprauto/evaluations/multilang/runs/m16-phase6-deterministic-c9c629c-20260904/summary.json`
- M17 LLM canary：
  `/home/master/dprauto/evaluations/multilang/runs/m17-llm-canary-c9c629c-20260906/summary.json`
- M18 三层语义 canary：
  `/home/master/dprauto/evaluations/multilang/runs/m18-phase6-runnability-canary-c9c629c-20260906/summary.json`
- M19 修复验证：
  `/home/master/dprauto/evaluations/multilang/runs/m19-phase6-fix-validation-c9c629c-20260906/summary.json`

## 15. 接手时必须避免的误读

1. HEAD 是 c9c629c，不代表当前代码等于干净 c9c629c；通用改进都还在工作区。
2. 原 21 项是 21/21 构建成功，不是 21/21 严格环境成功。
3. M19 的 2/3 不是新增 30 项的当前总体成功率。
4. Docker build 成功不等于 Testability 或 Runnability 成功。
5. 测试退出码 0 不等于真实执行了测试；必须看测试数量证据。
6. 文件具有执行位不等于它是构建生成的 Native 程序。
7. Testability SKIPPED 可以用于“环境总体可用”的宽松统计，但不属于严格三层成功。
8. LLM 已接入且真实调用过，但当前没有最终环境修复成功样本。
9. 后台 runner 不需要人工盯守；人工是否在线不会决定 LLM 是否被调用。
10. 后续修改仍必须是通用规则，不允许按 repo 名称、case id 或固定 revision 写特例。

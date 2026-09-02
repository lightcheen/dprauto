# DPRAuto 当前对话完整操作与结论交接总结

更新时间：2026-09-02（Asia/Shanghai）

## 1. 文档目的

本文总结本轮对话中围绕 `/home/master/dprauto` 完成的全部只读审计、结论澄清和后续评测方案，
方便下一次对话直接继续工作。

本轮没有启动 HerAgent `ExecutionAgent-Bench` 50 项构建或测试，没有修改 DPRAuto 生产代码，
没有修改 `myapi.json`，没有调用外部 LLM API，没有提交或 push。

## 2. 用户本轮核心问题

本轮依次讨论了以下问题：

1. 目前 Python 8 个、Java 4 个、C/C++ 8 个成功是否可能过拟合；
2. 如何使用 HerAgent 中 ExecutionAgent-Bench 的 50 个、覆盖 14 种语言的项目评测 DPRAuto，
   但先只制定方案、不直接测试；
3. DPRAuto 是否已经达到“来一个项目就自动构建，出错后调用 LLM 修复”的目标；
4. DPRAuto 是否只能完成一次 LLM 修复，修复后出现新错误能否继续修复；
5. 为什么不是所有失败都进入 LLM；
6. 为什么当前还不能称为通用环境自动构建 Agent；
7. `evaluations/multilang/run_execution.py` 在哪里关闭了 Agent/LLM，以及为什么这样做；
8. DPRAuto 当前生产工作流到底是什么，LLM 修复是否关闭；
9. HerAgent 为什么能够覆盖很多语言。

## 3. 本轮读取和检查的资料

### 3.1 DPRAuto 历史交接记录

已读取并继承以下文件：

- `/home/master/dprauto/HANDOFF_SUMMARY_2026-08-13.md`
- `/home/master/dprauto/CONVERSATION_PROGRESS_SUMMARY_2026-08-18.md`
- `/home/master/dprauto/CONVERSATION_HANDOFF_SUMMARY_2026-08-24.md`
- `/home/master/dprauto/CONVERSATION_HANDOFF_SUMMARY_2026-08-25.md`
- `/home/master/dprauto/CONVERSATION_HANDOFF_SUMMARY_2026-08-31.md`

### 3.2 DPRAuto 主要生产和评测代码

重点检查了：

- `src/dprauto/adapters/multilang/parser.py`
- `src/dprauto/adapters/multilang/detector.py`
- `src/dprauto/application/build.py`
- `src/dprauto/application/agent.py`
- `src/dprauto/agent/full_workflow.py`
- `src/dprauto/agent/workflow.py`
- `src/dprauto/config.py`
- `evaluations/multilang/run_execution.py`
- `evaluations/multilang/manifest.json`
- `evaluations/prompt12/run_evaluation.py`

### 3.3 HerAgent 和 ExecutionAgent-Bench

重点检查了：

- `/home/master/auto-build/HerAgent-main/projects/executionAgent.txt`
- `/home/master/auto-build/HerAgent-main/README.md`
- HerAgent 论文 PDF：
  `2026_arxiv_llm自动环境构建_HerAgent Rethinking the Automated Environment Deployment via Hierarchical Test Pyramid.pdf`
- `/home/master/auto-build/HerAgent-main/evaluation-results/ExecutionAgent/`
- `/home/master/auto-build/HerAgent-main/app/parser/`
- `/home/master/auto-build/HerAgent-main/app/lang_graph/`
- `/home/master/auto-build/HerAgent-main/app/container/general_container.py`
- `/home/master/auto-build/HerAgent-main/Dockerfile.fulltest`
- `/home/master/auto-build/CNB/cnb-benchmark/README.md`
- `/home/master/auto-build/CNB/cnb-benchmark/heragent-manifests/executionagent.commands.jsonl`
- `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/`

确认本地已有 ExecutionAgent-Bench 50 个项目的固定 revision 源码目录，无需正式评测前重新从网络
获取源码。

## 4. 关于现有成功结果和过拟合风险的结论

### 4.1 当前成功数字不是一次统一全量结果

历史记录明确显示：

- M10 在同一实现身份下运行固定 21 项，标准构建成功为 `15/21`，Testability 为 `7/21`，
  严格 build/install/test/run 成功为 `5/21`，Agent/LLM 调用为 0；
- Java 4/4 来自后续 M12 四个 JVM 项目的统一定向复测；
- C/C++ 的最新成功证据也包含 M10、M11 和 M9 CXXCrafter 的分阶段结果；
- 当前尚未在 M12/最新生产身份下重新统一运行全部 21 项。

因此不能把 Python、Java、C/C++ 的分阶段成功数直接拼接成新的通用成功率。

### 4.2 存在明显的样本适配风险

Java M12 是在 Commons CSV、MyBatis、RxJava、Spring Security 的具体日志上逐步形成合同，
C/C++ 合同也来自固定项目暴露的问题。这些合同很多是合理的可泛化规则，但是否真正泛化尚未通过
未见语料证明。

固定 21 项与 ExecutionAgent-Bench 50 项有 10 项直接重叠：

1. django/django
2. apache/commons-csv
3. mybatis/mybatis-3
4. ReactiveX/RxJava
5. spring-projects/spring-security
6. ccache/ccache
7. distcc/distcc
8. libevent/libevent
9. json-c/json-c
10. nlohmann/json

所以 ExecutionAgent-Bench 最终必须分别报告：

- 已见/污染集：10 项；
- 相对 DPRAuto 调试历史的项目级冷启动集：40 项；
- 全部 50 项。

最能判断泛化能力的是冷启动 40 项，而不是只看总体 50 项。

## 5. ExecutionAgent-Bench 50 项建议评测方案

本轮只制定方案，没有开始执行。

### 5.1 冻结生产身份

正式测试前固定并记录：

- Git commit 和生产实现 SHA-256；
- 评测 manifest 与 ground truth SHA-256；
- 50 个仓库的精确 commit、Git HEAD、dirty 状态、submodule/LFS 状态；
- LLM 模型顺序、温度、token、timeout 和 failover 策略；
- Docker/Base Image digest、网络、CPU、内存、磁盘和缓存策略；
- 每项目总预算、构建/验证预算和最大修复轮数。

首轮基线期间禁止根据前面项目的结果修改生产代码后继续跑后面的项目。若只修复 runner bug，
所有受影响记录必须作废并使用新 evaluation identity 重跑。

### 5.2 新建 Agent-enabled 评测入口

建议新增：

```text
evaluations/executionagent/
├── manifest.json
├── contamination.json
├── evaluation_policy.json
├── run_evaluation.py
├── hidden_oracles/
└── runs/<evaluation-identity>/
    ├── records/
    ├── artifacts/
    ├── llm-logs/
    └── summary.json
```

新 runner 应复用生产入口：

```python
create_api_environment_build_workflow(...)
```

而不是只调用 parser/builder/verifier。

每个项目使用独立、全新的可写副本；原始固定源码只读。项目级 checkpoint、结果和日志原子保存，
支持中断恢复，但 identity 不匹配时禁止复用旧记录。

### 5.3 严格隔离 HerAgent 答案

DPRAuto Agent 不得访问：

- HerAgent 的 `BashFile/`；
- HerAgent 的 `TestPyramid/`；
- `executionagent_final_project_analysis_report.txt`；
- `heragent-manifests/executionagent.commands.jsonl`；
- DPRAuto 旧 ground truth、旧运行记录和人工分析报告。

HerAgent 生成的命令如需用于可比性验证，只能由 DPRAuto 完成后运行的隐藏 evaluator 使用，不能
提前注入 Agent 上下文，否则会造成答案泄漏。

### 5.4 双重成功标准

建议同时报告：

1. Parse coverage：项目是否被正确识别；
2. Initial build：LLM 介入前的确定性构建成功；
3. Benchmark success：镜像构建成功且隐藏 benchmark 测试成功；
4. DPRAuto strict success：Installability、Testability、Runnability 全部通过；
5. Agent repair success：初始失败，经实际 LLM 修改后最终通过；
6. False success：DPRAuto 自验成功但隐藏 oracle 失败；
7. Infrastructure failure；
8. Unsupported；
9. Runner error。

不能把单纯 `docker build` 成功与 HerAgent 的 Testability/论文成功数比较。

### 5.5 执行顺序

建议批准后按以下顺序执行：

1. 使用已见 10 项验证 harness，不把 plumbing 结果当最终分数；
2. 冻结 harness 和生产实现；
3. 串行或最多两个并发执行全部 50 项 Pass@1；
4. 使用统一 90 分钟硬上限，并报告 15/30/60/90 分钟 time-to-success；
5. 基础设施失败可原样重试一次，但保留原始失败；
6. Pass@1 完成后再决定是否执行 Pass@3/Pass@5；
7. 与 HerAgent 论文比较时必须使用相同 build+test 判据和相同尝试次数。

最终报告必须按已见 10 项、冷启动 40 项、全 50 项、语言和构建系统分别统计，并报告 LLM
调用、token、耗时、p50/p95、修复转化率和失败分类。

## 6. DPRAuto 当前生产工作流

当前生产完整流程是：

```text
已准备好的本地项目 workspace
→ MultiLanguageProjectParser
→ 确定性策略组合构建
→ 构建失败分类
→ 修复资格门控
→ LLM 只读调查
→ 证据化诊断
→ 有界修复计划
→ 隔离候选工作区修改
→ policy/CAS/preflight
→ 重新构建
→ Installability/Testability/Runnability
→ regression comparison
→ 接受候选或拒绝/恢复
→ 生成最终结果和 artifacts
```

确定性策略顺序包含：

- 项目自带 Dockerfile；
- JVM template；
- Native C/C++ template；
- Python template；
- 可选 CNB fallback。

## 7. LLM 修复没有在生产系统中关闭

生产入口 `create_api_environment_build_workflow()` 会：

1. 从 `myapi.json` 创建 failover LLM client；
2. 创建 `LLMRepairPlanner`；
3. 注册调查、文件读取、上下文检索和受控修改工具；
4. 创建完整 `EnvironmentBuildWorkflow`。

因此生产系统启用了 LLM，但采用“确定性优先、可行动失败才进入 LLM”，不是每个项目开始就调用。

当前 `AgentConfig` 生产默认值为：

- `max_attempts=5`；
- `max_repeated_failures=2`；
- `max_total_seconds=7200`。

旧 `evaluations/prompt12/run_evaluation.py` 为控制评测成本，单独设置了最多 2 轮和总计 900 秒，
不能与生产默认值混淆。

## 8. DPRAuto 支持多轮修复，不是只能修一次

“只有一次真实修复成功”指历史统一评测中只有 YubiKey Manager 从初始失败经 Agent 修改后最终成功，
并不表示代码只允许一轮。

当前循环是：

```text
修复1
→ preflight
→ rebuild/verify
→ 若产生新的可修复 FailureInfo
→ evaluate 将状态恢复为 CLASSIFYING
→ 再次 analyze/plan/apply
→ 修复2
→ ...
```

循环会在以下情况停止：

- 达到最大修复轮数；
- 总时间耗尽；
- 同一 failure fingerprint 连续重复；
- 同一 failure family 长时间没有因果进展；
- 新失败不符合环境修复资格；
- 修复方案违反策略；
- 没有安全修复工具；
- 修复导致原本成功的构建超时；
- LLM/API 或工具产生不可恢复错误。

## 9. 为什么不是任意失败都调用 LLM

正确目标应是：所有失败都被分类和处理；所有具有环境修复可能且存在安全修复面的失败进入 LLM。
以下失败当前不会消耗 LLM 修复轮次：

1. Git、网络、DNS、Docker daemon 等基础设施失败；
2. 依赖仍在正常下载时达到构建超时；
3. 测试仍有明确进度时达到验证超时；
4. 生成 template 的依赖安装超时，且没有项目自有 Dockerfile/setup.sh 可安全缩小；
5. 构建成功后出现明确业务代码 assertion failure；
6. parser 阶段失败，没有产生 `ProjectProfile`；
7. failure 没有对应的安全修复工具或有界搜索空间；
8. 超出时间、轮数、重复失败和安全策略限制。

基础设施、资源预算和项目业务 bug 应由不同控制器处理，不应通过修改项目构建脚本伪装修复。

## 10. 当前还不能称为“任意项目通用 Agent”的原因

可以称为：

> 具备通用架构、当前重点覆盖 Python/JVM/C/C++ 的多语言环境构建与有限自主修复 Agent。

暂时不能称为任意项目通用 Agent，主要还缺：

### 10.1 语言和构建生态

生产 parser 当前只注册 Python、JVM、Native C/C++。还缺生产级：

- JavaScript/TypeScript：npm、yarn、pnpm；
- Rust/Cargo；
- Go Modules；
- Ruby/Bundler；
- PHP/Composer；
- C#/.NET；
- Bazel/Buck；
- Android、React Native；
- 多语言 monorepo。

Tree-sitter 能读取这些语言不等于能选择 runtime、安装依赖、构建、测试和运行。

### 10.2 Unknown ecosystem bootstrap

当前 parser 无法识别时在构建前直接停止，LLM 没有机会为未知项目从零生成 `ProjectProfile` 和
`BuildPlan`。通用版本需要增加：

```text
UnknownEcosystemAgent
→ 只读调查 README/CI/manifest/build files
→ 结构化 ProjectProfile
→ 候选 BuildPlan/setup script
→ sandbox preflight
→ 执行反馈修复
```

### 10.3 完整仓库接入

当前顶层工作流要求调用者已经提供存在的本地 workspace。还需要补齐：

- Git URL/压缩包/本地目录统一输入；
- clone 固定 commit；
- submodule、Git LFS；
- monorepo 子项目发现；
- 干净隔离副本；
- 最终镜像、脚本和使用说明交付。

### 10.4 服务和平台合同

还需扩展 MySQL、MongoDB、Kafka、RabbitMQ、Compose、多进程、worker、浏览器、GPU/CUDA、
Android SDK/NDK、交叉编译、专有依赖、外部 API 和 secret 等场景。

### 10.5 验证和泛化证据

仍需各语言原生测试发现、monorepo task 映射、CLI/Web/library 真实探针、稳定测试切片、隐藏
oracle，以及新的未见项目评测。历史统一 Agent 修复证据仍只有 1/10，尚不足以证明稳定修复率。

## 11. `multilang/run_execution.py` 为什么没有 Agent/LLM

这里不是某个开关在运行时关闭，而是该 runner 从架构上没有构造 Agent 和 LLM。

文件开头明确声明它只运行固定语料的确定性构建和验证，不创建 Agent/LLM client；其 evaluation
policy 也记录：

```python
"agent_enabled": False
"llm_enabled": False
```

`main()` 只创建：

```python
MultiLanguageProjectParser()
create_deterministic_builder(...)
create_layered_verifier(...)
```

`run_case()` 只调用：

```python
parser.parse(...)
builder.build(...)
verifier.verify(...)
```

构建失败后直接记录，不会调用 `create_api_environment_build_workflow()`。

这样设计是为了 M10–M12 单独测确定性能力，避免 LLM 掩盖 parser/build strategy 缺陷，降低成本
和非确定性，并形成 Agent 增益的初始基线。它能回答确定性构建能力，不能回答完整 Agent 修复能力。

`evaluations/prompt12/run_evaluation.py` 才会创建 API LLM 和完整生产 Agent，但该 runner 原本面向
旧 Python manifest，不应原样用于 ExecutionAgent 50 项。

## 12. HerAgent 为什么能覆盖很多语言

HerAgent 的多语言能力不是为每种语言实现一个完整专用构建 adapter，而是由以下组合产生：

### 12.1 LLM 充当通用生态适配器

HerAgent 检索 README、CI、dependency/build/config 文件后，让 LLM 生成完整的
`prometheus_setup.sh`。LLM 根据 `package.json`、`Cargo.toml`、`go.mod`、`Gemfile`、`pom.xml`
等自行选择 npm/yarn/pnpm、Cargo、Go、Bundler、Maven 等工具。

### 12.2 通用大基础容器

其通用镜像预装 Python、Node/npm、Java/JDK、gcc/g++、CMake/Make、数据库客户端和常用工具；
未预装的 Rust、Go、Ruby、PHP 等可由 setup script 动态安装。

### 12.3 14 种语言的 Tree-sitter/知识图谱

HerAgent parser 层覆盖 Bash、C、C#、C++、Go、Java、JavaScript、Kotlin、PHP、Python、SQL、
Rust、Ruby、TypeScript，并用 AST 和文本块构建 Neo4j 知识图谱。该能力用于理解和检索项目，
真正生成安装命令的仍是 LLM。

### 12.4 CI/CD 是跨语言可执行说明书

HerAgent 从 `.github/workflows` 提取 install/build/test/run 命令。不同语言都能归一为 shell command、
exit code 和 stdout/stderr，减少了专用 adapter 数量。

### 12.5 多轮脚本修复

环境或测试失败后，HerAgent 把当前脚本、日志和前三轮历史交给 LLM，让模型生成新的系统包、
Python、Node、Cargo、Go、Gem、Composer 等修复命令，并局部修改 setup script 后继续执行。

### 12.6 覆盖广度的代价

HerAgent 本地 ExecutionAgent 报告不是 50/50：完全成功 33/50，部分成功 41/50。其路线覆盖广，
但也有 LLM 调用多、成本高、非确定性、通用镜像大、Shell 权限宽、弱测试/错误命令和审计难度更高
等代价。

## 13. DPRAuto 与 HerAgent 的核心路线差异

| 维度 | HerAgent | DPRAuto 当前 |
|---|---|---|
| 主路线 | LLM-first | deterministic-first |
| 语言适配 | LLM 生成 Bash | Python/JVM/C/C++ 专用 parser/strategy |
| 未知语言 | LLM 尝试动态安装 | parser 失败时可能提前停止 |
| 初始环境 | 通用大镜像 | 按语言/项目生成镜像 |
| 修复权限 | Shell 和脚本较自由 | 有界工具、白名单、CAS、policy |
| 初始覆盖 | 广 | 较窄 |
| 可复现和审计 | 相对弱 | 相对强 |
| 安全边界 | 更宽 | 更严格 |

建议 DPRAuto 吸收 HerAgent 的 Unknown ecosystem、CI command extraction 和通用 setup plan 能力，
但保留当前结构化模型、确定性优先、安全工具、CAS、preflight、分层验证和 regression，不应简单
复制任意 Shell 执行模式。

## 14. 建议下一步

如果用户批准进入实现，推荐顺序：

1. 新建 `evaluations/executionagent/`，只实现 manifest、identity、checkpoint、隔离和结果 schema；
2. 复用 `create_api_environment_build_workflow()` 接入真正 Agent；
3. 增加 HerAgent 答案目录隔离和隐藏 oracle；
4. 用已见项目验证 runner，不修改生产策略；
5. 冻结生产 digest 后执行 50 项 Pass@1；
6. 单独报告已见 10、冷启动 40、全 50；
7. 封存首轮基线后再聚类失败；
8. 优先实现 UnknownEcosystemAgent 和 Node/Rust/Go 等生态，但不能在看完测试集后把同一 50 项
   的提升继续宣称为纯冷启动泛化；后续还需新的未见语料。

## 15. 本轮实际操作边界和当前 Git 状态

本轮执行的均为只读检查，包括 `rg`、`find`、`sed`、`nl`、`pdftotext`、`git status/log` 等；
唯一文件写入是新增本交接总结。

写本文前检查到：

- 当前分支：`main`；
- 当前 HEAD：`a31a058 docs: record language build totals`；
- `main` 与 `origin/main` 未显示 ahead/behind；
- 工作区已有未跟踪目录 `evaluations/corpus60/`，本轮未读取、未修改、未删除，应视为用户或其他
  工作留下的内容并继续保护；
- 没有执行任何项目构建、Docker 测试、单元测试或 LLM 请求；
- 没有修改或读取 `myapi.json` 内容；
- 没有 commit、reset、clean、push。

---

# 本轮新增交接：四项目比较、60 项新语料、能力检测与改进计划

> 本章追加于 2026-09-02。上面的第 1～15 节是同一天较早一轮对话的记录；本章记录此后围绕
> CXXCrafter、HerAgent、EnvBench、CNB 与 DPRAuto 开展的工作。两部分应一起保留，不应互相覆盖。

## 16. 本轮用户目标与任务演进

用户再次明确 DPRAuto 的最终目标：输入任意语言的项目后，系统能够自动完成：

```text
项目接入
→ 仓库结构和构建入口分析
→ 环境需求与构建步骤生成
→ 自动执行、诊断和修复
→ 构建并验证可直接运行的 Docker/OCI 环境
```

本轮任务按时间依次为：

1. 分析 `/home/master/auto-build` 下参考项目及 `/home/master/dprauto` 的原理、工作流、能力缺口和
   与最终目标的距离；
2. 判断 DPRAuto 应分别向其他项目学习什么；
3. 在修改生产能力之前，先建立一个新的、未被 DPRAuto 既往测试使用的小型跨语言测试集：
   C++、Python、Java 各 20 个；
4. 用新语料检测 DPRAuto 当前的静态识别和确定性计划能力；
5. 根据实测缺陷和参考项目优点提出 DPRAuto 改进计划；
6. 解释为什么阶段计划中“要学习的东西”与用户提供的总结文档表述不完全一致，并修正优先级和
   缺失项。

## 17. 参考项目与 DPRAuto 的定位结论

本轮讨论涉及以下参考来源：

- CNB：最值得学习标准化、可扩展的构建协议、层模型、进程类型和最终 OCI 镜像交付；
- CXXCrafter：最值得学习复杂 C/C++ 工具链、构建目标、依赖映射以及真实构建失败反馈循环；
- HerAgent-main：最值得学习全仓库理解、问题驱动检索、CI 命令抽取、环境脚本生成与分层验证；
- EnvBench-main：主要价值是可复现的数据集、固定 revision、ground truth 和评测基础设施，不应被
  当作生产构建架构直接照搬；
- DPRAuto：已有最完整的“确定性构建 + 失败分类 + 有界 Agent 修复 + 安全修改 + 分层验证”骨架，
  但语言/构建生态覆盖和多工作区发现能力仍不足。

“哪一个最接近目标”必须按维度回答：

| 维度 | 最接近者 | 原因 |
|---|---|---|
| 当前可继续演进的完整 Agent 骨架 | DPRAuto | 已有结构化状态、策略组合、失败分类、有界修复和验证闭环 |
| 任意语言/未知生态的仓库理解广度 | HerAgent | LLM、CI 和通用脚本路线覆盖面最广 |
| 标准构建协议及最终镜像交付 | CNB | detect/build 分离、Buildpack 生态、layers/processes/OCI 交付成熟 |
| 复杂原生项目构建和诊断 | CXXCrafter | 对 C/C++ 工具链、依赖和失败反馈更深入 |
| 基准测试与可复现实验 | EnvBench | 更适合作为数据和评价体系 |

因此，DPRAuto 不应照搬某一个项目。正确方向是以自身安全、可审计的闭环为基础，分别吸收各项目
最强的部分。

## 18. 新建的 60 项跨语言测试集

### 18.1 目录与文件

已在 `/home/master/dprauto/evaluations/corpus60/` 创建：

- `manifest.json`：60 项冻结清单、来源、revision、选择依据和大小信息；
- `exclusions.json`：从 DPRAuto 既往评测 JSON 汇总出的 38 个历史仓库排除证据；
- `README.md`：语料来源、选择规则、完整项目列表和复现命令；
- `CAPABILITY_BASELINE.md`：当前能力、实测结果、缺陷和下一阶段建议；
- `build_manifest.py`：可重复生成/检查清单的脚本；
- `fetch_sources.py`：按冻结 revision 下载并安全解包源码；
- `validate_corpus.py`：验证唯一性、语言配额、历史排除、revision、标志文件和本地快照；
- `probe_dprauto.py`：调用生产 parser 和确定性计划生成器做静态能力探测；
- `sources/`：60 个项目的本地源码快照，约 253 MiB，按语言分为三层目录并被 `.gitignore` 忽略；
- `probe-results.json`：60 项静态探测结果，被 `.gitignore` 忽略。

上述目录目前是未跟踪工作区内容，没有提交到 Git；`sources/` 和探测结果设计为不入库。

### 18.2 样本来源和选择约束

- C++：从 CXXCrafter `Top100` 数据集中选取 20 项；
- Python：从 HerAgent 使用的 EnvBench Python 清单中选取 20 项；
- Java：从 EnvBench JVM 清单中选取 20 项；
- 所有项目均排除 DPRAuto 既往评测 JSON 中出现过的仓库；
- 所有项目固定到明确 revision；CXXCrafter 原始 Top100 未提供 revision，因此固定到 2026-09-02
  当时可获取的完整 40 位 commit；
- 选择时限制仓库规模，并优先保留较小快照；
- C++ 集合有意保留 Vireo、Stockfish、OpenALPR 三个只有嵌套构建入口的项目，用于测试工作区发现，
  不能因为当前系统失败而从语料中移除。

完整 60 项名称已记录在 `evaluations/corpus60/README.md` 和 `manifest.json`，不在本交接文件重复维护
第二份容易漂移的清单。

### 18.3 “之前未使用”的严格含义

这里的“之前未使用”定义为：未出现在扫描到的 DPRAuto 既往冻结评测 JSON 中。共汇总 38 个历史
排除仓库，新 60 项与其项目级交集为 0。

它不能表示这些项目从未被 CXXCrafter、HerAgent 或 EnvBench 使用，因为用户要求正是从这些数据源
抽取项目。它也不是密码学意义上的全世界未见数据；未来如果发现其他未被扫描的人工调试记录，仍应
补充排除证据并重新审计。

## 19. 语料验证和可复现状态

本轮创建后以及撰写本文时均执行了语料验证。最新复核命令为：

```bash
python3 evaluations/corpus60/validate_corpus.py --require-sources
```

最新输出：

```text
validated 60 unique cases including local snapshots: cpp=20 python=20 java=20; history exclusions=38
```

当前可复核事实：

- 60 个唯一项目；
- C++ 20、Python 20、Java 20；
- 38 个历史排除仓库；
- 60 份本地快照均存在，revision、来源和项目标志检查通过；
- 本地源码约 253 MiB；
- `probe-results.json` 含 60 条记录；
- 冻结时 DPRAuto revision：`a31a05847e4a62cbb58c16913de05dd9a7a98fe7`。

撰写本文时三个关键文件的 SHA-256 为：

```text
2f74a3f95f571005755488df78856c948204c31824f688dc61b1e2cdc1282939  manifest.json
6da79954a6efad3bb0e5361318c0cb0a957520bc7daf0f614cc5f6fa168ee8f2  exclusions.json
dc2966f90b6cf66ddb5da71056586850c8e6e2d779d056fc8a3cce12f9c67271  probe-results.json
```

注意：这些哈希对应当前未提交文件；任何重新生成操作都应记录新哈希和原因。

## 20. DPRAuto 当前能力检测结果

### 20.1 静态 parse/plan 探测

探测复用了生产 `MultiLanguageProjectParser` 和确定性 Docker 计划生成逻辑，但没有运行 60 次
Docker build，因此结果不能表述为“57 个项目环境构建成功”。

| 语言 | 成功解析并生成计划 | 已解析但无计划 | 未解析 |
|---|---:|---:|---:|
| Python | 20/20 | 0 | 0 |
| Java | 20/20 | 0 | 0 |
| C++ | 17/20 | 2/20 | 1/20 |
| 合计 | 57/60 | 2/60 | 1/60 |

细分计划结果：

- Python：20/20；
- Java：20/20，其中 Maven 14、Gradle 6；
- C++：17/20，其中 CMake 15、Make 1、Autotools 1。

### 20.2 三个失败样本与根因

1. `twitter/vireo`：根目录没有当前 parser 接受的原生构建标志，真正入口位于子目录，导致没有
   parser 路由；
2. `official-stockfish/Stockfish`：原生入口为 `src/Makefile`，根层辅助 Python 文件先被识别，项目
   被误判为 Python，随后不能生成 Native 计划；
3. `openalpr/openalpr`：`src/CMakeLists.txt` 是嵌套入口，根层 Python 痕迹导致误判为 Python，随后
   不能生成 Native 计划。

共同根因不是缺一条项目特例，而是：

- `NativeProjectParser.supports()` 过度依赖根目录标志；
- 系统只选择单一 profile，缺少多组件/多 workspace 候选；
- parser 优先级在 Native 被根目录规则拒绝后，让辅助语言痕迹主导分类；
- 尚无“收集证据 → 构建候选图 → 评分并选择入口”的统一仓库智能层。

### 20.3 自动化测试证据

本轮已获得以下证据：

- 聚焦多语言 parser/strategy 回归：50/50 通过，用时 63.171 秒；
- 全测试发现：396 项，用时 258.822 秒；390 通过、5 跳过、1 失败；
- 唯一失败为 PostgreSQL 服务编排集成测试超时；该测试单独重跑约 23 秒通过，因此属于套件级
  flaky 证据，不能把全套件声称为全绿。

旧证据仍应分开报告：历史固定 21 项 M10 是 15/21 确定性构建成功、5/21 严格成功；后续 Java
4/4 和 C++ 5/5 是定向结果，不能与旧分母拼接成新的全局成功率。

### 20.4 已确认的其他能力边界

- 生产构建 parser/strategy 目前实际覆盖 Python、Maven/Gradle JVM、C/C++，不是任意语言；
- 代码索引能识别更多语言后缀，不等于能为这些语言构建环境；
- 已有策略包括仓库 Dockerfile、JVM template、Native template、Python template 和可选 CNB；
- 已有完整 LangGraph 流程：parse、确定性构建、失败分类、有界修复、preflight、重建、分层验证、
  regression 和最终差异；
- 已有 typed failure taxonomy、修改范围限制、CAS/候选工作区思想、风险门控和轮数/时间预算；
- `pyproject.toml` 尚无正式安装式 CLI entrypoint；
- README 中仍残留 JVM/Native 是未来工作的过时说法；
- 尚未对新 60 项执行完整 Docker build、install/test/run 或 Agent 修复评测。

## 21. DPRAuto 应向各项目学习的具体能力

### 21.1 向 CNB 学习：战略架构最高优先级

1. Buildpack 风格的 Provider/生态插件协议：

```text
detect
→ analyze
→ create_build_plan
→ build
→ discover_tests
→ discover_processes
→ verify
→ classify_failure
→ propose_repairs
```

2. detect 与 build 分离，并引入 `provides` / `requires` 能力协商，使语言、运行时、系统库、服务等
   Provider 能组合，而不是依赖单一语言分类；
3. 显式 LayerPlan：toolchain、runtime、dependencies、application、cache，支持缓存、复用和精确重建；
4. Process Types：`web`、`worker`、`release`、`task`、`console`，避免把“镜像能 build”误当“环境
   可直接运行”；
5. RuntimeContract：入口、命令、工作目录、端口、健康检查、环境变量、服务依赖、持久卷和权限；
6. 区分 build image、test image 和最小 runtime image，并以 OCI 镜像、digest、运行说明及验证报告
   作为正式交付物。

### 21.2 向 CXXCrafter 学习：复杂 Native 知识和真实反馈

1. 补齐 Bazel、Buck、XMake、SCons、Ninja、Build2 等构建生态；
2. 支持 Conan、vcpkg、CPM、Hunter、pkg-config、Git submodule、Debian control 等依赖来源；
3. 建立 header/library/pkg-config 名称到发行版系统包的证据化映射；
4. 分析内部 target、option、artifact path、C/C++ standard、GPU/SIMD 和平台约束；
5. 保留“真实构建 → 编译/链接错误分类 → 有界修改 → 重建”的闭环；
6. 首先泛化递归 workspace/入口发现，以修复新语料暴露的三个失败，不能为单项目写特例。

不应照搬 CXXCrafter 中任意 `eval`、整文件 LLM 覆盖、让 LLM 自判成功或弱化测试等不安全做法。

### 21.3 向 HerAgent 学习：仓库理解和环境成熟度

1. 问题驱动的检索模板，而不是把整个仓库无差别塞给模型；建议形成：

```text
FailureCategory
→ RetrievalTemplate
→ EvidenceBundle
→ RepairCandidate
```

2. 从 README、CI workflow、manifest、lockfile、构建脚本和测试配置提取可执行证据；
3. 将 CI 环境重建为结构化、安全的 Environment IR，而不是直接信任并执行任意 Shell；
4. 使用环境成熟度等级：L0 SourceAnalyzed、L1 Planned、L2 Buildable、L3 Testable、
   L4 Runnable、L5 Deployable；
5. 由同一结构化 IR 渲染 Dockerfile、setup script、Compose、BuildPlan 和最终报告；
6. 学习分层测试金字塔和测试命令发现，但继续使用 DPRAuto 的隔离、安全策略和隐藏 oracle。

不应照搬任意 Shell、Docker socket、host network、宿主可写目录或弱成功判据。

### 21.4 向 EnvBench 学习：评测和数据基础设施

1. 固定项目 revision、输入 manifest 和评价身份；
2. 明确训练/调试污染、已见集、冷启动集和 holdout；
3. 保存不可变运行记录、日志、镜像 digest、资源预算和失败类别；
4. 将 parse、plan、build、test、run 和 Agent repair 分阶段计分；
5. 使用隐藏 oracle 检测 false success。

EnvBench 的角色是持续证明能力，而不是替代 CNB/CXXCrafter/HerAgent 的生产架构经验。

## 22. 综合改进计划

### 紧急 P0：修复当前实测入口发现缺陷

- 实现深度受限、忽略 vendor/build/cache 的递归 workspace 扫描；
- 产出多个 `ComponentCandidate`，包含根路径、语言证据、构建系统、入口和置信度；
- 支持 monorepo 和“根层辅助语言 + 子目录主项目”；
- 先用 Vireo、Stockfish、OpenALPR 验证通用规则，再对 60 项做无回退回归。

这是当前缺陷修复顺序的第一项，但不代表 CXXCrafter 在长期架构上高于 CNB。

### 战略 P1：CNB 风格 Provider 协议

- 定义 Provider 生命周期接口；
- 增加 `provides/requires` 能力协商和依赖图求解；
- 将现有 Python/JVM/Native/CNB 策略迁移为 Provider；
- 引入 LayerPlan、ProcessType、RuntimeContract 和 OCI ArtifactContract。

### P2：扩充 C/C++ 构建知识

- 增加构建系统、包管理器、系统依赖映射和内部 target 分析；
- 将日志分类绑定到结构化检索与安全修复工具；
- 用真实 build/test/run 验证，而不是模型自报成功。

### P3：HerAgent 风格仓库智能与 Unknown Ecosystem

- 构建统一 Evidence Graph；
- 增加 CI/README/manifest/lockfile 的问题驱动检索；
- parser 无法识别时进入只读 UnknownEcosystem 分析，而不是立即停止；
- 生成结构化 Environment IR，再由受控 renderer 产生构建文件；
- 逐步增加 Node、Rust、Go、Ruby、PHP、.NET 等 Provider。

### P4：安全反馈闭环和环境交付

- 保留 FailureInfo、预算、重复失败检测、policy、CAS、preflight 和 regression；
- 将修复范围从文件级扩展为 IR/层/Provider 级原子变更；
- 实测 Process Types、健康检查、端口和服务依赖；
- 输出可直接运行的镜像引用/digest、启动命令、Compose（需要时）、SBOM/依赖摘要和验证报告。

### P5：CLI、文档和产品化

- 提供类似 `dprauto build <git-url|archive|directory>` 的稳定入口；
- 统一 clone、固定 revision、submodule/LFS、隔离副本和产物目录；
- 修正文档中 JVM/Native 的过时状态；
- 提供机器可读结果和清晰退出码。

### P6：分阶段评测

对 corpus60 分开运行并记录：

1. parse/workspace discovery；
2. plan generation；
3. Docker build；
4. installability；
5. testability；
6. runnability；
7. 初始失败后的 Agent repair；
8. 最终 OCI artifact 与 RuntimeContract 验证。

每阶段区分 unsupported、project failure、infrastructure failure、timeout、runner error 和 false
success。完成开发调优后还需要新的 holdout，不能把已经用于修复的 corpus60 继续称为纯未见集。

## 23. 为什么此前计划与用户引用的“应学习内容”不完全一致

结论：方向大体一致，但此前计划把“当前故障的实施顺序”和“长期架构学习优先级”混在了一起，
又把若干能力压缩改名，导致看起来不一致；其中也确有三项内容被弱化或遗漏。

原计划中的对应关系是：

| 用户引用中的能力 | 原阶段计划中的名称 |
|---|---|
| CNB 插件协议 | M3 Provider 插件系统 |
| CNB Process Types / 最终运行 | M6 RuntimeContract / 可运行交付 |
| CNB layers | M4 plan portfolio 中部分体现，但不充分 |
| CXXCrafter 入口发现 | M1 multi-workspace discovery |
| CXXCrafter 构建失败循环 | M5 safe feedback/repair |
| HerAgent 全仓库理解 | M2 evidence graph |
| HerAgent 分层验证 | M6 runtime contract / verification |
| HerAgent 结构化环境表达 | M4 plan/IR，但表述过于隐含 |

真正需要修正之处：

1. CNB 的长期战略优先级被表现得过低。正确说法是“当前热修先做 CXX 风格入口发现，长期架构先做
   CNB 风格协议”；
2. CNB 的 `provides/requires` 能力协商必须显式加入；
3. CNB 的 LayerPlan 不能只藏在 plan portfolio 中，必须成为核心模型；
4. CXXCrafter 的构建生态、依赖映射和内部 target 分析被过度压缩，必须恢复；
5. HerAgent 的“问题类型 → 检索模板”必须显式写入；
6. EnvBench 是后来根据实际测试工作加入的第四类来源，应定位为评测基础设施，不能替代三项核心
   架构学习来源。

修正后的统一优先级是：

```text
当前立即修复：CXX 风格递归 workspace discovery（解决 3 个 corpus60 失败）
长期架构 P1：CNB Provider + provides/requires + layers + runtime/process contracts
能力纵深 P2：CXXCrafter Native 工具链、依赖与错误反馈
能力广度 P3：HerAgent 问题驱动检索、CI 重建、Environment IR、测试金字塔
持续证明 P4：EnvBench 式可复现评测与新 holdout
```

用户提供的引用总结在“应学习内容”上比此前压缩版计划更完整；后续实施应以本章恢复后的版本为准。

## 24. 本轮实际写入、未执行事项与当前边界

### 已执行/写入

- 审计 DPRAuto 与参考项目的架构、数据集和评测资料；
- 汇总 DPRAuto 既往评测仓库并生成 38 项排除证据；
- 创建并冻结 60 项清单；
- 下载 60 个固定 revision 的源码快照；
- 创建复现、下载、验证和静态探测脚本；
- 执行语料完整性验证；
- 执行生产 parser/确定性 plan 静态探测；
- 执行聚焦 50 项回归测试和一次 396 项完整测试发现，并隔离重跑唯一超时项；
- 生成 README、能力基线和本交接增补。

### 尚未执行

- 未修复 Vireo、Stockfish、OpenALPR 暴露的生产代码缺陷；
- 未对 corpus60 执行 60 个 Docker 镜像构建；
- 未对 corpus60 执行完整 install/test/run 分层验证；
- 未在 corpus60 上启用 LLM Agent 修复；
- 未实现 Provider、Capability negotiation、LayerPlan、Environment IR 或新 CLI；
- 未修改 `myapi.json`，未在本轮调用外部 LLM API；
- 未 commit、push、reset、clean 或删除用户文件。

### 当前 Git 工作区注意事项

撰写本增补前看到以下未跟踪内容：

```text
?? CONVERSATION_HANDOFF_SUMMARY_2026-09-02.md
?? evaluations/corpus60/
```

这些都是应保留的交接/评测成果。后续 Agent 不得用 `git clean` 或其他清理操作删除。是否将脚本、
manifest、README、能力报告及本交接文档纳入版本控制，应由用户后续决定；`sources/` 和动态探测结果
继续保持忽略更合适。

## 25. 下一位接手者的建议起点

如果用户下一步要求“开始改进”，建议先：

1. 冻结 corpus60 当前 manifest/hash，不改样本；
2. 为 WorkspaceScanner、ComponentCandidate 和多 profile 选择补单元测试；
3. 实现通用嵌套入口发现，不添加 Stockfish/OpenALPR/Vireo 名称特判；
4. 重新运行聚焦测试、完整测试和 60 项 probe，目标是 60/60 parse+plan 且既有用例无回退；
5. 随后再启动分批 Docker build 基线；
6. 封存初始可执行基线后，进入 CNB 风格 Provider/Capability/Layer/RuntimeContract 重构。

若用户只要求“给计划或解释”，不要擅自修改生产代码或启动耗时、可能调用付费 LLM 的完整评测。

# DPRAuto 全部对话工作总结

更新时间：2026-08-18（Asia/Shanghai）

本文汇总截至 2026-08-18 的全部连续对话，包括已有交接记录中的工作、后续代码优化、阶段性真实项目验证，以及最新完成的 21 项完整复跑。

## 1. 总体目标与当前结论

项目目标是构建一套面向真实 Python 项目的可复现环境自动构建系统，主流程为：

```text
项目解析
  -> 确定性构建
  -> 失败分类
  -> Agent 诊断与受控修复
  -> 重新构建
  -> Installability/Testability/Runnability 分层验证
  -> 回归检查
  -> 环境差异与结果归档
```

截至目前，核心工程链路已经完整落地并在真实项目上运行。最新同项目 21 项评测相较旧评测取得了以下结果：

- 标准构建成功：`9/21 -> 12/21`；
- 最终成功：`1/21 -> 2/21`，成功率从 `4.76%` 提升到 `9.52%`；
- 总耗时：`15089.5s -> 6780.2s`，下降 `55.1%`；
- 最大单项目耗时：`1512.0s -> 811.4s`，下降 `46.3%`；
- LLM 调用：`89 -> 44`，下降 `50.6%`；
- LLM 错误：`13 -> 5`，下降 `61.5%`；
- `max_attempts` 终态：`14 -> 0`；
- apt DNS 的约 295 秒重试问题没有在最新完整评测中复现。

但核心限制仍然存在：最新 21 项中 Agent 参与了 16 项，Agent 修复成功仍为 0。当前成功率提升主要来自确定性构建、验证策略和快速停止，而不是 LLM 修复能力发生了质变。

## 2. 最初已经完成的工程能力

最早阶段完成了从领域模型到真实执行的基础框架，而不是只停留在架构设计。

### 2.1 领域模型与工程分层

已经实现：

- `ProjectProfile`、`BuildPlan`、`BuildResult`、`FailureInfo`；
- `VerificationResult`、`VerificationReport`、`EnvironmentDiff`；
- 统一配置、错误类型、领域枚举；
- `ports/adapters/application/agent` 分层；
- Artifact 存储和 SHA-256 校验；
- 业务代码默认不可修改的安全边界。

主要目录：

- `src/dprauto/domain`
- `src/dprauto/ports`
- `src/dprauto/adapters`
- `src/dprauto/application`
- `src/dprauto/agent`
- `src/dprauto/verification`

### 2.2 Python 项目解析

实现了规则优先、只读的 Python 项目解析，可提取：

- Python 版本约束；
- requirements、PEP 621、Poetry、PDM、setup.py/setup.cfg 等依赖信息；
- 包管理器；
- Dockerfile、README、CI 文件；
- 安装、测试和运行命令；
- library、CLI、script、web 等项目类型。

### 2.3 确定性构建

实现了统一构建策略接口和以下策略：

- `DockerStrategy`：优先使用项目自带 Dockerfile；
- `TemplateStrategy`：没有 Dockerfile 时生成 Python 模板环境；
- `CNBStrategy`：保留固定 builder 的 CNB/Pack 路径。

构建会持久化计划、输入 Dockerfile、生成文件、命令、完整日志和结构化结果。

### 2.4 失败分类

规则分类覆盖：

- Git 和网络；
- Docker 基础设施；
- 系统依赖；
- Python 版本；
- Python 依赖缺失或冲突；
- 构建工具、原生编译、测试、运行；
- 外部服务和 Unknown。

规则无法识别时才进入受控 fallback，完整日志不会直接无界传给 LLM。

### 2.5 Agent、状态与持久化

LangGraph 工作流拆分为独立节点：

```text
analyze_failure -> plan_fix -> apply_fix -> execute -> verify -> evaluate
```

已经支持：

- 最大修复次数；
- 重复失败和修复方法指纹；
- SQLite checkpoint 与恢复；
- 有界上下文摘要；
- 完整历史和日志外置；
- 每轮 unified diff 与 `environment-diff.json`；
- 默认只允许修改 Dockerfile、setup.sh 等构建脚本；
- 路径穿越、symlink、任意 shell 和业务源码修改拦截。

### 2.6 分层验证与回归检查

实现三个验证层：

- Installability：镜像和安装结果是否可用；
- Testability：选择项目证据支持的测试命令并执行；
- Runnability：按 library/CLI/script/web 类型进行运行验证。

同时保存通过基线，并在修复后检查是否引入回归。

## 3. 第一轮真实项目评测暴露的问题

旧的 24 项真实项目评测由以下两部分组成：

- 项目 1–3：`evaluations/prompt14/runs/third-round-20260812`
- 项目 4–24：`evaluations/prompt14/runs/third-round-20260813-part2`

合并结果：

- 标准构建成功 12/24；
- 最终成功 1/24；
- 唯一成功项目为 `karpathy/minbpe`；
- Agent 参与 22 项，Agent 修复成功 0；
- 总 LLM 调用 107 次，其中错误/超时记录 19 次；
- 总耗时 17784.8 秒，平均每项目 741.0 秒；
- `max_attempts` 15 项；
- `hydropandas` 耗时 1512.0 秒；
- `donation-tracker` 耗时 1381.8 秒。

该轮证明了主链路可以运行，但也暴露出：

1. `agent_max_total_seconds=900` 并非真正的硬上限；
2. 构建超时后 Agent 容易继续扩大依赖面；
3. 测试/运行失败证据不足，LLM 经常修错目标；
4. 标准构建成功不能稳定转化为最终环境成功；
5. LLM 超时和多模型故障转移显著放大总耗时；
6. 很多失败最终笼统落入 `max_attempts`，终态解释性较弱。

## 4. 围绕 LLM 与 Tool 的改动

### 4.1 LLM 超时、重试和多模型故障转移

完成的操作：

- 新增 `LLMTimeoutError`；
- 从 `myapi.json` 按顺序加载模型池；
- 当前模型超时后先重试，再切换下一个模型；
- 限制模型最大输出 token；
- LLM HTTP timeout 会根据工作流剩余总预算进行收缩；
- 总预算耗尽后不再启动新的模型调用或故障转移。

效果：

- 单个模型超时不再立即结束项目；
- 最新 21 项 LLM 调用从 89 降至 44；
- LLM 错误从 13 降至 5；
- token 从 454630 降至 237795，下降 47.7%。

### 4.2 Tool 参数契约

完成的操作：

- 每个 Tool 暴露严格参数 schema；
- 规划 prompt 中包含 Tool schema；
- 调用前进行额外字段和必填字段校验；
- 非法结构允许一次受控纠正；
- `build_image` 不再暴露给 LLM，重建只由工作流节点调度。

效果：

- 减少无效 Tool 参数和重复构建调用；
- LLM 只负责提出受控构建脚本修改，不直接控制重建流程；
- 最新评测的重复修复计划为 0。

### 4.3 验证失败证据进入 LLM

完成的操作：

- 把验证层、失败命令、退出码、是否超时、耗时和输出摘要写入 `FailureInfo`；
- Testability/Runnability 失败时优先使用验证证据，而不是读取无关的成功构建日志；
- 验证阶段的明确网络错误归类为基础设施故障，不允许 Agent 修改项目来“修复网络”。

效果：

- 终态从大量 `max_attempts` 转为更明确的 `verification_failed`、`regression`、`infrastructure_failed` 和 `project_failed`；
- 最新 21 项 `max_attempts` 已从 14 降为 0。

## 5. 总时间预算与快速停止

### 5.1 硬 deadline 贯穿全链路

新增共享时间预算模块 `src/dprauto/time_budget.py`，将绝对 `deadline_at` 传递到：

- Agent 每个工作流节点；
- Docker/Template/CNB 构建；
- Tool 执行；
- LLM 请求和 failover；
- Installability/Testability/Runnability 验证。

所有子操作的 timeout 会被裁剪到剩余预算；预算耗尽后不再启动新操作。

效果：

- `hydropandas`：`1512.0s -> 280.3s`，下降约 81.5%；
- `donation-tracker`：`1381.8s -> 300.3s`，下降约 78.3%；
- 最新 21 项最大耗时为 811.4 秒，没有再超过 900 秒总预算。

### 5.2 构建超时快速停止与成本回归

增加了面向超时的处理规则：

- 对 pip/Poetry/uv、系统包、原生编译、依赖解析等超时进行画像；
- 超时本身不能成为随意增加依赖、修改 Python 版本或更换基础镜像的理由；
- 优先缩小安装范围，避免 docs/lint/dev/test 全量 extras；
- 如果修复前构建成功、修复后构建超时，直接判定为 repair cost regression；
- 明显不可修复的模板安装超时可在进入 LLM 前终止；
- 基础设施错误在 LLM 和项目修改之前停止。

效果：

- 旧评测 14 个 `max_attempts` 被消除；
- Agent 平均修复轮数从约 1.95 降到 1.19；
- 但回归终态从 1 增加到 4，说明成本回归被更准确发现，同时也表明修复计划质量仍不足。

## 6. 验证策略与运行策略改进

### 6.1 Testability 命令选择

完成的操作：

- 排除 `--help`、`--version`、单纯 import、compileall 等 smoke command；
- 优先 pytest/unittest，再考虑 tox/nox；
- 对 docs/lint/typecheck 和外部数据库集成命令降低优先级；
- 跳过包含未解析 CI 变量的命令；
- 对 tox/nox matrix 尽量选择有证据的单一默认环境。

### 6.2 Runnability 规则

完成的操作：

- Library：要求成功 import，并观察公共 API 或已有测试通过；
- CLI：要求退出码为 0 且存在可观察输出；
- CLI 默认运行成功但无输出时，自动追加 `--help` 再验证；
- Script：要求输出或文件系统副作用；
- Web：检查进程、端口和 HTTP；
- 所有验证命令受总 deadline 约束。

效果：

- `python-markdown/markdown` 在 `post-cli-help-fallback-17-20260817` 中首次通过，最新完整评测继续成功；
- 最新 21 项 installability 通过数从 10 提升到 17；
- runnability 通过数从 3 提升到 9；
- testability 仍为 2，说明测试命令和测试依赖仍是当前最大瓶颈之一。

### 6.3 验证修复策略约束

增加了以下拒绝规则：

- 为解决验证失败而无依据更换基础镜像；
- 为解决验证失败而无依据修改 Python 版本；
- 把仅用于测试的依赖大规模塞入最终构建镜像；
- 通过过度扩大依赖面来绕过一个具体测试工具缺失。

阶段性验证目录包括：

- `post-verification-policy-0508-20260814`
- `post-runnability-policy-17-20260817`
- `post-cli-help-fallback-17-20260817`
- `post-cli-fallback-subset-20260817`

## 7. 网络与基础设施分类改进

完成的操作：

- Docker daemon、平台 manifest、镜像拉取、网络、DNS 与项目构建错误分开；
- `Temporary failure resolving ...` 等 apt DNS 文本进入网络规则；
- 系统包安装无进展并超时时推断为网络基础设施故障；
- 验证阶段的 pip/Git 网络失败也标记为 infrastructure；
- 评测汇总改为使用最终 terminal failure，而不是只使用初始失败；
- infrastructure failure 不进入项目 Agent 修复。

效果：

- 网络故障不再被误计为项目修复失败；
- 避免网络不可用时继续消耗 LLM 和修改项目；
- 最新评测中 2 个 network failure 和 1 个 Docker/platform failure 被单独统计；
- runner error 始终为 0。

## 8. apt DNS 约 295 秒问题的处理

### 8.1 原始问题

在 `boto3` 修复构建中，apt DNS 故障出现约 25 秒一段的多轮等待，直到约 295 秒才输出：

```text
Temporary failure resolving 'deb.debian.org'
```

原始验证目录：

- `evaluations/prompt14/runs/post-apt-dns-classifier-10-20260817`

该次最终正确分类为 `infrastructure_failed/network`，但等待成本仍过高。

### 8.2 被尝试但已经撤销的方案

曾尝试：只要修复增加系统包，就把整个 Agent Docker build 限制为约 90 秒。

验证目录：

- `evaluations/prompt14/runs/post-system-package-cap-10-20260818`

实际观察：

- 健康网络下 apt 正常完成；
- 后续 pip 仍在下载/安装；
- 整个构建在约 89 秒被误杀；
- 导致一次不必要的额外修复。

结论：该方案会把“apt DNS 故障”和“健康但较慢的后续 pip”混在一起，因此已经完整撤销，没有保留全 Docker build 的 90 秒硬限制。

### 8.3 当前最终方案

在 `ModifyBuildScriptTool` 写入 Agent 修复 Dockerfile 时，对以下命令进行幂等规范化：

- `apt-get update`
- `apt-get install`
- `apt update`
- `apt install`

加入：

```text
RES_OPTIONS="attempts:1 timeout:2"
-o Acquire::Retries=0
-o Acquire::http::Timeout=15
-o Acquire::https::Timeout=15
```

性质：

- 只作用于对应 apt 命令；
- 不修改宿主机或镜像的全局 DNS；
- 同时覆盖索引更新和 deb 下载；
- 再次经过 Tool 时不会重复插入；
- 不限制后续 pip、Git 或其他健康构建阶段。

实现位置：

- `src/dprauto/agent/tools/filesystem.py`
- `tests/test_agent_tools.py`
- `tests/fixtures/docker/apt_dns_bounded/Dockerfile`

### 8.4 apt 专项验证结果

受控测试结果：

- Docker `--network=none`：DNS 故障约 1.09 秒结束；
- 本地 UDP DNS 黑洞，仅关闭 apt retries：91.08 秒；
- 同时加入 `RES_OPTIONS="attempts:1 timeout:2"`：18.99 秒；
- 相比历史约 295 秒，预期等待下降约 93.6%；
- 健康网络完整 apt update + install 构建成功；
- 健康构建耗时主要来自正常的 27.1 MB 软件包下载，不是 DNS。

最新 21 项真实复跑中：

- 所有 command log 中 `deb.debian.org` DNS 失败为 0；
- `boto3` 的 apt 索引下载在 6.6 秒完成；
- apt 安装层在 138.7 秒完成，耗时来自正常慢下载；
- 最终 300 秒构建超时发生在随后 `git clone botocore`，不是 apt；
- 因此 apt DNS 的 295 秒重试链已经被消除，新的主要网络瓶颈转移到 GitHub/Python 包下载。

## 9. 自动化测试演进

交接前完整测试状态：

- 108 tests passed；
- 42 subtests passed。

随着 deadline、分类、验证、CLI fallback、评测汇总和 apt 限时等测试加入，当前完整测试状态为：

- 137 tests passed；
- 43 subtests passed；
- 1 个已有 `PytestCollectionWarning`，原因是 `TestabilityVerifier` 类名以 Test 开头且有自定义构造函数；
- 无测试失败。

新增或强化的测试重点包括：

- 工作流预算耗尽后不调用 LLM/Tool；
- build、verification、LLM timeout 根据剩余预算裁剪；
- infrastructure failure 在修复前停止；
- 修复导致构建超时的 regression；
- 验证失败证据传给 LLM；
- 验证网络失败分类；
- CLI 空输出时 `--help` fallback；
- 评测汇总使用终态失败；
- apt update/install 限时参数和幂等性。

## 10. 阶段性真实项目验证

优化过程中没有每次都立即运行全部项目，而是针对风险点使用小子集验证：

| 结果目录 | 目的 |
|---|---|
| `post-fix-subset-20260814` | 首轮 deadline/修复策略综合验证 |
| `post-verification-policy-0508-20260814` | 验证失败修复约束 |
| `post-plan-failover-limit-12-20260814` | LLM 规划 failover 与 deadline |
| `post-template-timeout-faststop-12-20260814` | 模板构建超时快速停止 |
| `post-faststop-subset-20260817` | 快速停止综合回归 |
| `post-policy-relax-1723-20260817` | 对过严验证政策做定向放宽 |
| `post-verification-network-infra-23-20260817` | 验证网络错误基础设施分类 |
| `post-runnability-policy-17-20260817` | library/CLI 运行策略 |
| `post-cli-help-fallback-17-20260817` | CLI 空输出 help fallback |
| `post-cli-fallback-subset-20260817` | CLI fallback 子集回归 |
| `post-apt-dns-classifier-10-20260817` | apt DNS 分类与 295 秒问题复现 |
| `post-system-package-cap-10-20260818` | 全构建 90 秒实验，后撤销 |
| `post-bounded-apt-healthy-10-20260818` | apt 命令级限时的健康网络验证 |
| `post-apt-dns-bounded-full21-20260818` | 当前最终方案的完整 21 项复跑 |

这些子集评测用于验证单一策略是否符合预期；由于外部网络状态变化较大，单次子集的成功/失败不能单独视为总体成功率结论。

## 11. 最新 21 项完整复跑

评测范围为清单索引 4–24，与旧的 `third-round-20260813-part2` 完全相同。

统一参数：

- Docker build timeout：300 秒；
- verification command timeout：90 秒；
- Agent 最大修复轮数：2；
- Agent 总预算：900 秒；
- source workspace 使用独立副本；
- Docker build cache：开启。

结果目录：

- `evaluations/prompt14/runs/post-apt-dns-bounded-full21-20260818`

### 11.1 汇总对比

| 指标 | 旧 21 项 | 新 21 项 | 变化 |
|---|---:|---:|---:|
| 标准构建成功 | 9/21 | 12/21 | +3 |
| 最终成功 | 1/21 | 2/21 | +1 |
| 最终成功率 | 4.76% | 9.52% | +4.76 个百分点 |
| Agent 参与 | 19 | 16 | -3 |
| Agent 修复成功 | 0 | 0 | 无变化 |
| 总耗时 | 15089.5s | 6780.2s | -55.1% |
| 平均耗时 | 718.5s | 322.9s | -55.1% |
| 最大耗时 | 1512.0s | 811.4s | -46.3% |
| LLM 调用 | 89 | 44 | -50.6% |
| LLM 错误 | 13 | 5 | -61.5% |
| Token | 454630 | 237795 | -47.7% |
| Installability 通过 | 10 | 17 | +7 |
| Testability 通过 | 2 | 2 | 无变化 |
| Runnability 通过 | 3 | 9 | +6 |
| `max_attempts` | 14 | 0 | -14 |
| Regression | 1 | 4 | +3 |
| Runner error | 0 | 0 | 无变化 |

### 11.2 新终态分布

- `succeeded`: 2；
- `verification_failed`: 10；
- `infrastructure_failed`: 3；
- `regression`: 4；
- `project_failed`: 2；
- `max_attempts`: 0。

### 11.3 成功项目

- `python-markdown/markdown`：8.1 秒，旧状态为 `verification_failed`，本轮新增成功；
- `karpathy/minbpe`：53.6 秒，保持成功。

两者均在标准路径直接成功，没有进入 Agent 修复。因此最终成功数增加，但 Agent repair success 仍为 0。

### 11.4 逐项目结果

| 索引 | 项目 | 旧状态 | 新状态 | 新耗时 | 构建次数 | LLM |
|---:|---|---|---|---:|---:|---:|
| 4 | `tmux-python/tmuxp` | `max_attempts` | `verification_failed` | 92.3s | 1 | 2 |
| 5 | `Textualize/rich` | `max_attempts` | `verification_failed` | 342.2s | 1 | 3 |
| 6 | `encode/starlette` | `verification_failed` | `infrastructure_failed` | 300.3s | 1 | 0 |
| 7 | `mandarons/icloud-drive-docker` | `infrastructure_failed` | `infrastructure_failed` | 7.8s | 1 | 0 |
| 8 | `simplistix/testfixtures` | `regression` | `infrastructure_failed` | 20.2s | 1 | 0 |
| 9 | `piccolo-orm/piccolo` | `max_attempts` | `regression` | 746.7s | 3 | 4 |
| 10 | `boto/boto3` | `max_attempts` | `verification_failed` | 811.4s | 3 | 5 |
| 11 | `andreidrang/python-rucaptcha` | `max_attempts` | `verification_failed` | 66.2s | 1 | 2 |
| 12 | `artesiawater/hydropandas` | `max_attempts` | `verification_failed` | 280.3s | 1 | 2 |
| 13 | `asdf-format/asdf` | `max_attempts` | `verification_failed` | 284.1s | 2 | 4 |
| 14 | `compserv/hknweb` | `max_attempts` | `regression` | 660.9s | 3 | 4 |
| 15 | `dagshub/client` | `project_failed` | `regression` | 632.4s | 2 | 2 |
| 16 | `gamesdonequick/donation-tracker` | `max_attempts` | `project_failed` | 300.3s | 1 | 0 |
| 17 | `python-markdown/markdown` | `verification_failed` | `succeeded` | 8.1s | 1 | 0 |
| 18 | `yubico/yubikey-manager` | `max_attempts` | `project_failed` | 300.3s | 1 | 0 |
| 19 | `psf/black` | `max_attempts` | `verification_failed` | 477.1s | 2 | 4 |
| 20 | `ranaroussi/yfinance` | `max_attempts` | `verification_failed` | 113.4s | 1 | 2 |
| 21 | `soimort/you-get` | `max_attempts` | `verification_failed` | 150.1s | 1 | 2 |
| 22 | `tiangolo/fastapi` | `max_attempts` | `regression` | 539.3s | 3 | 4 |
| 23 | `karpathy/minbpe` | `succeeded` | `succeeded` | 53.6s | 1 | 0 |
| 24 | `alexmolas/microsearch` | `project_failed` | `verification_failed` | 593.4s | 1 | 4 |

## 12. 当前取得的实际效果

### 已经明显改善

1. 总运行成本下降：同一 21 项节省约 8309 秒，约 2 小时 18 分钟。
2. 硬时间预算生效：不再出现 1300–1500 秒的单项目失控运行。
3. LLM 成本下降：调用、错误和 token 均下降约一半。
4. 终态更明确：14 个 `max_attempts` 被具体状态替代。
5. 基础设施隔离更好：网络和 Docker/platform 故障不再继续修改项目。
6. Installability 和 Runnability 通过数量明显提升。
7. CLI 无输出 fallback 让 `python-markdown` 从失败转为成功。
8. apt DNS 约 295 秒等待链已被命令级限时消除。
9. 完整测试从 108 增至 137，新增行为都有回归覆盖。

### 尚未解决

1. Agent 修复成功仍为 0/16。
2. Testability 通过仍只有 2/21，没有提升。
3. `verification_failed` 达到 10 项，是当前最大终态类别。
4. Regression 从 1 增至 4，修复计划仍会破坏已经通过的能力或增加构建成本。
5. GitHub clone、pip 下载、依赖解析和慢速软件包下载仍可能耗尽 300 秒构建上限。
6. 某些项目的“项目自带测试命令”实际包含 lint、全 CI matrix 或外部服务，仍然过重。
7. 最新结果受 Docker cache 和实时外部网络影响，是工程运行对比，不是完全隔离的实验室 A/B。

## 13. 建议的下一步顺序

1. 对最新 10 个 `verification_failed` 逐项聚类，区分：测试工具缺失、错误测试命令、外部服务、真实项目测试失败。
2. 改进测试命令选择，优先最小、离线、无外部服务的项目测试切片，避免 lint/docs/full tox matrix。
3. 对 4 个 regression 检查 Agent diff，加入“修复前后能力和依赖成本预测”，在重建前拒绝高风险计划。
4. 为 Git clone 和 pip 网络阶段增加与 apt 类似的阶段级超时/低速检测，但不能再次使用会误杀整个 Docker build 的粗粒度 90 秒限制。
5. 对 `boto3` 这类 VCS requirements 优先使用已有 lock、发布版依赖或浅克隆策略，减少 GitHub clone 卡住。
6. 在完成下一轮修复策略后，继续使用相同索引 4–24 和相同评测参数复跑，保证可比较性。

## 14. 关键文件与结果索引

代码：

- `src/dprauto/time_budget.py`
- `src/dprauto/agent/workflow.py`
- `src/dprauto/agent/full_workflow.py`
- `src/dprauto/agent/tools/filesystem.py`
- `src/dprauto/adapters/failure/rules.py`
- `src/dprauto/adapters/llm/api.py`
- `src/dprauto/adapters/llm/repair.py`
- `src/dprauto/verification/commands.py`
- `src/dprauto/verification/runnability.py`
- `evaluations/prompt12/run_evaluation.py`

旧基线：

- `HANDOFF_SUMMARY_2026-08-13.md`
- `evaluations/prompt14/runs/third-round-20260812`
- `evaluations/prompt14/runs/third-round-20260813-part2/summary.json`

最新结果：

- `evaluations/prompt14/runs/post-apt-dns-bounded-full21-20260818/summary.json`
- `evaluations/prompt14/runs/post-apt-dns-bounded-full21-20260818/records`
- `evaluations/prompt14/runs/post-apt-dns-bounded-full21-20260818/artifacts`
- `evaluations/prompt14/runs/post-apt-dns-bounded-full21-20260818/llm-logs`

## 15. 最终判断

当前系统已经从“链路能跑但容易失控”进展为“链路稳定、成本受控、失败可解释”。成功率从同项目 1/21 提升到 2/21，时间和 LLM 成本下降约一半，apt DNS 长等待问题得到实质解决。

下一阶段不应继续优先优化全局超时，而应集中处理 Testability 和 Agent 修复质量：选择更小的测试目标、限制依赖扩张、提前拒绝高风险修复，并解决 Git/pip 阶段的网络慢速问题。只有这些问题得到改善，Agent repair success 才可能从 0 开始增长。

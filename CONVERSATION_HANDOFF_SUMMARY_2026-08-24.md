# DPRAuto 当前会话工作与评测交接总结

更新时间：2026-08-24（Asia/Shanghai）

## 1. 文档范围与核心结论

本文汇总本轮连续会话中完成的项目审计、方案设计、代码修改、真实项目评测和当前环境状态。
本轮是在阅读以下已有交接资料后继续推进的：

- `CONVERSATION_PROGRESS_SUMMARY_2026-08-18.md`
- `HANDOFF_SUMMARY_2026-08-13.md`
- `scheme.md`

同时参考了 `/home/master/auto-build` 下三个项目的实现和数据：

- `CNB`：真实项目样本、CNB benchmark、历史构建结果和源码快照；
- `CXXCrafter`：环境构建与自动修复的工程组织参考；
- `HerAgent-main`：Agent 工作流、状态和工具调用边界参考。

当前结论分三层理解：

1. **确定性构建能力已经成为主要有效路径。** 最新完成的 ASDF 和 Rich 修复都在一次标准
   构建内成功，不需要 Agent 或 LLM。
2. **Agent 链路稳定性、安全性和成本控制明显增强，但真实修复成功仍为 0。** 当前没有证据
   表明 LLM Agent 已经成功救回一个标准路径失败的真实项目。
3. **最新同口径完整 21 项评测仍只有 2 项最终成功；之后 ASDF、Rich 已分别独立成功。** 按
   每个项目的最新已验证结果拼接为 4/21，但尚未在当前最终代码哈希下重新完整复跑 21 项，
   因此不能把 4/21 当成正式的统一全量成功率。

## 2. 本会话完成的全部主要操作

### 2.1 项目审计与实施计划

- 阅读已有总结、README、方案文档和主要代码目录；
- 对照 CNB、CXXCrafter、HerAgent-main，确认可复用的方向是：规则优先解析、确定性构建、
  失败后有限 Agent 修复、不可变 Artifact、状态持久化和分层验证；
- 明确不直接复制其他项目的宽权限 shell Agent 或无边界上下文，而是保留 DPRAuto 的
  端口/适配器分层、安全策略和可审计产物；
- 形成并逐步执行“事务一致性与安全 -> 网络与工具层 -> 测试选择 -> SCM 版本 -> 真实项目
  闭环”的修改顺序。

### 2.2 修复候选事务、恢复和评测身份

- 新增隔离的 repair candidate 工作区；候选修改只有通过比较并被接受后才原子提升到已接受
  工作区，拒绝候选会被清理；
- 原子提升检查相对路径、symlink、文件类型和 SHA-256，失败时恢复备份；
- checkpoint 保存 pending candidate 和已接受的 profile/plan/build/verification 状态，支持
  中断后恢复而不污染源目录；
- 为评测记录增加身份摘要，覆盖测试 case、源码树、源码 revision、实现哈希和策略配置；
- 只有身份完全匹配的历史记录才跳过；实现、策略或源码变化时记录为 stale 并重新执行；
- 评测 workspace 是源码副本，不允许 Agent 修改原始 benchmark 仓库。

相关代码：

- `src/dprauto/agent/candidates.py`
- `src/dprauto/agent/models.py`
- `src/dprauto/adapters/persistence/sqlite.py`
- `evaluations/prompt12/run_evaluation.py`

### 2.3 Agent 修复安全和质量门控

- 增加结构化修复工具：系统包、Python 依赖和基础镜像分别使用有界字面参数；
- 拒绝 URL、shell 片段、包管理器选项、`latest` 镜像和不精确的 FROM 替换；
- 每轮只允许改变一个高风险环境维度，实际 diff 再检查 runtime、system packages、Python
  dependencies 和 startup，防止一次修复同时大改多个维度；
- 对 `setup.sh` 使用 `sh/bash -n`，对 Dockerfile 使用
  `docker build --check --network none`；预检只检查语法和解析，不执行 RUN；
- Docker 不可用、镜像解析网络失败或预检超时时标记 inconclusive，由正式构建继续裁决，
  只有明确语法错误才提前拒绝；
- 完整保存每轮 unified diff、`environment-diff.json` 和 `repair-preflight.json`；
- 改进失败分类，确保日志末尾明确的依赖、编译或测试错误不会被前面已经恢复的网络重试覆盖。

相关代码：

- `src/dprauto/agent/tools/structured.py`
- `src/dprauto/adapters/preflight/docker.py`
- `src/dprauto/adapters/failure/rules.py`
- `src/dprauto/adapters/llm/repair.py`
- `src/dprauto/agent/workflow.py`

### 2.4 构建与验证网络拆分

- 实测发现宿主默认 Docker bridge 无法稳定出站；
- 尝试把 BuildKit 直接放入普通自定义 bridge 失败，因为 Docker build 的逐构建 network 只接受
  `default`、`none`、`host`；该 `post-custom-network-canary` 实验没有生成有效 summary；
- 最终使用拆分网络：Docker/BuildKit 构建使用 `host`，验证容器使用幂等创建的
  `dprauto-eval` 用户自定义 bridge；
- 不重启 Docker daemon，不修改全局 builder；
- 增加宿主代理转发：只在命令和计划中记录 `--build-arg HTTP_PROXY`、`--env HTTPS_PROXY`
  等变量名，不持久化代理 URL；禁网或关闭配置时清空 Pack 隐式代理继承；
- ASDF 实测中，代理转发把标准构建从下载 NumPy 300 秒超时推进为约 49 秒成功。

相关代码：

- `src/dprauto/proxy.py`
- `src/dprauto/strategies/docker.py`
- `src/dprauto/strategies/template.py`
- `src/dprauto/strategies/cnb.py`
- `src/dprauto/adapters/verification/docker.py`
- `evaluations/prompt12/run_evaluation.py`

### 2.5 Poetry 工具镜像和依赖范围

- 固定 Poetry 版本，按 Python 版本构建
  `dprauto-tools/python-poetry:python-<version>-poetry-<version>` 工具镜像；
- 工具镜像在项目计时前按配方 SHA-256 校验、构建和复用，预检日志写入 `preflight/`；
- Poetry 项目不再逐项目重复安装 Poetry；
- 标准模板构建只安装运行时依赖：Poetry `--only main`、uv 禁用默认开发组、PDM `--prod`；
- test/dev requirements、extras 和包管理器测试组推迟到 Testability 临时容器；
- BuildKit cache mount 和验证 Docker volume 只缓存包下载/构建缓存，不缓存项目虚拟环境或
  最终镜像内容；
- Poetry 锁文件先执行 `poetry check --lock`，只有不一致时才在镜像内部做不升级已锁版本的
  refresh；
- VCS dependency 和 `pyscard` 等确定性系统依赖使用明确证据和白名单，不猜测通用
  `build-essential`。

真实 canary 结果：4 个样本标准构建 4/4，最终成功 2/4，另外 2 项进入真实测试失败；
Poetry 工具层本身没有引入 runner error。

### 2.6 测试命令和 matrix 选择

- 静态解析 tox `envlist`/`env_list`、brace factor、nox session/Python 参数和简单 CI matrix；
- 不执行目标仓库的配置代码；展开最多 12 个字面组合；
- 优先选择与构建 Python 版本一致的安全 unit 环境；排除 lint、docs、format、typecheck、
  fuzz、benchmark、集成、远程服务和发布环境；
- matrix runner 只临时安装 tox/nox，由被选环境安装自己的依赖；
- 对直接 pytest/unittest 只选择一个最窄测试依赖来源，避免同时安装
  `requirements-dev.txt`、`.[tests]`、test group 和多个 runner；
- ASDF 的验证依赖由“dev requirements + test extra + runner”收敛为 `.[tests] + runner`，
  消除了三条 Git main 分支以及开发版 NumPy/SciPy 的无效扩张。

相关代码：

- `src/dprauto/adapters/python/test_matrix.py`
- `src/dprauto/inspection/commands.py`
- `src/dprauto/verification/commands.py`
- `src/dprauto/verification/testability.py`

### 2.7 固定测试 runner 与活动超时门控

- 固定临时 runner：`pytest==8.3.5`、`tox==4.23.2`、`nox==2024.10.9`；
- 固定 `pytest-xdist==3.6.1`；所有版本都可通过配置覆盖，但必须是数字开头的固定版本；
- runner pin 与项目测试依赖合并给 resolver，避免两次独立安装互相覆盖；
- ASDF 中，这一步消除了 pytest 9.1.1 对 generator-based parametrize 的移除警告失败；
- Testability 命令实际超时且日志仍有 pytest/unittest/TAP 测试进度时，直接判为验证预算问题，
  不进入 Agent；普通下载百分比不会误判为测试进度；
- 活动下载超时、基础设施网络错误、模板安装超时和纯测试断言失败也有对应的 Agent 资格门控，
  避免 LLM 修改无关构建脚本。

### 2.8 有 CI 证据的有界并行测试

- 只有项目同时声明 pytest-xdist、`--numprocesses`、tox parallel factor，并且 CI 实际使用
  对应 factor 时，直接 pytest 才复用并行策略；
- `auto` 不直接透传，受 `DPRAUTO_VERIFICATION_MAX_PARALLEL_TEST_WORKERS` 限制，默认 4；
- 已有并行参数、复合 shell 控制符或证据不完整时不重写；
- ASDF 完整 1812 项测试从串行 90 秒只到约 35%，推进为 4 worker 下 37.59 秒执行完毕；
- 完整执行后隔离出唯一真实失败：缺少 Git 标签时版本被回退成 `0.0+g...`，低于测试使用的
  内建扩展版本。

### 2.9 setuptools-scm 缺失 VCS 元数据恢复

第一阶段：

- 只有 manifest 明确配置 setuptools-scm、声明合法项目名，并且 SourceReference 有 7–64 位
  十六进制 revision 时才启用；
- 只设置项目专属的
  `SETUPTOOLS_SCM_PRETEND_VERSION_FOR_<NORMALIZED_PROJECT_NAME>`，不设置会影响依赖包的
  全局变量；
- 最初用 `0.0+g<revision>` 解决了“无法检测版本”的构建失败，但 ASDF 全量测试证明该版本
  语义仍不正确。

第二阶段增加严格源码版本证据优先级：

1. `[tool.setuptools_scm]` 中配置的 `version_file` 或旧 `write_to` 生成文件；
2. 根目录 `PKG-INFO: Version`；
3. 标准根目录 changelog 的第一个数字版本标题，且必须明确标记 unreleased/development；
4. 都没有时才回退 `0.0+g<revision>`。

生成文件和 PKG-INFO 保留精确版本；未发布 changelog 基线生成
`<version>.dev0+g<12位revision>`。较后的版本标题、文档示例、已发布的首标题、非标准路径和
不安全相对路径均不会成为证据。

ASDF 源码 `CHANGES.rst` 的首标题为 `3.3.0 (unreleased)`，最终生成并安装：

```text
asdf-3.3.0.dev0+g77d2bba699ac
```

相关代码：

- `src/dprauto/adapters/python/parser.py`
- `src/dprauto/strategies/template.py`

### 2.10 跨用途命令去重与 Rich 修复

- Rich 含 `rich/__main__.py`，解析器正确推导出 run 命令 `python -m rich`；
- 多语言 README 的安装章节也出现相同文本，并被高置信度标为 install；
- 旧逻辑只按命令文本全局去重，install 记录覆盖了 run 记录，Runnability 因而执行 Python
  基础镜像默认命令，退出 0 但没有输出；
- 去重 key 改成“规范化文本 + CommandPurpose”，同一用途内仍去重，不同用途的独立证据
  不互相覆盖；
- Rich 真实复测正确执行 `python -m rich`，输出 8,782 字节，不需要 `--help` fallback。

相关代码：

- `src/dprauto/adapters/python/parser.py`
- `tests/test_python_parser.py`

## 3. 真实评测结果

### 3.1 三次同范围 21 项完整评测

范围均为 manifest 索引 4–24。外部网络、Docker cache 和实现哈希不同，因此这些是工程运行
对比，不是严格隔离 A/B。

| 完整评测 | 标准构建成功 | 最终成功 | Agent 进入 | Agent 修复成功 | Testability 通过 | LLM 调用 |
|---|---:|---:|---:|---:|---:|---:|
| `post-apt-dns-bounded-full21-20260818` | 12/21 | 2/21 | 16 | 0 | 2 | 44 |
| `full-post-poetry-tool-layer-20260820` | 10/21 | 2/21 | 18 | 0 | 2 | 49 |
| `post-test-matrix-full21-20260821` | 8/21 | 2/21 | 6 | 0 | 3 | 14 |

最新完整评测中标准构建降至 8/21，主要与实时网络和 300 秒下载预算有关：标准构建 p50、p95
都约为 300 秒，11 项最终为 `time_budget_exceeded`。不能据此断言测试 matrix 修改导致构建
能力回归，因为多数项目没有进入 Testability。

### 3.2 最新完整 21 项基线

结果目录：`evaluations/prompt14/runs/post-test-matrix-full21-20260821`

核心指标：

- 标准构建成功：8/21（38.10%）；失败或超时：13/21；
- 最终环境成功：2/21（9.52%）；最终失败：19/21；
- 成功项目：`python-markdown/markdown`、`karpathy/minbpe`；
- Agent 进入：6 项；Agent repair success：0；
- Installability / Testability / Runnability 通过：8 / 3 / 6；
- 终态：2 succeeded、6 verification_failed、11 time_budget_exceeded、
  1 infrastructure_failed、1 project_failed；
- 失败类别：8 build_command、5 test、3 network、1 docker、1 build_tool、1 run；
- 总耗时：4714.414 秒（约 78.6 分钟）；
- LLM：14 次调用、3 次错误、81,258 tokens；
- runner errors：0。

逐项目结果：

| 索引 | 项目 | 标准构建 | 完整基线终态 | 后续最新已验证终态 |
|---:|---|---|---|---|
| 4 | `tmux-python/tmuxp` | succeeded | verification_failed/test | 未复测，仍按失败 |
| 5 | `Textualize/rich` | succeeded | verification_failed/run | **succeeded（独立复测）** |
| 6 | `encode/starlette` | timed_out | time_budget_exceeded | 未复测 |
| 7 | `mandarons/icloud-drive-docker` | failed | infrastructure_failed/docker | 未复测 |
| 8 | `simplistix/testfixtures` | succeeded | verification_failed/test | 未复测 |
| 9 | `piccolo-orm/piccolo` | succeeded | verification_failed/test | 未复测 |
| 10 | `boto/boto3` | timed_out | time_budget_exceeded | 未复测 |
| 11 | `andreidrang/python-rucaptcha` | timed_out | time_budget_exceeded | 未复测 |
| 12 | `artesiawater/hydropandas` | timed_out | time_budget_exceeded | 未复测 |
| 13 | `asdf-format/asdf` | failed | project_failed/build_tool | **succeeded（独立复测）** |
| 14 | `compserv/hknweb` | succeeded | verification_failed/test | 未复测 |
| 15 | `dagshub/client` | timed_out | time_budget_exceeded | 未复测 |
| 16 | `gamesdonequick/donation-tracker` | timed_out | time_budget_exceeded | 未复测 |
| 17 | `python-markdown/markdown` | succeeded | succeeded | succeeded |
| 18 | `yubico/yubikey-manager` | timed_out | time_budget_exceeded/network | 未复测 |
| 19 | `psf/black` | timed_out | time_budget_exceeded/network | 未复测 |
| 20 | `ranaroussi/yfinance` | timed_out | time_budget_exceeded | 未复测 |
| 21 | `soimort/you-get` | succeeded | verification_failed/test | 未复测 |
| 22 | `tiangolo/fastapi` | timed_out | time_budget_exceeded/network | 未复测 |
| 23 | `karpathy/minbpe` | succeeded | succeeded | succeeded |
| 24 | `alexmolas/microsearch` | timed_out | time_budget_exceeded | 未复测 |

### 3.3 ASDF 索引 13 的逐步复测

ASDF 是本轮最完整的“逐层消除阻塞”案例：

1. revision-only SCM pretend version：解决无法检测版本，但运行依赖下载仍超时；
2. 代理按名称转发：标准构建成功；
3. 单一测试依赖来源：测试开始收集，隔离出 pytest 最新版不兼容；
4. 固定 runner：消除 pytest 9.1.1 兼容失败；
5. 活动测试超时门控：90 秒有进度超时不再进入 LLM；
6. CI 确认的 4 worker xdist：1812 项在预算内跑完，隔离出唯一 SCM 版本语义失败；
7. changelog 版本证据：完整成功。

最终结果目录：
`evaluations/prompt14/runs/post-scm-version-evidence-asdf-13-20260821`

最终指标：

- 标准构建和三层验证全部通过；
- 1796 passed、14 skipped、2 xfailed、0 failed；pytest 39.70 秒；
- Testability 命令 77.812 秒；标准构建命令 54.200 秒；
- 总耗时 136.746 秒；
- 1 次构建，Agent false，repair 0，LLM 0，runner error 0；
- 当时实现哈希：
  `f2963d3ce1150dc27a97a6fbaf90e46cc772efe0afe7fc15ae001631d670f1cb`。

### 3.4 Rich 索引 5 的独立复测

结果目录：
`evaluations/prompt14/runs/post-purpose-aware-command-dedup-rich-05-20260821`

最终指标：

- 正确运行 `python -m rich`，输出 8,782 字节；
- 836 passed、23 skipped、0 failed；pytest 28.82 秒；
- 标准构建 7.156 秒，Testability 59.557 秒，Runnability 2.906 秒；
- 三层验证全部通过，总耗时 70.535 秒；
- 1 次构建，Agent false，repair 0，LLM 0，runner error 0；
- 实现哈希：
  `96dc20f0a0013fd5ed345a96913faa51d547c532a5d2e0fe208e59a2c17fae0d`。

### 3.5 当前“最新已验证记录”拼接视图

如果保留最新完整评测中未复测项目的结果，仅用后续 ASDF 和 Rich 成功记录替换对应两项，
则得到：

- 标准构建成功：9/21（42.86%）；失败/超时：12/21；
- 最终成功：4/21（19.05%）；最终失败：17/21；
- 成功项目：Rich、ASDF、python-markdown、minbpe；
- 剩余终态：5 verification_failed、11 time_budget_exceeded、1 infrastructure_failed；
- Agent repair success 仍为 0。

**重要限制：**这是跨实现哈希、跨时间和跨网络状态的 last-known 拼接视图，不是一次完整评测。
正式对外报告当前最终成功率前，必须用当前代码和相同索引 4–24 重新完整运行。

## 4. 当前自动构建 Agent 的成功与失败判断

### 4.1 已经成功的能力

- 项目解析、策略选择、Docker/Template 构建、Artifact 保存和分层验证链路可真实运行；
- 确定性策略已经让 4 个样本获得最新完整成功证据；
- ASDF 证明复杂项目可在固定 90 秒 Testability 预算内完成 1812 项测试；
- Rich 证明 parser/运行验证可使用项目自有模块入口，而不是基础镜像默认命令；
- 网络、Docker infrastructure、项目测试失败和预算耗尽能够分开终止；
- 活动下载/测试超时不再浪费 LLM；
- 最新完整评测的 Agent 参与从之前 16–18 项降到 6 项，LLM 调用降到 14 次；
- runner error 始终为 0，评测记录有实现/源码/策略身份校验。

### 4.2 仍然失败或未证明的能力

- **Agent 真实救回项目仍为 0。** 当前成功改善全部来自确定性解析、构建和验证策略；
- 11 个项目在最新完整评测中耗尽 300 秒构建预算，主要是 GitHub clone、pip 下载、依赖解析、
  大 wheel 或系统包下载；
- 5 个未复测项目仍是 Testability 失败，需要逐项区分测试工具缺失、错误测试命令、外部服务、
  Python 版本不兼容和真实项目断言失败；
- `icloud-drive-docker` 是 Docker/platform 基础设施失败，不应交给 Agent 修改项目；
- 当前 4/21 只是拼接视图，尚缺一次最终实现的统一全量评测；
- 当前网络和缓存影响很大，同范围标准构建曾在 8/21 到 12/21 间波动；
- LLM 修复计划仍可能触发高风险维度拒绝或没有有效 diff，安全门控正确，但修复质量没有得到
  成功样本证明。

## 5. 自动化测试和代码完整性

本轮最终回归：

- `PYTHONPATH=src python3 -m pytest -q`：220 passed，73 subtests passed；
- `PYTHONPATH=src python3 -m unittest discover -s tests -q`：216 tests，OK；
- 现有 1 个 `PytestCollectionWarning`：`TestabilityVerifier` 类名以 Test 开头且自定义
  `__init__`，不影响测试结果；
- 曾并行运行 pytest/unittest 时，unittest 清理了 pytest 正在使用的同名临时 Docker 镜像，
  导致一个 Docker 集成用例 30 秒拉取超时；该用例单独重跑通过，随后串行完整 pytest 通过。
  后续应继续串行运行这两套包含 Docker 生命周期操作的回归；
- 最后检查时无残留带 `dprauto` label 的容器；
- 当前实现摘要与 Rich 评估记录一致：
  `96dc20f0a0013fd5ed345a96913faa51d547c532a5d2e0fe208e59a2c17fae0d`。

## 6. 当前环境和仓库注意事项

- 工作区：`/home/master/dprauto`；
- 真实项目源：`/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos`；
- 评测 harness：`evaluations/prompt12/run_evaluation.py`；
- manifest：`evaluations/prompt12/manifest.json`；
- 当前日期环境为 2026-08-24，时区 Asia/Shanghai；
- Docker 构建网络使用 `host`，验证网络使用 `dprauto-eval`；
- Poetry 固定版本为 1.8.5；当前预检使用 Python 3.9/3.11 工具镜像；
- Build timeout 300 秒，Verification command timeout 90 秒，Agent 总预算 900 秒，最大修复轮数 2；
- 默认最多 4 个测试 worker；pytest 8.3.5、pytest-xdist 3.6.1、tox 4.23.2、nox 2024.10.9；
- Git 会因目录所有权报告 `dubious ownership`，只读操作可使用
  `git -c safe.directory=/home/master/dprauto ...`，不要修改 `/home/master/.git`；
- 当前 Git 仓库只有 `README.md` 被 HEAD 跟踪，`src/`、`tests/`、`evaluations/` 等大部分工作
  都显示为 untracked。这是现有仓库状态，**不要执行 `git clean`、reset 或 checkout 清理**；
- 工作区已有用户文件和大量评估 Artifact，应保留，不要做递归删除。

## 7. 关键结果目录

- 最近完整 21 项：
  `evaluations/prompt14/runs/post-test-matrix-full21-20260821`
- 4 项 warm-cache matrix 复测：
  `evaluations/prompt14/runs/post-test-matrix-warm-05131924-20260821`
- ASDF 最终成功：
  `evaluations/prompt14/runs/post-scm-version-evidence-asdf-13-20260821`
- Rich 最终成功：
  `evaluations/prompt14/runs/post-purpose-aware-command-dedup-rich-05-20260821`
- Poetry 工具层完整 21 项：
  `evaluations/prompt14/runs/full-post-poetry-tool-layer-20260820`
- 网络拆分 canary：
  `evaluations/prompt14/runs/post-split-network-canary-20260819`
- 事务身份 canary：
  `evaluations/prompt14/runs/post-transaction-identity-canary-20260819`
- 8 月 18 日较早完整基线：
  `evaluations/prompt14/runs/post-apt-dns-bounded-full21-20260818`

单项分析报告：

- `evaluations/prompt14/runs/post-test-dependency-selection-asdf-13-20260821/FINDINGS.md`
- `evaluations/prompt14/runs/post-pinned-test-runners-asdf-13-20260821/FINDINGS.md`
- `evaluations/prompt14/runs/post-active-test-timeout-gate-asdf-13-20260821/FINDINGS.md`
- `evaluations/prompt14/runs/post-ci-confirmed-parallel-tests-asdf-13-20260821/FINDINGS.md`
- `evaluations/prompt14/runs/post-scm-version-evidence-asdf-13-20260821/FINDINGS.md`
- `evaluations/prompt14/runs/post-purpose-aware-command-dedup-rich-05-20260821/FINDINGS.md`

## 8. 建议的后续顺序

1. 用当前代码、相同 manifest 索引 4–24、相同 300/90/900 秒策略执行一次完整 21 项复跑，
   得到统一实现哈希下的正式成功率；
2. 在长跑前先复测 5 个仍为 verification_failed 的项目：tmuxp、testfixtures、piccolo、
   hknweb、you-get，逐项记录所选命令、依赖来源和最小失败证据；
3. 对 11 个 build timeout 按阶段聚类：Git clone、pip 下载、resolver、大 wheel、apt；优先做
   阶段级低速/超时检测，不要恢复粗粒度 90 秒整构建误杀；
4. 针对 boto3 的 VCS requirements，研究 lock、发布版依赖或明确浅克隆证据，但不要无依据
   改写项目依赖；
5. 继续以 Agent repair success 为独立指标；确定性策略成功不能计作 Agent 修复成功；
6. 若进入 Agent 质量优化，先分析现有 0 成功案例的 rejected diff 和 preflight 结果，再调整
   prompt 或工具，不要放宽业务源码、任意 shell、网络和多维环境修改边界。

## 9. 最终交接判断

DPRAuto 当前已经不是“只有框架”的原型：项目解析、确定性环境构建、真实 Docker 执行、
分层验证、失败分类、事务化 Agent 候选、持久化、时间预算和 Artifact 审计均已落地。最近两项
复杂失败 ASDF 和 Rich 已通过确定性改进转为完整成功，证明继续优先消除可规则化失败是有效的。

但项目仍不能宣称 Agent 修复有效：统一完整评测的最终成功仍是 2/21，拼接后的最新证据是
4/21，而 Agent repair success 仍为 0。下一阶段的关键不是放宽 Agent 权限，而是先完成当前
代码的统一全量复跑，再集中解决 5 个 Testability 失败和 11 个构建阶段网络/预算失败，并用
真实“标准失败 -> Agent 修复后成功”样本证明 Agent 的增量价值。

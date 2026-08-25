# DPRAuto 当前对话完整工作与结果交接总结

更新时间：2026-08-25（Asia/Shanghai）

## 1. 文档目的与范围

本文用于把本次连续对话中对 `/home/master/dprauto` 完成的审计、方案、代码实施、真实项目
评测、Git 提交、当前结论和下一步工作完整交接给后续维护者。

本轮开始时阅读并继承了以下资料：

- `HANDOFF_SUMMARY_2026-08-13.md`
- `CONVERSATION_PROGRESS_SUMMARY_2026-08-18.md`
- `CONVERSATION_HANDOFF_SUMMARY_2026-08-24.md`
- `scheme.md`

同时对比了 `/home/master/auto-build` 下三个参考项目：

- `CNB`：真实项目 benchmark、历史构建数据、确定性构建路径和样本源码；
- `CXXCrafter`：环境构建、修复和工程组织参考；
- `HerAgent-main`：Agent 状态机、工具调用和修复循环参考。

用户的总目标始终是：**项目到达后自动构建；如果构建或环境验证失败，调用 LLM 自主调查
原因、尝试修改环境构建配置，并通过真实重建和验证确认修复。**

用户还明确要求：除 `/home/master/dprauto/myapi.json` 外，每完成一个独立部分就提交一次 Git，
commit 摘要和正文要说明本次完成内容与测试结果。本轮遵守了该要求，没有修改或提交
`myapi.json`，也没有 push。

## 2. 最重要的当前结论

### 2.1 当前正式成功率

最新一次同一实现身份、同一策略、索引 4–24 的 21 项全量评测是 M9：

- 标准构建成功：19/21，**90.48%**；
- 最终环境成功：8/21，**38.10%**；
- Installability passed：20；
- Testability passed：7，policy skipped：1；
- Runnability passed：14；
- Agent 进入：10；
- Agent 修复成功：1/10，**10%**；
- runner error：0。

M10 后 Boto3 又获得独立最终成功，因此按最新已验证项目拼接为 9/21，即 **42.86%**。但 M10
代码尚未重新跑统一身份的 21 项全量，所以正式对外成功率仍应写 **38.10%**，42.86% 只能写成
局部回归后的预估值。

### 2.2 Agent 是否能在报错后自己查原因、修改并验证

**可以，当前已形成真实闭环，并有一个真实 Agent 修复成功案例。**

当前生产图为：

```text
项目解析
→ 标准构建
→ 分层验证
→ 失败分类/资格门控
→ LLM 只读调查（读文件、分页、搜索）
→ 证据化诊断
→ 最小修复计划
→ 隔离候选工作区修改
→ preflight
→ 重建或 Testability overlay 复验
→ Installability/Testability/Runnability
→ regression comparison
→ 接受候选或恢复/拒绝
```

YubiKey Manager 是首个完整证据链案例：初始 Runnability 需要不存在的 `cryptography.txt`；
Agent 读取项目和 CI 后只修改 Dockerfile，导出 Poetry requirements 并生成该文件；候选 preflight、
重建、三层验证和 regression 全部通过，业务源码未修改。

### 2.3 LLM 是否总能拿到完整文件进行修改

**常见 Dockerfile/setup.sh 基本可以，但目前不能保证任意大型文件完整进入最终修复上下文。**

已经具备：

- `read_file` 每页最多 400 行；
- 支持 `start_line/end_line`；
- 返回 `total_lines/truncated/next_start_line`；
- LLM contract 明确要求 `truncated=true` 时继续读取下一页；
- 结构化 patch 工具优先，尽量避免整文件重写。

仍存在的明确上限：

- 单文件读取上限 256 KiB；
- 默认最多 3 个调查轮次、6 个调查动作；
- EvidencePack 默认最多 10000 字符；
- LLM 总上下文最多 24000 字符；
- `current_build_scripts` 初始最多约 8000 字符，超限时会进一步压缩；
- `modify_build_script` 要求 whole-file replacement，但当前没有硬校验证明所有分页已经读完后
  才允许整文件替换。

因此“Agent 拿不到任何源码”的旧问题已经改善，但“任意大文件完整读取后才允许 whole-file
replacement”仍应作为后续 M11 的安全/正确性缺口处理。

## 3. 初始审计发现的主要缺陷

结合三个参考项目和旧版 DPRAuto，最初确认的关键问题包括：

1. 标准构建确定性不足，太多可规则化问题过早进入 LLM；
2. Docker/网络、代理、包下载和验证容器网络没有正确分层；
3. Testability 过严，容易直接执行全量 tox/nox/docs/lint 或外部服务测试；
4. 大项目测试和依赖安装共享粗粒度超时，导致大量假失败；
5. LLM HTTP 请求超过配置 timeout 后仍可能阻塞，没有真实 wall-clock kill；
6. Agent 虽调用过 `read_file`，但分页、行号、truncation 和关键证据在上下文压缩中丢失；
7. 诊断是自由文本，缺少真实 evidence ref，LLM 能基于错误猜测添加依赖；
8. 修复工具边界过粗，整文件改写、多维环境变化和无因果 overlay 风险较高；
9. 修复候选、回滚、checkpoint 和评测身份不够严格，可能混用旧记录或污染工作区；
10. 测试 runner 固定版本会覆盖项目自己的 pytest pin/hash-lock；
11. PDM 无 lockfile 项目仍无条件执行 `pdm sync`；
12. pytest collection/import 错误会被 `short test summary info` 误认为业务 assertion，从而跳过 Agent。

## 4. M1–M4：基础构建、事务、安全和验证框架

这些阶段主要由提交 `d8dcb2b` 汇总落地，旧交接文档有更详细的逐文件说明。完成的核心能力：

- 规则优先的 Python 项目解析和确定性 build strategy portfolio；
- Template、Docker、CNB 构建路径；
- 不可变 Artifact、命令日志和环境 diff；
- Installability/Testability/Runnability 分层验证；
- repair candidate 隔离工作区、接受/拒绝、原子提升和回滚；
- checkpoint 持久化和恢复；
- 评测 identity 覆盖实现、源码 revision、策略和 case；
- 结构化系统包/Python 依赖/基础镜像修改工具；
- Dockerfile/setup.sh 本地 preflight；
- 每轮只允许改变一个高风险环境维度；
- Docker 构建网络使用 host，验证使用 `dprauto-eval` 自定义 bridge；
- 代理只按变量名转发，不持久化代理值；
- 固定 Poetry 工具镜像和 BuildKit/package cache；
- tox/nox/pytest 测试 matrix 解析；
- setuptools-scm 缺失 `.git` 元数据的版本恢复；
- Rich 跨用途命令去重修复。

这一阶段把项目从“只有框架”推进为可真实 Docker 构建、验证、失败分类并有限自动修复的系统。

## 5. M5：有界 Testability 与 required-secret policy

相关提交：

- `ff1a1d6`：记录 M5 Testability 失败簇；
- `91b3e99`：记录 bounded Testability evidence；
- `06cc81b`：将 Testability 限制为本地代表性测试；
- `efeb0f5`：policy-skipped 测试不进入 Agent；
- `bb38624`：记录 M5 代表验证结果。

主要修改：

- 对直接 pytest/unittest 从安全本地测试文件中最多选择 8 个代表文件；
- 跨测试子目录轮转，避免全部取自同一目录；
- 排除 integration/e2e/remote/live/slow/benchmark 等路径和明显联网测试；
- 静态识别模块级 required secret；没有真实 secret 时以 policy skip 结束，不伪造密钥；
- 保存原始命令、实际命令、selection kind/reason/targets 等可审计 metadata。

真实结果：

- Microsearch：最终成功，0 LLM；
- RuCaptcha：识别 `RUCAPTCHA_KEY`，Testability policy skipped，最终成功，0 LLM；
- Boto3：从 51 个测试文件识别 30 个安全本地文件并生成 8 文件切片；
- you-get：两文件切片在 9 秒内暴露 pytest capture 与项目 stdio 重绑定的真实兼容错误。

M5 同时暴露：LLM 请求超过 120 秒后仍阻塞，必须人工中止。

## 6. M6：LLM 硬 wall-clock deadline 和故障转移

相关提交：

- `30cd9ce`：生产 LLM 请求硬截止；
- `532be87`：限制 timeout failover；
- `712f32f`：保留健康模型优先级；
- `13334e9`、`20e82e4`：记录 M6 验证和传输尝试限制。

主要修改：

- HTTP 请求在独立 spawn 子进程执行；
- 父进程以单请求 timeout 和工作流剩余 deadline 的较小值做绝对墙钟上限；
- 超时后 terminate/kill worker；
- 单响应最大 8 MiB；
- 正常、HTTP、socket、硬回收全部写 JSONL 审计，API key 不入日志；
- 默认每个 operation 最多 2 次 timeout transport attempt；
- 模型超时后优先切换不同模型；成功 fallback 成为后续首选。

you-get canary：

- 自然结束为 `verification_failed`；
- 总耗时 758.07 秒，小于 Agent 900 秒预算；
- 12 次 LLM、2 次 120 秒硬超时；
- 2 个修复轮次；
- 0 runner error；
- 不再需要人工 Ctrl-C。

M6 解决了失控等待，没有解决 LLM 诊断质量。

## 7. M7：完整证据分页、诊断 contract 和因果修复门禁

相关提交：

- `cde08d9`：固化 M7 失败基线；
- `e0210fa`：保留可行动源码证据和分页元数据；
- `07891c4`：保护 pytest/xdist/tox/nox 等受管 runner；
- `f615b3f`：Testability overlay 必须有直接因果证据；
- `b88e995`：识别 stdio rewrap 并为直接 pytest 增加 `-s`；
- `b01f0d7`：保存 pytest capture compatibility trace；
- `27ca31c`：允许 checkpoint 反序列化 `BuildStrategyAttempt`；
- `5d32339`：LLM diagnosis 强制 evidence-backed claims；
- `e3b3600`：记录 M7 结果。

主要修改：

- `read_file` 支持 400 行分页和 `next_start_line`；
- evidence context 保存路径、行号、total/truncated 信息；
- 普通源码 observation 不再冒充 build script；
- LLM 每条诊断 claim 必须引用真实 `failure:*`、`observation:*`、`path:*` 或 `profile:*`；
- 缺失或虚构 evidence ref 只允许一次 contract correction；
- overlay 只能在缺模块、缺插件、distribution/version conflict 等直接证据下修改；
- 日志已经说明 requirement satisfied 时拒绝重复加包；
- overlay 不得覆盖 DPRAuto 管理的 pytest、pytest-xdist、tox、nox；
- 确定性识别模块级 `sys.stdout/sys.stderr` buffer 重包裹，直接 pytest 增加 `-s`。

you-get 最终 canary：

- standard/final：succeeded/succeeded；
- Installability/Testability/Runnability 全 passed；
- Agent false、repair 0、LLM 0；
- 12.81 秒；
- 实际执行两文件切片并加 `-s`，不是跳过测试。

这证明“报错证据可进入调查链路”和“完整分页能力”已落地，但完整文件仍受第 2.3 节中的上下文
与动作上限约束。

## 8. M8：代表性多项目回归与 parser/persistence/metrics 修复

相关提交：

- `4c7b33d`：保留 TOML section 最后一个 Python test extra；
- `7dca839`：持久化 EvidenceRecord/EvidencePack 可反序列化；
- `5fce680`：evaluation metrics 分离 policy skip；
- `a3b2bf1`：记录 M8 代表性回归。

发现与修复：

1. `_test_group_names` 的 EOF lookahead 不能识别 TOML 最后一个数组，ASDF 的 `tests` extra 被
   丢失；同时要支持 `fsspec[http]` 这种嵌套 brackets；
2. EvidenceRecord/EvidencePack 未在 checkpoint allowlist，恢复时产生 blocked-deserialization；
3. Testability `skipped` 被错误计为 build-success-test-failure。

最终代表集：Rich、RuCaptcha、ASDF、Markdown、Microsearch，5/5 最终成功；standard 5/5；
Agent/LLM 0；runner error 0。

当时全量自动测试：355 cases/subtests，0 failures/errors。

## 9. M9：当前实现 21 项统一全量回归

报告：`evaluations/prompt14/M9_FULL21_CURRENT_RESULTS_2026-08-24.md`

运行目录：`evaluations/prompt14/runs/m9-full21-current-20260824`

命令：

```bash
PYTHONPATH=src python3 evaluations/prompt12/run_evaluation.py \
  --output evaluations/prompt14/runs/m9-full21-current-20260824 \
  --indices 4-24
```

统一实现摘要：
`c4fb2b13c635072211b5f525fb9ed5725db36beec0b53aaac96e5567fb423c36`

结果：

| 指标 | M9 | 旧完整基线 `post-test-matrix-full21-20260821` |
| --- | ---: | ---: |
| 标准构建成功 | 19/21（90.48%） | 8/21（38.10%） |
| 最终环境成功 | 8/21（38.10%） | 2/21（9.52%） |
| Installability passed | 20 | 8 |
| Testability passed | 7 | 3 |
| Testability policy skipped | 1 | 未分离 |
| Runnability passed | 14 | 6 |
| Agent 修复成功 | 1 | 0 |
| time budget exceeded | 1 | 11 |
| runner error | 0 | — |

成功项目：

- Textualize/rich
- andreidrang/python-rucaptcha
- asdf-format/asdf
- python-markdown/markdown
- yubico/yubikey-manager
- soimort/you-get
- karpathy/minbpe
- alexmolas/microsearch

成本与安全：

- 10 个项目进入 Agent，1 个修复成功；
- 79 次 LLM；
- input 388357、output 66609、总 tokens 454966；
- LLM API 累计 2497.39 秒；
- 0 regression、0 duplicate repair plan、0 runner error；
- 单项目最长 631.43 秒，没有再次超过 900 秒后失控运行。

剩余失败主要是测试依赖/依赖组不完整、系统 executable/服务/Django settings、宽 dev 安装超时、
PDM 无锁、Docker platform infrastructure。

## 10. 首个真实 Agent 修复成功：YubiKey Manager

初始失败：CI 推导的 Runnability 命令引用不存在的 `cryptography.txt`：

```text
poetry run pip download -r cryptography.txt --platform macosx_10_12_universal2 \
  --only-binary :all: --no-deps --dest .
```

Agent 行为：

- 读取项目和 CI；
- 仅修改允许范围内的 Dockerfile；
- 添加 Poetry export 并 grep 生成 `cryptography.txt`；
- preflight passed；
- 镜像重建 succeeded；
- Installability/Testability/Runnability passed；
- regression passed；
- 无业务源码变化。

记录为 `agent_participated=true`、`repair_attempts=1`，这是当前唯一统一全量中的真实 Agent
repair success。

## 11. M10：测试依赖契约、PDM、超时和 Agent gate

报告：`evaluations/prompt14/M10_DEPENDENCY_CONTRACT_RESULTS_2026-08-24.md`

### 11.1 保留项目 pytest 约束

提交 `e986c17`：项目已有 test extra/group/requirements 时，不再追加 DPRAuto 固定 pytest；只有
没有项目 runner 来源时才 bootstrap 固定 pytest。

直接解决：

- Boto3 hash-lock 后追加无 hash pytest；
- Dagshub `pytest==8.2.1` 与 DPRAuto 8.3.5 resolver conflict。

### 11.2 无锁 PDM 使用 install

提交 `1b7f9cf`：

- 有 `pdm.lock`：继续 `pdm sync`；
- 无 `pdm.lock`：标准构建和 Testability group 使用 `pdm install`。

FastAPI 标准构建由 M9 failed 变为 succeeded。

### 11.3 专用 test requirements 优先

提交 `6f935b9`：精确的 `requirements-tests.txt`、`test-requirements.txt` 等优先于
`requirements-docs-tests.txt` 和宽 dev 文件。

FastAPI 不再只安装 docs httpx 并破坏 PDM 自身依赖，改为选择根级 `requirements-tests.txt`。

### 11.4 依赖感知 Testability timeout

提交 `7439d28`：新增 `verification.dependency_command_timeout_seconds=180`。

- 带 extra/group/requirements/runner 安装：使用 180 秒硬上限；
- 纯测试：继续使用普通 command timeout（评测环境 90 秒）；
- metadata 记录 timeout policy、requested/effective timeout；
- 仍受 Agent/workflow 总 deadline 二次限制。

FastAPI 从 94.5 秒安装即将完成时被误杀，推进为 80.64 秒完成安装并真正进入 pytest。

### 11.5 pytest collection failure 允许进入 Agent

提交 `fc40a80`：删除“出现 `short test summary info` 就是业务断言”的宽泛规则。现在只有明确
`AssertionError`、`E assert` 或 `N failed` 才跳过环境修复；collection/import/dependency
compatibility 错误会进入 Agent。

真实 FastAPI 回归确认 `agent_participated=true`。

### 11.6 M10 真实结果

#### Boto3

- M9：verification_failed、4 LLM、317.90 秒；
- M10：succeeded、0 Agent、0 LLM、35.13 秒；
- 实际命令不再在 hash-lock 后追加 pytest。

#### Dagshub

- pytest 版本冲突已经消失；
- 新失败为完整 `requirements-dev.txt` 拉入 FiftyOne、PyArrow 等宽依赖，下载超过时限；
- 最终仍 verification_failed；
- 5 LLM、466.4 秒；
- 下一步应裁剪宽 dev contract 或预构建可缓存 test dependency layer。

#### FastAPI

- 标准构建已经成功；
- test requirements 已选对；
- 依赖安装不再超时；
- 当前真实失败为旧 FastAPI revision 与最新版 `python-multipart` 的
  `PendingDeprecationWarning` collection compatibility；
- Agent gate 已允许调查；
- 最后一次 `investigate_failure` 收到外部服务 HTTP 402“账户余额不足”；
- 因而 `agent_participated=true`、`repair_attempts=0`、`llm_calls=1`；
- 该次失败不是代码上下文或 wall-clock timeout 问题，需恢复外部 LLM 配额后续跑。

## 12. 当前 Agent 的安全边界和停止条件

默认允许修改：

- `Dockerfile`
- `**/Dockerfile`
- `setup.sh`
- `**/setup.sh`
- `.dprauto/requirements-verification.txt`

业务源码默认禁止修改。secret、`.env`、key/pem/p12/pfx 等敏感文件禁止读取。

Agent 不会对以下情况盲修：

- Git/network/Docker infrastructure failure；
- 明确真实业务 assertion failure；
- 仍有测试进度的 timeout；
- 仍在依赖下载的 build timeout；
- 无直接因果证据的 Testability dependency overlay；
- 一轮同时修改多个高风险环境维度；
- 重复 repair method；
- preflight 明确失败；
- 重建、分层验证或 regression 未通过。

Testability overlay 只作用于临时验证容器，不改变最终 runtime image。

## 13. 当前自动化测试状态

当前最终代码全量测试：

```text
273 passed
88 subtests passed
0 failures
0 errors
1 existing DeprecationWarning
```

最近全量命令：

```bash
PYTHONPATH=src python3 -m pytest -q
```

重点覆盖：

- repair candidate、回滚、checkpoint；
- LLM hard deadline/failover；
- read_file 分页和敏感文件拒绝；
- evidence-backed claims；
- bounded Testability 和 required-secret skip；
- stdio capture compatibility；
- project pytest pin/hash-lock；
- locked/unlocked PDM；
- test requirements priority；
- dependency-aware timeout；
- collection warning 进入 Agent、真实 assertions 仍跳过；
- preflight、重建、三层验证和 regression。

## 14. 本轮 Git 提交清单

以下为本轮从 M1–M10 的主要提交顺序：

| 提交 | 内容 |
| --- | --- |
| `d8dcb2b` | 建立 M1–M4 确定性构建和修复管线 |
| `ff1a1d6` | 分类 M5 Testability 失败模式 |
| `91b3e99` | 记录 bounded Testability evidence |
| `06cc81b` | 本地代表性测试切片 |
| `efeb0f5` | policy skip 不进入 Agent |
| `bb38624` | M5 代表验证报告 |
| `30cd9ce` | LLM request 硬 deadline |
| `532be87` | 限制 timeout failover |
| `712f32f` | 保留健康 fallback 首选 |
| `13334e9` | M6 deadline 结果报告 |
| `20e82e4` | 澄清 transport attempt 上限 |
| `cde08d9` | M7 evidence 失败基线 |
| `e0210fa` | 源码证据、read_file 分页 |
| `07891c4` | 保护受管验证 runner |
| `f615b3f` | Testability overlay 因果证据 |
| `b88e995` | stdio rewrap 检测和 pytest `-s` |
| `b01f0d7` | pytest capture trace |
| `27ca31c` | BuildStrategyAttempt checkpoint 反序列化 |
| `5d32339` | evidence-backed LLM diagnosis |
| `e3b3600` | M7 结果报告 |
| `4c7b33d` | 修复 TOML 最后一个 test extra |
| `7dca839` | EvidenceRecord/EvidencePack 反序列化 |
| `5fce680` | policy skip metrics 分离 |
| `a3b2bf1` | M8 代表回归报告 |
| `620e4a0` | M9 21 项全量结果报告 |
| `e986c17` | 保留项目 pytest 约束 |
| `1b7f9cf` | 无锁 PDM 使用 install |
| `6f935b9` | 专用 test requirements 优先 |
| `7439d28` | Testability dependency setup 预算 |
| `fc40a80` | pytest collection failure 进入 Agent |
| `47d5361` | M10 依赖契约结果报告 |

除文档特别注明的实验运行外，每个独立代码部分均在 targeted tests 和最终 full pytest 通过后
提交。没有 push。

## 15. 关键代码位置

- 总工作流：`src/dprauto/agent/full_workflow.py`
- Agent 调查/计划/候选/preflight/重建/验证：`src/dprauto/agent/workflow.py`
- LLM repair contracts：`src/dprauto/adapters/llm/repair.py`
- LLM HTTP hard deadline：`src/dprauto/adapters/llm/api.py`
- 文件读取/修改工具：`src/dprauto/agent/tools/filesystem.py`
- 结构化修改工具：`src/dprauto/agent/tools/structured.py`
- 上下文和 EvidencePack：`src/dprauto/agent/context.py`
- Python parser：`src/dprauto/adapters/python/parser.py`
- Template strategy：`src/dprauto/strategies/template.py`
- Testability：`src/dprauto/verification/testability.py`
- Test command selection：`src/dprauto/verification/commands.py`
- Docker preflight：`src/dprauto/adapters/preflight/docker.py`
- Regression checker：`src/dprauto/application/regression.py`
- 配置和安全 globs：`src/dprauto/config.py`
- 评测 harness：`evaluations/prompt12/run_evaluation.py`
- 21 项 manifest：`evaluations/prompt12/manifest.json`

## 16. 关键报告和运行目录

报告：

- `evaluations/prompt14/M5_VERIFICATION_FAILURE_CLUSTERS.md`
- `evaluations/prompt14/M5_REPRESENTATIVE_RESULTS_2026-08-24.md`
- `evaluations/prompt14/M6_LLM_DEADLINE_RESULTS_2026-08-24.md`
- `evaluations/prompt14/M7_AGENT_EVIDENCE_FAILURE_BASELINE_2026-08-24.md`
- `evaluations/prompt14/M7_AGENT_EVIDENCE_RESULTS_2026-08-24.md`
- `evaluations/prompt14/M8_REPRESENTATIVE_REGRESSION_RESULTS_2026-08-24.md`
- `evaluations/prompt14/M9_FULL21_CURRENT_RESULTS_2026-08-24.md`
- `evaluations/prompt14/M10_DEPENDENCY_CONTRACT_RESULTS_2026-08-24.md`

运行目录：

- M6 you-get：`evaluations/prompt14/runs/m6-hard-llm-youget-rerun-20260824`
- M7 you-get：`evaluations/prompt14/runs/m7-agent-evidence-youget-20260824`
- M8 代表集：`evaluations/prompt14/runs/m8-representative-current-20260824`
- M9 21 项：`evaluations/prompt14/runs/m9-full21-current-20260824`
- M10 三项目：`evaluations/prompt14/runs/m10-dependency-contract-targeted-20260824`
- M10 FastAPI dependency budget：
  `evaluations/prompt14/runs/m10-fastapi-dependency-budget-20260824`
- M10 FastAPI Agent gate：`evaluations/prompt14/runs/m10-fastapi-agent-gate-20260824`

运行目录通常被 gitignore，不应把大型 artifact 提交到仓库；结论通过上述 Markdown 报告固化。

## 17. 当前环境与操作注意事项

- 工作区：`/home/master/dprauto`；
- 参考项目和 benchmark：`/home/master/auto-build`；
- Docker build network：`host`；
- verification network：`dprauto-eval`；
- build timeout：300 秒；
- M9 verification command timeout：90 秒；
- 依赖感知 Testability timeout：M10 新增 180 秒；
- Agent 评测总预算：900 秒；
- 评测最大修复轮次：2；
- pytest 8.3.5、pytest-xdist 3.6.1、tox 4.23.2、nox 2024.10.9；
- Poetry 1.8.5；
- Git 命令使用：`git -c safe.directory=/home/master/dprauto ...`；
- 不要执行 `git clean`、`git reset --hard` 或递归删除评测目录；
- `myapi.json` 属于用户配置/secret 范围，继续保持不修改、不提交；
- 当前外部 LLM 服务最后一次返回 HTTP 402，续跑 Agent 前应先恢复该服务的余额/配额；
- 本轮没有 push，远端仍可能停留在较早提交。

## 18. 明确未解决的问题

### P0：恢复外部 LLM 可用性并验证 FastAPI Agent 修复

代码已经让 FastAPI 正确进入 Agent，但 HTTP 402 阻止了首次调查。配额恢复后应重新运行索引 22，
确认模型能否识别 `python-multipart` 版本漂移并通过 verification overlay 完成真实修复。

### P1：完整文件读取与 whole-file replacement 硬门禁

需要增加：

- 对 whole-file 修改记录源文件 SHA-256、总行数和完整读取证明；
- 若任何 read page `truncated=true` 且没有覆盖到 EOF，拒绝 `modify_build_script`；
- 计划阶段必须携带完整文件或改用基于原 SHA 的结构化 patch；
- 修改工具进行 compare-and-swap，源 SHA 变化时拒绝；
- 为超过 context 的 Dockerfile/setup.sh 优先提供行级 patch 工具，而不是要求模型重发整个文件。

### P1：Dagshub 宽 dev requirements

当前为了 8 个本地测试安装整个 `requirements-dev.txt`，拉入 FiftyOne/PyArrow。应基于 CI install
步骤、选中测试 imports 和项目声明形成最小 test dependency contract，或预构建可缓存测试层。

### P1：其余依赖失败簇

继续处理：

- Starlette：测试/full extra 或依赖版本；
- testfixtures：缺 `sybil`；
- Piccolo：缺 PostgreSQL driver；
- Black：缺 `aiohttp`/`d` extra；
- yfinance：缺 `requests_cache`；
- tmuxp：缺系统 `tmux`；
- Hknweb/Donation Tracker：Django settings/init contract。

### P2：M10 后统一 21 项全量复测

M10 修改后尚未做统一全量，因此需要重新得到正式成功率、Agent success 和 LLM 成本。不能用
9/21 拼接视图替代正式结果。

## 19. 建议接手后的执行顺序

1. 先确认外部 LLM 配额恢复，不修改或提交 `myapi.json`；
2. 运行 FastAPI 单项：

   ```bash
   PYTHONPATH=src python3 evaluations/prompt12/run_evaluation.py \
     --output evaluations/prompt14/runs/m11-fastapi-agent-recheck-20260825 \
     --indices 22
   ```

3. 若外部 LLM 仍不可用，不要重复烧时间；转而实现“完整读取证明 + source SHA compare-and-swap”；
4. 每个独立代码部分先跑 targeted tests，再串行跑：

   ```bash
   PYTHONPATH=src python3 -m pytest -q
   ```

5. 按用户约定每完成一部分单独 commit，摘要说明内容，正文记录测试；
6. 处理 Dagshub/Starlette/testfixtures/Piccolo/Black/yfinance 代表失败簇；
7. 代表集稳定后运行统一 21 项：

   ```bash
   PYTHONPATH=src python3 evaluations/prompt12/run_evaluation.py \
     --output evaluations/prompt14/runs/m11-full21-current-20260825 \
     --indices 4-24
   ```

8. 单独报告：standard build success、final environment success、Agent repair success、policy skip、
   infrastructure failure、LLM calls/tokens/API seconds 和单项目 p95。

## 20. 最终交接判断

DPRAuto 当前已经具备可运行的自动环境构建和有限自主修复系统，而不是只有 prompt 的原型：

- 标准构建统一全量已达到 90.48%；
- 最终环境统一全量达到 38.10%；
- 已出现首个真实 Agent 修复成功；
- 报错后能够读取项目证据、形成受引用约束的诊断、修改隔离候选、preflight、重建、分层验证、
  regression，并在失败时拒绝/恢复；
- LLM 请求和 Agent 总流程都有硬时间边界；
- 安全策略仍禁止业务源码、任意 shell、多维高风险修改和无证据依赖 overlay。

但还不能宣称“任意项目都能自动修复”：正式最终成功率仍是 8/21，Agent success 仍只有 1/10；
大文件完整读取没有 whole-file 硬证明；Dagshub 等宽依赖 contract 未解决；FastAPI 的下一次真实
Agent 修复又被外部 HTTP 402 阻塞。下一阶段应优先补齐完整文件修改门禁、恢复 LLM 后验证
FastAPI，并在当前最终代码下重新运行统一 21 项。


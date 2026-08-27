# M7 有界修复搜索空间与执行反馈结果（2026-08-27）

## 结论

M7 将 HerAgent/CXXCrafter 的“小搜索空间 + 执行反馈再规划”原则接入 DPRAuto 的现有安全闭环。
Agent 不再把所有 mutation tools 同时交给每一种失败：先由确定性分类和真实错误证据生成 repair
search space，再让 planner 在该子集中选择动作。计划违反门禁或重复已经失败的方法时，拒绝原因
会在同一修复轮内反馈，默认最多纠正 2 次，不再直接浪费一次真实 rebuild/test 机会。

每次实际 preflight、构建和分层验证后，下一轮会收到结构化 `RepairRoundFeedback`，包括前后失败
fingerprint、归一化 failure family、实际修改的环境维度和进展类别。不同时间、worker id 或路径
数字造成 fingerprint 改变时，只要因果失败族没有变化，连续两轮仍会停止，避免“换一段日志就再猜
一次”。本阶段没有调用外部 LLM，也没有读取或修改 `myapi.json`。

## 实现边界

- Testability 只有缺模块、缺 pytest plugin/fixture、distribution/version conflict，或者异常文本
  明确点名包的 collection compatibility warning 时，才暴露
  `patch_verification_dependencies`；Dockerfile/runtime mutation 在 TEST stage 被拒绝。
- 失败证据中的包候选只来自 `No module named`、`cannot import name ... from ...`、
  `Please use import ...`、仓库给出的 `Try running pip install ...` 或 resolver 明确点名的包。
  常见 import/distribution 差异会规范化，未被证据点名的 overlay package 在执行前被拒绝。
- pytest、pytest-xdist、tox、nox 的控制面 resolver conflict 不进入 LLM mutation space，应由 M6
  的依赖来源与 runner 契约确定性处理。
- build/install timeout 只暴露缩减范围的 `modify_build_script` fallback，不暴露增加依赖、切换
  runtime 或 base image 的动作。
- system dependency、Python dependency、runtime compatibility 各自只暴露对应结构化工具与必要
  的 whole-file fallback；一般环境失败仍保留原有有界工具集合和单高风险维度门禁。
- `DPRAUTO_AGENT_MAX_PLAN_FEEDBACK_ROUNDS` 默认 2，必须为正数。该上限约束计划候选，不扩大
  `max_attempts`、总 deadline 或真实 rebuild 次数。

## 真实数据集与历史失败重放

使用已有 M9/M10 真实记录中的原始 `FailureInfo` 做只读重放；没有把人工构造错误冒充数据集结果。

| 项目 | 测试项目目录 | 历史记录 | 当前 M7 search space |
|---|---|---|---|
| tmux-python/tmuxp | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/envbench-python-paper-tmux-python-tmuxp-3e0fec3596cc` | `evaluations/prompt14/runs/m9-full21-current-20260824/records/04-tmux-python-tmuxp.json` | 空；tmux executable 由 M5 确定性编排，不允许猜 Python overlay |
| simplistix/testfixtures | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/envbench-python-paper-simplistix-testfixtures-608b0532dbbe` | `.../records/08-simplistix-testfixtures.json` | 仅 verification overlay；候选 `sybil` |
| piccolo-orm/piccolo | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/envbench-python-paper-piccolo-orm-piccolo-17c0a8859c19` | `.../records/09-piccolo-orm-piccolo.json` | 仅 verification overlay；证据点名 `piccolo[postgres]`，候选 distribution `piccolo` |
| compserv/hknweb | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/envbench-python-paper-compserv-hknweb-422acacc4b1a` | `.../records/14-compserv-hknweb.json` | 空；Django settings/init 由 M5 runner contract 处理 |
| dagshub/client | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/envbench-python-paper-dagshub-client-f8d89c53c733` | `.../records/15-dagshub-client.json` | 空；唯一候选为受控 pytest 冲突，已由 M6 确定性闭包解决 |
| psf/black | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/installamatic-psf-black-98a580b` | `.../records/19-psf-black.json` | 仅 verification overlay；候选 `aiohttp` |
| ranaroussi/yfinance | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/installamatic-ranaroussi-yfinance-3fe87cb` | `.../records/20-ranaroussi-yfinance.json` | 仅 verification overlay；候选 `requests-cache` |
| tiangolo/fastapi | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/installamatic-tiangolo-fastapi-212fd5e` | `evaluations/prompt14/runs/m10-fastapi-agent-gate-20260824/records/22-tiangolo-fastapi.json` | 仅 verification overlay；兼容警告点名 `python-multipart` |

表中的 `...` 均指同一绝对仓库内目录
`/home/master/dprauto/evaluations/prompt14/runs/m9-full21-current-20260824`。M9/M10 运行 artifact
保持只读，没有迁移或提交这些大型目录。

## FastAPI 真实容器复验

使用现有标准镜像 `dprauto-eval/fastapi-212fd5e:fe780020b58e` 和上述 FastAPI 数据集的 `/tmp`
副本，仅写入临时 `.dprauto/requirements-verification.txt`：

```text
python-multipart<0.0.14
```

当前生产 parser、TestCommandSelector、TestabilityVerifier 和 Docker runtime 执行结果：

```text
status = passed
overlay = python-multipart<0.0.14
selected tests = 8 files, 26 collected tests
22 passed, 4 skipped in 2.50s
```

这证明 M10 的 `PendingDeprecationWarning: Please use import python_multipart` 可以由 M7 唯一暴露的
Testability overlay 维度解决，不需要改业务源码或最终 runtime image。该运行使用本地已有镜像，
没有把一次 scripted overlay 复验声称为外部 LLM Agent 成功；临时副本和 artifact 随后移入回收站。

## 搜索与反馈验收

| 原行为 | M7 行为 |
|---|---|
| 每种失败看到全部 mutation tools | 按 stage/category/直接证据生成 allowlist、preference、exclusion reason 和 package candidates |
| 计划门禁拒绝后整个工作流停止 | 同一修复轮把拒绝原因写入 `plan_feedback`，请求受限替代计划 |
| 重复方法直接停止 | 先反馈 method fingerprint，允许一次不同方法；仍重复才停止且不 rebuild |
| fingerprint 改变即可继续 | `failure_family` 去除时间、worker id 等波动，连续无因果进展触发停止 |
| 下一轮只有自然语言摘要 | `RepairRoundFeedback` 保留前后 failure、进展、环境维度和真实执行结果 |
| TEST stage 可用 whole-file Dockerfile 兜底 | 只允许 verification overlay；无依赖证据则 search space 为空 |

## 自动验证

- search space、Agent workflow、反馈 context、LLM payload、checkpoint、配置和完整环境构建聚焦
  回归：`67 passed, 47 subtests passed in 10.04s`。
- FastAPI 真实 Testability：`22 passed, 4 skipped in 2.50s`。
- `compileall` 与 `git diff --check` 通过。
- 全仓库回归：`357 passed, 5 skipped, 136 subtests passed in 76.09s`；唯一 warning 来自
  `test_regression_checker.py` 加载的既有 OpenLane 数据中的无效转义弃用提示，与 M7 无关。

## 尚未声称解决

- M7 缩小并验证修复动作，不自动推导任意包的正确版本范围。FastAPI 的版本界限来自已知兼容
  假设并经过真实测试；未知版本仍需要 lock/CI/发布元数据证据或受控反馈。
- 外部 LLM 服务此前返回 HTTP 402，本阶段没有读取用户 API 配置或伪造一次在线 Agent 成功。
- 动态 plugin hook、非 Python dependency graph、Docker Compose、GPU/设备和复杂多进程拓扑仍
  需要对应的确定性 planner；search space 为空表示不授权猜测，不表示项目不可运行。
- M7 不替代 M5/M6：服务、Django、tmux、pytest 控制面和最小测试依赖应优先在 LLM 前解决。

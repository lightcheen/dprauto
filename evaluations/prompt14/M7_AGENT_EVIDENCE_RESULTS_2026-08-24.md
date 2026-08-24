# M7 Agent 证据一致性与因果修复结果

日期：2026-08-24（Asia/Shanghai）

## 结果摘要

M7 解决了 M6 `soimort/you-get` 暴露的两类问题：一类是 Agent 虽然读过源码，但关键行在
上下文压缩时丢失；另一类是 LLM 诊断和依赖修复缺少确定性证据门禁，能够连续加入 `dukpy`
和与固定 runner 冲突的 `pytest<8.3.5`。

当前实现先确定性识别项目在 import 时重包裹 `sys.stdout/sys.stderr.buffer` 的 pytest capture
不兼容，并对直接 pytest 命令增加 `-s`。这使同一 you-get canary 在 Agent/LLM 介入前直接完成，
没有用 prompt 或候选声明替代真实 Testability。

## 实现与提交

| 提交 | 完成内容 |
| --- | --- |
| `cde08d9` | 固化 M7 失败基线和真实根因 |
| `e0210fa` | `read_file` 增加有界行分页；上下文保留近期源码头尾、行号和 truncation 元数据；普通源码不再冒充 build script |
| `07891c4` | overlay 写入前拒绝覆盖 DPRAuto 管理的 pytest、pytest-xdist、tox、nox |
| `f615b3f` | Testability 依赖计划必须有缺模块、缺插件、distribution 或版本冲突证据；日志已证明 requirement satisfied 时拒绝 |
| `b88e995` | 解析器识别模块级 stdio buffer 重包裹，Testability 为直接 pytest 命令确定性增加 `-s` |
| `b01f0d7` | 验证结果记录原命令、实际命令、选择类型、目标文件和 capture 兼容原因 |
| `27ca31c` | checkpoint serializer 允许 `BuildStrategyAttempt`，恢复 build portfolio 时不再丢失类型 |
| `5d32339` | LLM diagnosis 改为 claims contract；每条 claim 必须引用 catalog 中真实 evidence ref，并显式列出反证 ref |

## 真实 canary

项目：`soimort/you-get`（manifest index 21）

运行目录：
`evaluations/prompt14/runs/m7-agent-evidence-youget-20260824`

最终评测 identity：
`d5ef1665e55a67f0ed47386af14dfb166fb932f07dadf6a46494e947f3434339`

执行命令：

```bash
PYTHONPATH=src python3 evaluations/prompt12/run_evaluation.py \
  --output evaluations/prompt14/runs/m7-agent-evidence-youget-20260824 \
  --indices 21
```

结果：

| 指标 | 结果 |
| --- | --- |
| standard build | `succeeded`，1 个 build attempt |
| final status | `succeeded` |
| Installability / Testability / Runnability | `passed / passed / passed` |
| Agent participated | `false` |
| repair attempts | `0` |
| LLM calls | `0`（结果字段和 API log 均为 0） |
| runner error | 空 |
| elapsed | `12.808451s`（复用持久 Docker/package cache） |

Testability 可追溯数据：

```text
selection_kind = bounded-file-slice+pytest-capture-disabled
original_command = python -m pytest
selection_targets = tests/test_common.py, tests/test_util.py
executed_command = python -m pip install pytest==8.3.5 &&
                   python -m pytest tests/test_common.py tests/test_util.py -s
reason = selected 2 local test files from 2 safe and 1 external-risk file;
         disabled capture because project source rewraps sys.stdout/sys.stderr
```

第一次 M7 canary 还暴露了 `BuildStrategyAttempt` 不在 checkpoint allowlist 的告警。该告警没有
改变成功结果，但已由 `27ca31c` 修复；基于当前实现摘要的最终重跑没有再出现该告警。

## 验收映射

1. 源码 observation 使用近期优先和公平预算，超长内容保留头尾；you-get 的
   `sys.stdout = io.TextIOWrapper(sys.stdout.buffer, ...)` 可被确定性扫描和 LLM evidence context
   保留。
2. `read_file` 支持 `start_line/end_line`，每页最多 400 行，并返回
   `start_line/end_line/total_lines/truncated/next_start_line`。
3. diagnosis 不再接受自由文本兼容形状。1 到 8 条 claims 均需引用
   `failure:*`、`observation:*`、`path:*` 或 `profile:*` catalog ref；缺失/虚构 ref 只纠正一次。
4. verification dependency mutation 有因果证据门禁，并拒绝日志中已 satisfied 的请求；受控
   runner pin 无法通过 overlay 修改。
5. you-get 没有进入 Agent 是预期改进：已知兼容问题由确定性策略在 LLM 前解决，真实容器仍
   执行两文件 Testability 并通过，而不是跳过验证或把 LLM 文本当作成功。

## 自动测试

- capture/parser/selection targeted：53 passed；
- Testability trace targeted：35 passed；
- evidence diagnosis/context/Docker integration targeted：11 passed，另有 2 个 subtests；
- full suite JUnit：352 cases/subtests，0 failures，0 errors，0 skipped。

## 保留边界

evidence ref 校验能够证明“模型引用的材料真实存在”，不能从形式上证明自然语言 claim 一定被
材料逻辑蕴含。因此 mutation 前仍保留确定性计划门禁，mutation 后仍必须执行 preflight、真实
分层验证和 regression comparison。后续提高修复覆盖率时不应删除这些边界。

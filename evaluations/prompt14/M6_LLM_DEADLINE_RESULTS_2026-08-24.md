# M6 LLM 硬截止与故障转移验证

日期：2026-08-24（Asia/Shanghai）

## 目标

解决生产 LLM 请求在代理握手或响应读取中超过 `LLM_TIMEOUT_SECONDS` 仍阻塞的问题，并限制
多模型故障转移对 Agent 900 秒总预算的放大。验收要求是自然形成终态，不再依赖人工
`Ctrl-C`；这不等同于要求 LLM 一定修复项目。

## 实现

- 生产 HTTP 调用进入独立 spawn 子进程；父进程把单次 request timeout 与工作流剩余时间的
  较小值作为绝对墙钟截止，并在超时后 terminate/kill 子进程。
- 响应最大 8 MiB；正常响应、HTTP 错误、socket 错误和父进程硬回收统一进入小时 JSONL
  审计日志，API key 不进入日志。
- `investigate_failure`、`analyze_failure`、`plan_fix` 每次调用默认最多执行 2 次传输尝试；
  多模型池下优先使用不同模型。配置项 `DPRAUTO_LLM_MAX_TIMEOUT_ATTEMPTS_PER_OPERATION`
  的范围为 1–8。
- 超时后先切换模型，再考虑重试同一模型；成功的 fallback 会成为后续 Agent 调用的首选，
  避免每个调查轮次重新撞击刚超时的第一个模型。

## 真实项目 canary

项目：`soimort/you-get`（manifest index 21）

运行目录：`evaluations/prompt14/runs/m6-hard-llm-youget-rerun-20260824`

| 指标 | 结果 |
|---|---:|
| 自然终态 | `verification_failed` |
| 总耗时 | 758.07 秒 |
| Agent 总预算 | 900 秒 |
| 修复轮次 | 2 |
| LLM 调用 | 12 |
| LLM 硬超时 | 2 |
| runner error | 0 |
| stop reason | `maximum repair attempts reached: 2` |

关键调用序列：

1. Flash 调查成功（52.76 秒）；下一轮 Flash 在 120.28 秒被硬回收。
2. 同一次调查切换 Pro 并在 36.68 秒成功；后续调查、分析、规划继续首选 Pro，没有重新从
   Flash 开始。
3. 第二轮 Pro 分析在 120.33 秒被硬回收；同一次分析切换 GLM 并在 38.13 秒成功，随后 GLM
   规划在 20.91 秒返回。
4. 工作流在两次候选都没有通过重新验证后自然结束，全程无需人工中止，也没有遗留 LLM
   worker。

作为对照，M5 的相同项目运行中，第二次 Flash 请求超过配置的 120 秒后持续阻塞，主进程约
11 分钟时仍未形成结果，只能人工中止。M6 canary 虽然仍耗时较长，但每个外部调用和整个
Agent 都已有可执行的硬边界。

## 不应误读的结果

本 canary 没有证明 Agent 修复质量合格。第一轮把 pytest capture 异常错误归因为缺少
`dukpy`；第二轮又提出 pytest 版本范围，两个候选都未解决问题，因此最终失败是正确结果。
M6 解决的是“调用不能失控、失败必须自然收敛”；LLM 是否基于已读取的完整源码做出正确
因果判断，应作为下一阶段独立优化和验收指标。

## 回归

- `PYTHONPATH=src python3 -m unittest tests.test_llm_api -v`：8 项通过。
- `PYTHONPATH=src python3 -m unittest tests.test_evaluation_harness -v`：15 项通过。
- `PYTHONPATH=src python3 -m unittest discover -s tests`：252 项通过。

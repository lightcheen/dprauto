# M9 当前实现 21 项全量回归

日期：2026-08-24（Asia/Shanghai）

## 范围与身份

本轮在 M8 代表性回归 5/5 通过后，对索引 4–24 的全部 21 个真实项目做统一复测：

```bash
PYTHONPATH=src python3 evaluations/prompt12/run_evaluation.py \
  --output evaluations/prompt14/runs/m9-full21-current-20260824 \
  --indices 4-24
```

所有有效 record 使用同一实现摘要：
`c4fb2b13c635072211b5f525fb9ed5725db36beec0b53aaac96e5567fb423c36`。

本轮自然结束，21 个 record 全部落盘，无 runner error；总耗时 4269.41 秒，单项目最长
631.43 秒，未再出现旧基线中单项目超过 900 秒后仍失控运行的问题。

## 核心结果

| 指标 | M9 当前实现 | 旧完整基线 `post-test-matrix-full21-20260821` | 变化 |
| --- | ---: | ---: | ---: |
| 标准构建成功 | 19/21（90.48%） | 8/21（38.10%） | +11 |
| 最终环境成功 | 8/21（38.10%） | 2/21（9.52%） | +6 |
| Installability passed | 20 | 8 | +12 |
| Testability passed | 7 | 3 | +4 |
| Testability policy skipped | 1 | 未分离 | — |
| Runnability passed | 14 | 6 | +8 |
| Agent 修复成功 | 1 | 0 | +1 |
| time budget exceeded | 1 | 11 | -10 |
| runner error | 0 | — | — |

成功项目为 Rich、RuCaptcha、ASDF、Markdown、YubiKey Manager、you-get、minbpe 和
microsearch。RuCaptcha 的 Testability 是基于 required secret 证据的 policy skip，不被误计为
测试失败。

## 首个真实 Agent 修复成功

YubiKey Manager 的标准构建成功，但初始 Runnability 命令需要仓库根目录中的
`cryptography.txt`，实际文件不存在：

```text
poetry run pip download -r cryptography.txt ...
ERROR: Could not open requirements file: [Errno 2] No such file or directory
```

Agent 读取项目和 CI 证据后，仅修改允许范围内的 Dockerfile，先从 Poetry lock 环境导出
`requirements.txt`，再生成命令所需的 `cryptography.txt`。候选 preflight、镜像重建、
Installability、Testability、Runnability 和 regression 全部通过，且没有修改业务源码。

该项目 `agent_participated=true`、`repair_attempts=1`，是当前评估中首个有完整证据链的真实
Agent 修复成功。补丁保存在运行产物：

```text
artifacts/agent-runs/prompt12-18-yubico-yubikey-manager-9c1dde9afde2/
rounds/01/diffs/Dockerfile-6558d2d444b7.patch
```

## 剩余失败聚类

最终未解决类型为 12 个 test、1 个 docker。按日志证据进一步分组：

1. **测试环境依赖或依赖组不完整**：Starlette、testfixtures、Piccolo、Boto3、Dagshub、Black、
   yfinance。典型证据包括缺少 `sybil`、PostgreSQL driver、`aiohttp`、`requests_cache`，固定
   pytest 版本冲突，以及 requirements hash 模式冲突。这是下一阶段收益最高的确定性改进点。
2. **测试需要系统服务、系统包或框架初始化**：tmuxp 缺少 `tmux`；Hknweb 和 Donation Tracker
   在 Django collection 阶段因 settings/框架初始化失败。当前有些项目未进入 Agent，说明应先
   改善验证契约和前置条件识别，而不是直接让 LLM 修改构建文件。
3. **测试命令执行超时或外部条件不稳定**：Hydropandas 验证命令超时；Black 最终耗尽 Agent
   时间预算。应将命令本身的可运行前置条件与项目代码失败分开。
4. **基础设施/平台不匹配**：iCloud Drive Docker 镜像拉取时报
   `no match for platform in manifest`，被正确归为 infrastructure failure，未交给 Agent 盲修。
5. **标准构建规划失败**：FastAPI 的 PDM 路径执行 `pdm sync` 时没有 lockfile，随后 Agent 有一次
  候选修改，但最终 Testability 仍失败。这里需要确定性识别“无 lockfile 的 PDM 项目”，避免
  先制造可预见的标准构建失败。

## Agent 成本与安全性

- 10 个项目进入 Agent，1 个修复成功，成功率 10%；
- 共 79 次 LLM 调用，388357 input tokens、66609 output tokens，共 454966 tokens；
- LLM API 累计 2497.39 秒；进入 Agent 的项目平均耗时 310.13 秒；
- Starlette、Piccolo、yfinance、FastAPI 分别使用 7、15、13、12 次调用，是主要成本热点；
- 硬 wall-clock deadline 生效：超时调用发生 failover，未无限等待；
- 0 regression、0 duplicate repair plan、0 runner error；不安全或无效候选没有被误判为成功。

这说明 M5–M8 已基本解决“超时失控、拿不到完整文件、报错后不能调查和验证”的链路问题；当前
主要瓶颈已转移为：进入 LLM 之前的测试依赖/前置条件规划不充分，以及对不可修验证失败的止损
不够早。

## 下一阶段 M10

优先按以下顺序实施：

1. 从 `pyproject.toml`、Poetry/PDM groups、tox/nox 和 CI 安装步骤中生成统一的 test dependency
   contract，优先使用项目声明的 test/dev extra 或 group，禁止无条件覆盖项目 pytest 版本；
2. 对缺失 Python module、系统 executable、Django settings、required service 和 hash-mode 冲突
   做结构化前置条件分类，在进入 LLM 前给出可验证的 deterministic overlay 或 policy outcome；
3. 对 PDM 无 lockfile 项目使用基于声明的 install 路径，不执行必然失败的 `pdm sync`；
4. 为重复调查、contract correction 和无候选修改增加按失败指纹的 LLM 调用预算，保留至少一次
   真正修复机会，同时减少 Piccolo/yfinance/FastAPI 一类 10 次以上调用；
5. 用上述失败簇做 targeted 回归，再运行代表集和 21 项全量复测。


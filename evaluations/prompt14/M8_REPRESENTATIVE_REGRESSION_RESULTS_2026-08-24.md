# M8 当前实现代表性多项目回归

日期：2026-08-24（Asia/Shanghai）

## 范围

本轮不是 21 项正式全量成功率，而是在 M7 完成后选择 5 个覆盖不同构建和验证路径的真实项目：

| 索引 | 项目 | 主要覆盖能力 |
| ---: | --- | --- |
| 5 | `Textualize/rich` | Poetry 工具镜像、测试文件切片、CLI Runnability |
| 11 | `andreidrang/python-rucaptcha` | required-secret Testability policy skip |
| 13 | `asdf-format/asdf` | setuptools-scm 版本恢复、PEP 621 test extra、大型测试切片 |
| 17 | `python-markdown/markdown` | pip extra、unittest 命令 |
| 24 | `alexmolas/microsearch` | 直接 pytest、CLI/脚本验证 |

运行目录：
`evaluations/prompt14/runs/m8-representative-current-20260824`

最终实现摘要：
`c4fb2b13c635072211b5f525fb9ed5725db36beec0b53aaac96e5567fb423c36`

## 回归发现与修复

### 1. TOML section 最后一个 test extra 被遗漏

首次代表运行中 4/5 成功，ASDF 最终为 `verification_failed`：

- 标准构建已成功；
- Testability 错选 `requirements-dev.txt`；
- 该文件包含 Git 开发版 schema、PyYAML、NumPy 和 SciPy；
- 90 秒验证预算主要消耗在下载/构建依赖，pytest 只到 session header；
- 随后错误进入 Agent，产生 7 次 LLM 调用和 2 次 120 秒硬超时，整项耗时 651.9 秒。

根因是 `_test_group_names` 的正则不能识别 TOML section 末尾的数组：最后一个 assignment 后有
换行时，lookahead 的 `\Z` 分支没有吸收空白；数组值中的 `fsspec[http]` 又会先遇到内部 `]`。
因此 ASDF 的 `[project.optional-dependencies].tests` 完全丢失。

提交 `4c7b33d` 修正 EOF lookahead 并增加嵌套 extra 回归测试。实仓解析恢复为：

```text
test_dependency_extras = tests
dependency install = python -m pip install '.[tests]'
```

修复后 ASDF 以 1 次标准构建、0 Agent、0 LLM 在 84.9 秒通过；最终同身份重跑为 86.3 秒。

### 2. 新 Evidence 类型未加入 checkpoint allowlist

首次 ASDF 的 `persisted_state` 出现 `EvidenceRecord`、`EvidencePack` blocked-deserialization 告警。
提交 `7dca839` 把两个不可变类型加入 serializer allowlist，并用包含分页行号的 evidence pack 做
完整 encode/decode 往返测试。后续 ASDF 重跑没有再出现该告警。

### 3. Policy skip 被错误统计为 test failure

RuCaptcha 因明确依赖 `RUCAPTCHA_KEY`，Testability 正确返回 `skipped`，最终环境为成功。旧 summary
使用“不是 passed”作为 test failure，导致 `build_success_test_failure=1`。

提交 `5fce680` 将 `passed`、`skipped`、`failed/error` 分开统计。最终结果为：

```text
testability_pass = 4
testability_skipped = 1
testability_failed_or_error = 0
build_success_test_failure = 0
```

## 最终同身份结果

执行命令：

```bash
PYTHONPATH=src python3 evaluations/prompt12/run_evaluation.py \
  --output evaluations/prompt14/runs/m8-representative-current-20260824 \
  --indices 5,11,13,17,24
```

| 项目 | 标准构建 | 最终状态 | Testability | Agent / LLM | 耗时 |
| --- | --- | --- | --- | --- | ---: |
| Rich | succeeded | succeeded | passed，8 文件切片 | 0 / 0 | 31.75s |
| RuCaptcha | succeeded | succeeded | skipped，required secret | 0 / 0 | 5.65s |
| ASDF | succeeded | succeeded | passed，`.[tests]` + 8 文件切片 | 0 / 0 | 86.35s |
| Markdown | succeeded | succeeded | passed，unittest | 0 / 0 | 24.74s |
| Microsearch | succeeded | succeeded | passed，直接 pytest | 0 / 0 | 13.53s |

聚合结果：

- standard build：5/5；
- final environment：5/5；
- Installability：5 passed；
- Testability：4 passed、1 policy skipped、0 failed/error；
- Runnability：5 passed；
- Agent participated：0；
- LLM calls/tokens：0/0；
- runner errors：0。

每项最终 record 的 evaluation identity 均基于同一实现摘要，不混用修复前缓存记录。

## 自动测试

- parser/Testability targeted：55 passed；
- persistence targeted：3 passed；
- evaluation harness targeted：16 passed；
- full suite JUnit：355 cases/subtests，0 failures，0 errors，0 skipped。

## 结论与下一范围

M7 的上下文、诊断和 overlay 安全修改没有使这 5 条既有成功路径回退；代表回归反而发现并修复
了一个会让确定性成功项目错误进入昂贵 Agent 链路的 parser 边界。该 5/5 不能替代完整 21 项
统一复测。下一步应在当前实现摘要下运行 4–24 全量，并按 build timeout、policy skip、真实
test failure、infrastructure failure 和 Agent repair outcome 重新聚类。

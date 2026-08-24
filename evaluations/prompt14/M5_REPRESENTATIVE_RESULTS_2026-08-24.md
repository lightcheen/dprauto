# M5 有界 Testability 代表性验证

日期：2026-08-24（Asia/Shanghai）

## 结论

M5 已满足本轮三个核心目标：大型测试套件能生成有界且可审计的文件切片；只有一个本地
pytest 文件的项目不再启动完整 tox 质量环境；明确依赖 secret 的项目不会伪造密钥或误入
LLM 修复。验证没有修改任何被测项目业务源码。

本记录是针对失败聚类的代表性验证，不是 21 项正式成功率复测。运行明细保存在 gitignore
覆盖的 `evaluations/prompt14/runs/` 下；可追溯结论和命令摘要固化在本文。

## 结果

| 项目/场景 | M5 选择 | 结果 |
|---|---|---|
| `boto/boto3` 静态实仓解析 | 从 51 个测试文件中识别 30 个安全本地文件和 21 个外部风险文件；跨 `tests/unit` 子目录轮转选择 8 个文件 | 生成 `bounded-file-slice`，不再默认执行 540 项全量测试 |
| `alexmolas/microsearch` 完整构建 | 直接执行 `python -m pytest`，没有启动 tox 的 black/mypy/types 环境 | 最终成功，项目测试 10.79 秒，整项 104.33 秒，0 次 LLM |
| `andreidrang/python-rucaptcha` 完整构建 | 静态识别 `RUCAPTCHA_KEY`，Testability 以 `required-secret-environment` 明确跳过 | Installability/Runnability 通过，最终成功，整项 6.40 秒，0 次 LLM |
| `soimort/you-get` 完整构建及诊断 | 排除联网的 `tests/test.py`，只执行 `tests/test_common.py`、`tests/test_util.py` | 构建及 CLI Runnability 通过；本地测试暴露项目重绑 `sys.stdout` 与 pytest capture 的真实兼容错误 |

`you-get` 不是测试选择超时：两文件测试命令在 9.07 秒内失败，错误为 pytest 捕获层读取已
关闭文件。Agent 首次诊断准确识别捕获层异常并主动读取两个失败测试文件，证明失败证据到
自主调查的链路可用。后续 LLM 请求在代理连接中超过配置的 120 秒仍阻塞；本次运行在总计
约 11 分钟时人工中止。该现象属于 LLM 传输的硬墙钟取消缺口，不属于 M5 Testability
切片，留给后续超时治理里程碑处理，也不能把该项目记为 M5 成功。

## Boto3 实际选择摘要

```text
python -m pytest \
  tests/unit/test_boto3.py \
  tests/unit/docs/test_action.py \
  tests/unit/dynamodb/test_conditions.py \
  tests/unit/ec2/test_createtags.py \
  tests/unit/resources/test_action.py \
  tests/unit/s3/test_inject.py \
  tests/unit/test_crt.py \
  tests/unit/docs/test_attr.py
```

选择 metadata 同时保留原始命令、`bounded-file-slice`、目标列表、数量和“30 safe / 21
external-risk”的原因，便于复现和审计。

## 回归

- `PYTHONPATH=src python3 -m unittest tests.test_evaluation_harness`：14 项通过。
- `PYTHONPATH=src python3 -m unittest discover -s tests -v`：248 项通过。
- Docker 真实验证：Microsearch 与 RuCaptcha 均最终成功且没有调用 LLM。


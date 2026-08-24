# M5 Testability 失败聚类基线

日期：2026-08-24（Asia/Shanghai）

## 数据边界

本聚类使用同口径 21 项运行：
`runs/post-apt-dns-bounded-full21-20260818`。它是 M5 修改前的历史证据，不代表当前代码的
正式成功率。ASDF 和 Rich 已有后续独立成功记录；其余项目只有在当前实现哈希下复测后才能
更新结论。

## 聚类结果

| 项目 | 历史 Testability 现象 | 聚类 | M5/M4 处理边界 |
|---|---|---|---|
| `tmux-python/tmuxp` | collection 时 `TmuxCommandNotFound` | 测试专用系统能力 | 不能伪装成 Python 依赖；优先选择无需 tmux 的本地测试切片，否则明确跳过/失败 |
| `Textualize/rich` | 错选 `tox -e lint`，Poetry 缺 lint group | 质量任务误选 | 当前 matrix 风险过滤已排除 lint；后续独立复测成功 |
| `simplistix/testfixtures` | conftest 缺 `sybil` | 测试专用 Python 依赖 | M4 verification Overlay；网络不可用仍按基础设施失败处理 |
| `boto/boto3` | `pytest tests/unit` 收集 540 项，90 秒只到约 40% | 本地测试范围过大 | M5 选择有界、可审计的代表性 unit 文件切片 |
| `andreidrang/python-rucaptcha` | conftest 强制读取 `RUCAPTCHA_KEY` | 密钥和真实外部服务 | 不伪造密钥，不运行收费/外部服务测试，记录明确的 Testability skip reason |
| `artesiawater/hydropandas` | 缺 `pastastore`、`pyproj` 等 optional test extra | 测试专用 Python 依赖 | 先从 manifest 识别 pytest 语义 extra；剩余缺包交给 M4 Overlay |
| `asdf-format/asdf` | 错选 `--remote-data` 且缺插件 | 远程测试误选 | 当前命令风险过滤已排除 remote；后续独立复测成功 |
| `psf/black` | 错选 fuzz tox 环境，且命令工作目录不适用 | expensive/fuzz 误选 | 当前 source/matrix 风险过滤排除 fuzz；M5 为存在本地测试文件的项目提供直接测试候选 |
| `ranaroussi/yfinance` | `--cov` 需要未声明 `pytest-cov` | 非必要观测插件 | M5 去除 coverage-only 参数；若项目测试本身需要插件再使用 M4 Overlay |
| `soimort/you-get` | pytest 默认收集 0 项；实际包含非标准 `tests/test.py` 且访问互联网 | 错误 runner + 外部网络 | 不把 0 tests 当成功；识别非标准 unittest 文件，同时排除需要真实网络的测试 |
| `alexmolas/microsearch` | tox 安装 black/mypy/types 后超时，实际只有一个 pytest 文件 | full tox/质量依赖过重 | M5 优先直接运行本地项目测试，不为 Testability 执行 lint/typecheck bootstrap |

## 实现原则

1. Testability 验证的是构建出的环境能否运行项目自有测试，不等同于复制完整 CI matrix。
2. 只根据仓库静态证据选择测试；不得生成项目不存在的测试、密钥或外部服务。
3. 优先项目声明的本地 unit 命令；范围明显超过预算时，选择数量受限且路径可审计的测试文件。
4. docs、lint、format、fuzz、benchmark、remote、integration 和 e2e 不作为默认 Testability。
5. coverage 是观测能力，不是环境正确性的必要条件；默认选择可移除 coverage-only 参数。
6. 因明确密钥或外部服务要求而无法运行时返回带原因的 SKIPPED，不能报告伪造的测试通过。
7. 所有缩小后的命令、原始命令、选择原因和目标数量必须进入 Verification metadata。

## M5 验收目标

- Boto3 类型的大型 unit suite 不再因默认全量执行耗尽 90/120 秒预算。
- Microsearch 类型项目不再为了一个 pytest 文件启动包含 lint/typecheck 的 tox 环境。
- RuCaptcha 类型项目不会因缺少真实 secret 进入 LLM 环境修复。
- 被缩小的测试仍必须执行至少一个真实项目测试文件；零收集不能转为成功。
- 修改不得削弱 Installability 和 Runnability，也不得修改项目业务源码。

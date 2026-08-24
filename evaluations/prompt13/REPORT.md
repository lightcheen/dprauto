# Prompt 13 第二轮通用优化与真实评测报告

评测日期：2026-08-12（Asia/Shanghai）  
样本：与 Prompt 12 完全相同的 24 个 Python 仓库  
评测参数：Docker build 300 秒、验证命令 90 秒、Agent 最多 2 轮、Agent 总时限 900 秒  
原始结果：`runs/second-round-20260812/records/*.json`  
LLM 请求/响应日志：`runs/second-round-20260812/llm-logs/*.log`

## 结论

本轮优化改善了项目解析、错误分类、测试命令真实性和 Agent 编排完整性，但没有改善标准构建成功率，也没有产生一次 Agent 修复成功。最终成功从 0 增至 1，是 `karpathy/minbpe` 通过确定性构建和三层验证直接成功，不是 Agent 修复结果。

系统目前不适合扩大到约 50 个项目。应先解决测试依赖最小化、LLM 超时与 Tool 参数校验、修复前局部验证、增量构建/缓存，以及高风险 Dockerfile 重写问题。

## 本轮只做的通用优化

没有加入任何 `if repo_name == ...` 或仓库名特例。

1. LLM Repair Planner

   - analysis 与 plan prompt 明确要求顶层 JSON schema。
   - analysis 兼容常见的 `root_cause`、`analysis`、`failure_analysis` 等结构。
   - 结果：首轮 23 次 analysis 全被 schema 拒绝；本轮 35 次 analysis 中 34 次成功返回规范 `diagnosis`，1 次 API 超时。

2. Agent rebuild 闭环

   - 每轮修改 Dockerfile/setup.sh 后重新解析 workspace，再选择 BuildStrategy 重建。
   - 解决“文件已修改，但重建仍使用旧 ProjectProfile/BuildPlan”的通用断链。

3. Python ProjectParser

   - 识别 `setup.sh`。
   - 识别 PEP 621、Poetry、PDM、setup.cfg/setup.py 中的 test/dev extras。
   - 排除 `.github`、`.circleci`、docs、examples、tests 中的辅助 Dockerfile。
   - `tiangolo/fastapi` 不再错误选择 `.github/actions/notify-translations/Dockerfile`。

4. TemplateStrategy

   - 宽泛 Python 下界优先使用满足约束的默认 Python，而不是使用约束中出现的最老版本。
   - 安装识别到的 test/dev requirements、extras、pytest/tox/nox。
   - Poetry/PDM/uv 不再默认丢弃 dev 组。
   - Agent 新增的 `setup.sh` 可在重新解析后生效。

5. TestCommandSelector

   - 忽略未解析的 CI matrix/shell 变量。
   - 普通 pytest/unittest 优先于 lint、format、docs、integration、外部数据库测试。
   - `rich` 不再选择 `make format`，`tmuxp` 不再选择 ruff，`piccolo` 不再优先数据库 integration 脚本。

6. FailureClassifier

   - 平台 manifest/image resolution 归为 Docker infrastructure。
   - 缺 git/gcc 归为系统依赖。
   - 陈旧 Poetry lock 归为依赖冲突。
   - setuptools-scm 缺 VCS metadata 归为 Build Tool/VCS metadata。
   - Dockerfile 缺 build context 归为 Build Command。
   - 不再把下载进度中的裸 `503` 误判为外部服务失败。
   - Unknown 的 key log 优先保留项目因果错误，不再保留 BuildKit Go stack 尾部。

## 测试

新增 Regression Test 覆盖：

- Prompt 12 中观察到的 LLM JSON 形态；
- Agent 修改后重新解析并使用新 ProjectProfile；
- 宽泛/有上界 Python 约束；
- test/dev requirements 和 extras；
- Poetry dev 组；
- CI helper Dockerfile 排除；
- 未解析 matrix、lint、docs、integration 命令降级；
- image manifest、缺 Git、陈旧 lock、VCS metadata、缺 Docker context、裸 503 和 BuildKit 栈尾日志。

完整测试命令：

```text
PYTHONPATH=src python3 -m pytest -q
```

结果：`104 passed, 42 subtests passed`。另有一个既有 PytestCollectionWarning，不影响测试结果。

## 核心前后对比

| 指标 | Prompt 12 | Prompt 13 | 变化 |
|---|---:|---:|---:|
| 标准构建成功率 | 12/24 = 50.00% | 12/24 = 50.00% | 0.00 pp |
| 标准构建 failed | 8 | 5 | -3 |
| 标准构建 timed out | 4 | 7 | +3 |
| Agent 修复成功率 | 0/22 = 0.00%（基础设施调整后） | 0/22 = 0.00% | 0.00 pp |
| 最终环境成功率 | 0/24 = 0.00% | 1/24 = 4.17% | +4.17 pp |
| Installability PASS | 12 | 14 | +2 |
| Testability PASS | 0 | 2 | +2 |
| Runnability PASS | 3 | 4 | +1（+4.17 pp） |
| 构建成功但测试失败 | 12 | 10 | -2 |
| 测试成功但运行失败 | 0 | 1 | +1 |
| Regression | 0（无已应用修复） | 0（8 次检查） | 数量不变，分母不同 |
| 平均已应用修复轮数 | 0.000 | 1.227 | +1.227 |
| 最大修复轮数 | 0 | 2 | +2 |
| 平均标准构建耗时 | 134.10 s | 144.56 s | +7.79% |
| P50 标准构建耗时 | 98.24 s | 110.35 s | +12.32% |
| P95 标准构建耗时 | 300.08 s | 300.10 s | 基本不变 |
| 平均 Agent 阶段耗时 | 48.33 s | 404.61 s | +737.19% |
| 累计 wall time | 4,330.39 s | 12,408.97 s | +186.56% |
| LLM 调用 | 23 | 69 | +200.00% |
| Token | 152,326 | 378,089 | +148.21% |

说明：本轮唯一最终成功项目 `karpathy/minbpe` 没有进入 Agent，所以最终成功率提升不能归因于 Agent 修复。

## Agent 与修复质量审计

- 实际进入 Agent：22。
- 基础设施排除后的 Agent eligible：22。
- 已应用修复轮次：27，平均 1.227，最大 2。
- 持久化 EnvironmentDiff：22；其中 high risk 17、medium 3、low 2。
- 业务源码修改：0；人工审核标记：0。
- 无效修改：10。
- 重复修复方案：0。
- 因 Tool `content` 参数为空停止：4。
- 因修复未产生环境变化停止：1。
- LLM API 超时：8（7 次 plan、1 次 analysis）。
- Regression check 完成 8 次，检测到 Regression 0 次。

Regression 为 0 不能证明修复质量：没有任何 Agent 修复达到最终成功，且 22 个 diff 中 17 个被 EnvironmentDiff 判为 high risk。高风险主要来自整份 Dockerfile 重写时同时改变基础镜像、Python 版本、依赖、环境变量和启动参数。

## 构建和验证结果

最终状态分布：

- succeeded：1；
- verification_failed：11；
- project_failed：11；
- infrastructure_failed：1；
- runner error：0。

验证通过：

- Installability：14；
- Testability：2（`python-markdown/markdown`、`karpathy/minbpe`）；
- Runnability：4；
- 三层全部通过：1（`karpathy/minbpe`）。

标准构建成功项目的组成发生变化，但总数不变：

- 从成功退化为超时：`encode/starlette`、`dagshub/client`；
- 从超时变为成功：`andreidrang/python-rucaptcha`；
- 从失败变为成功：`soimort/you-get`；
- 其他状态变化：`yubico/yubikey-manager` 从 failed 变为 timed_out，`alexmolas/microsearch` 从 failed 变为 timed_out。

## 失败类型变化

Prompt 12 初始分类：

```text
test 12, unknown 4, build_command 4, compilation 1,
external_service 1, runtime_version 1, source 1
```

Prompt 13 初始分类：

```text
test 10, build_command 7, system_dependency 2,
dependency_conflict 1, docker 1, run 1, unknown 1
```

有效变化：Unknown 从 4 降至 1；image manifest 被隔离为 infrastructure；缺 Git/VCS、陈旧 lock、运行层失败和超时得到更准确类别。没有任何错误类别被 Agent 成功修复并最终接受，因此“Agent 修复成功最多的错误类型”仍为空。

剩余无法解决的主要类型：

1. Build Command/timeout：7；
2. Test：10；
3. System Dependency：2；
4. Dependency Conflict、Run、Unknown：各 1；
5. Docker platform infrastructure：1（不计 Agent 失败）。

基础设施单列：

- image manifest/platform：1；
- Docker daemon：0；
- Git 下载：0；
- 网络：0；
- 磁盘/CPU/内存：0。

## 哪些优化真正有效

1. LLM schema 和 Agent 状态流：从 23/23 analysis 被 schema 拒绝，改善为 34 次成功 analysis 和 27 次可执行 plan；多轮 apply/rebuild 真正发生。
2. 重新解析后重建：Agent 修改的 Dockerfile/setup.sh 确实进入后续 BuildStrategy。
3. FailureClassifier：Unknown 4→1，且平台 manifest 不再浪费 LLM。
4. Dockerfile 选择：`fastapi` 不再构建 CI helper Dockerfile。
5. 测试命令选择：不再把 matrix、ruff、format、docs、数据库 integration 命令当默认项目测试。
6. 确定性验证依赖：`karpathy/minbpe` 从构建成功但测试失败变为完整成功；Testability 0→2。

## 哪些优化没有明显效果或有副作用

1. 标准构建成功率仍为 50%，没有净提升。
2. Agent 修复成功率仍为 0%。
3. 安装完整 dev/test 依赖导致 timeout 从 4 增至 7，平均标准构建耗时增加 7.79%。
4. Agent 平均阶段耗时增加 737%，Token 增加 148%，但没有成功修复。
5. 17 个 high-risk diff 表明模型经常重写整份 Dockerfile，而不是进行最小环境补丁。
6. Tool 参数契约仍不够稳健：4 个项目因空 `content` 参数停止。
7. LLM timeout 仍是高频停止原因：8 次。

## 下一阶段最优先的 5 个问题

1. 将 test/dev 依赖安装从“全部组”改为“与选定测试命令匹配的最小 extras/requirements/tox env”，并保留 install-only 基础镜像层。
2. 修复前先运行廉价、局部的假设验证；只有命令有效后再进行完整 Docker rebuild，避免每轮 300 秒长尾。
3. 为 LLM plan 加严格结构化 Tool schema、参数修复/拒绝反馈和一次短重试，消除空 `content`；API timeout 加退避重试与更短上下文。
4. 限制整份 Dockerfile 重写：提供结构化的 system-package/dependency/base-image patch Tool，默认拒绝同时改变多个高风险维度。
5. 对 tox/nox/CI matrix 做具体环境解析，区分普通单元测试、外部服务测试和多解释器矩阵。

## 是否扩大到约 50 个项目

不建议。24 项中 Agent 修复成功仍为 0，平均 Agent 时间和 Token 显著上升，且 17/22 个持久化 diff 为 high risk。应先完成上述 1～4 项并在同一 24 项上达到可重复的 Agent 修复成功、较低 timeout 和可接受成本，再扩大样本。

## 实际评测项目路径

| # | 仓库 | 原始项目路径 |
|---:|---|---|
| 1 | hips/autograd | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/envbench-python-paper-hips-autograd-9c756149ec30` |
| 2 | cookiecutter/cookiecutter | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/envbench-python-paper-cookiecutter-cookiecutter-f49dcf975ef0` |
| 3 | robotframework/robotframework | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/envbench-python-paper-robotframework-robotframework-dc42d589cf96` |
| 4 | tmux-python/tmuxp | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/envbench-python-paper-tmux-python-tmuxp-3e0fec3596cc` |
| 5 | Textualize/rich | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/installamatic-textualize-rich-d0de442` |
| 6 | encode/starlette | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/installamatic-encode-starlette-2d0dde8` |
| 7 | mandarons/icloud-drive-docker | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/installamatic-mandarons-icloud-drive-docker-8cbcc2c` |
| 8 | simplistix/testfixtures | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/envbench-python-paper-simplistix-testfixtures-608b0532dbbe` |
| 9 | piccolo-orm/piccolo | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/envbench-python-paper-piccolo-orm-piccolo-17c0a8859c19` |
| 10 | boto/boto3 | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/installamatic-boto-boto3-4e31351` |
| 11 | andreidrang/python-rucaptcha | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/envbench-python-paper-andreidrang-python-rucaptcha-dedaff141ece` |
| 12 | artesiawater/hydropandas | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/envbench-python-paper-artesiawater-hydropandas-2426022ead74` |
| 13 | asdf-format/asdf | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/envbench-python-paper-asdf-format-asdf-77d2bba699ac` |
| 14 | compserv/hknweb | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/envbench-python-paper-compserv-hknweb-422acacc4b1a` |
| 15 | dagshub/client | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/envbench-python-paper-dagshub-client-f8d89c53c733` |
| 16 | gamesdonequick/donation-tracker | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/envbench-python-paper-gamesdonequick-donation-tracker-63411a9fd9d8` |
| 17 | python-markdown/markdown | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/envbench-python-paper-python-markdown-markdown-33359faa385f` |
| 18 | yubico/yubikey-manager | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/envbench-python-paper-yubico-yubikey-manager-fbdae2bc12ba` |
| 19 | psf/black | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/installamatic-psf-black-98a580b` |
| 20 | ranaroussi/yfinance | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/installamatic-ranaroussi-yfinance-3fe87cb` |
| 21 | soimort/you-get | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/installamatic-soimort-you-get-c4042d0` |
| 22 | tiangolo/fastapi | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/installamatic-tiangolo-fastapi-212fd5e` |
| 23 | karpathy/minbpe | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/repo2run-karpathy-minbpe-1acefe` |
| 24 | alexmolas/microsearch | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/repo2run-alexmolas-microsearch-632ff2` |

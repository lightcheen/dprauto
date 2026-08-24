# M10 测试依赖契约与 Agent gate 结果

日期：2026-08-24（Asia/Shanghai）

## 目标

M9 的 21 项全量回归显示，剩余失败主要已从标准构建转移到 Testability：项目声明的 test/dev
依赖没有被准确安装、DPRAuto 固定 runner 与项目约束冲突、无 lockfile 的 PDM 项目被错误 sync，
以及 pytest collection 错误被误认为业务断言而跳过 Agent。

M10 先修复这些确定性契约问题，再用 Boto3、Dagshub 和 FastAPI 做真实回归。

## 已完成修改

### 1. 保留项目 pytest 约束

提交 `e986c17`：当项目已有 test extra、dependency group 或 requirements 文件时，以项目声明为
runner 版本的权威来源，不再追加 DPRAuto 的 `pytest==8.3.5`。只有没有任何项目 test dependency
来源时才安装固定 runner 作为兜底。

该修改同时解决两种冲突：

- Dagshub 的 `requirements-dev.txt` 已固定 `pytest==8.2.1`，不再与 8.3.5 发生 resolver conflict；
- Boto3 的 hash-lock requirements 后不再追加没有 hash 的 pytest 参数。

### 2. 正确处理无 lockfile 的 PDM 项目

提交 `1b7f9cf`：仅当项目确实包含 `pdm.lock` 时使用 `pdm sync`；没有 lockfile 时，标准构建和
Testability group 安装均使用 `pdm install`，允许 PDM 从 `pyproject.toml` 解析并创建 lock。

该行为与 PDM 官方 lockfile 语义一致：`sync` 从已有 lock 安装，`install` 会检查项目声明并在需要
时创建/更新 lock，然后执行 sync。

### 3. 专用测试 requirements 优先

提交 `6f935b9`：精确的 `requirements-tests.txt`、`requirements-test.txt`、
`test-requirements.txt` 等文件，优先于 `requirements-docs-tests.txt` 一类混合用途文件，其后才是
tox/dev 文件。

FastAPI 原先因字典序错选 `requirements-docs-tests.txt`，只安装了旧 httpx，既破坏 PDM 自身依赖，
又没有安装 pytest；修复后选择项目权威的根级 `requirements-tests.txt`。

### 4. 依赖感知的 Testability 时间预算

提交 `7439d28`：新增 `verification.dependency_command_timeout_seconds`，默认 180 秒。只有包含
项目 test extra/group/requirements 或 runner 安装的 Testability 命令使用该硬上限；纯测试命令
继续使用 `command_timeout_seconds`（评估环境为 90 秒）。结果 metadata 记录：

- `timeout_policy`；
- `requested_timeout_seconds`；
- `effective_timeout_seconds`（受工作流总 deadline 再次限制）。

这避免完整依赖在 90 秒附近即将安装完成时，转而触发数百秒 Agent 链路，同时没有取消硬 deadline。

### 5. collection/兼容性错误允许 Agent 调查

提交 `fc40a80`：删除把任意 `short test summary info` 当作业务断言的宽泛规则。现在只有明确的
`AssertionError`、pytest `E assert` 或 `N failed` 才因禁止业务源码修改而跳过 Agent；import、
collection、dependency compatibility warning 等环境错误会进入 Agent。

## 真实回归结果

### Boto3

M9：`verification_failed`，4 次 LLM，317.90 秒；失败为 hash-lock 后附加无 hash pytest。

M10：`succeeded`，0 Agent、0 LLM，35.13 秒：

```text
python -m pip install -r requirements-dev-lock.txt && python -m pytest <8-file-slice>
```

这是本轮新增的最终成功项目。

### Dagshub

pytest 8.2.1/8.3.5 冲突已消失，验证进入真实依赖下载。新的失败是宽
`requirements-dev.txt` 拉入 FiftyOne、PyArrow 等大型依赖，在评估时限内未完成：

```text
verification command timed out
Downloading pyarrow ... (50.1 MB)
```

本轮最终仍为 `verification_failed`，5 次 LLM、466.4 秒。下一步不应恢复固定 pytest，而应从 CI
安装步骤和所选 8 个测试文件中形成更窄的依赖 contract，或把宽 dev 环境预构建到可缓存层。

### FastAPI

进展分四层得到验证：

1. 无锁 PDM 修复后，标准构建从 M9 `failed` 变为 `succeeded`；
2. requirements 优先级修复后，不再错装 `requirements-docs-tests.txt`；
3. 180 秒依赖感知预算使完整安装和 pytest 在 80.64 秒完成，不再 timed out；
4. 最终真实错误为旧 FastAPI revision 与当前 `python-multipart` 的 collection compatibility warning，
   Agent gate 修复后 `agent_participated=true`。

当前最后一次运行目录：
`evaluations/prompt14/runs/m10-fastapi-agent-gate-20260824`。
实现摘要：`c7aa02264e1952dbb63849bc6545209ba4a4d1f7101d28335ba99b1af5068669`。

该次 Agent 首次 `investigate_failure` 调用收到外部 LLM 服务 HTTP 402：

```text
Sorry, your account balance is insufficient
```

因此记录为 `agent_participated=true`、`repair_attempts=0`、`llm_calls=1`。这次未能验证 overlay 修复
是否成功，原因是外部余额而非 Agent gate、上下文、wall-clock timeout 或验证器失控。

## 自动测试

最终代码全量测试：273 passed、88 subtests passed、0 failures/errors；保留 1 条既有
DeprecationWarning。

覆盖的关键回归包括：

- 项目 pytest pin 与 hash-lock 不被覆盖；
- locked/unlocked PDM 分流；
- dedicated tests requirements 优先；
- dependency-and-test 与 test-only timeout policy；
- collection warning 进入 Agent，真实 assertion failure 仍不修改业务源码。

## 下一步

1. 外部 LLM 配额恢复后，按同一实现摘要重新运行 FastAPI，确认 Agent 能否生成并验证
   `python-multipart` 兼容 overlay；
2. 对 Dagshub 实施“宽 dev requirements 识别与依赖裁剪”，避免为了 8 个本地测试安装完整
   FiftyOne/PyArrow 工具链；
3. 将相同规则回归到 Starlette、testfixtures、Piccolo、Black 和 yfinance 失败簇；
4. 代表集稳定后再做 M10 21 项全量复测，评估最终成功率、Agent 成功率和 LLM tokens。


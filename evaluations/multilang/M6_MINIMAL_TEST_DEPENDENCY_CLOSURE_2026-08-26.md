# M6 最小测试依赖闭包结果（2026-08-26）

## 结论

M6 已把 Testability 从“按依赖文件整体安装”改为“所选普通测试命令 + 静态可审计的最小依赖
闭包”。当 Python 项目只有宽泛的 `requirements-dev.txt`/`requirements-qa.txt` 时，DPRAuto
解析所选 pytest 文件、沿目录向上的 `conftest.py` 和递归可达的本地模块，再把实际 import、
fixture 与 pytest 配置映射回原 requirements 中的精确版本约束。未被所选测试使用的重量级
开发依赖不会进入临时验证容器。

Dagshub 的真实全链路验证从原来安装完整 `requirements-dev.txt`（会拉入 FiftyOne/PyArrow 并
超时）收敛为 4 个直接测试依赖；当前标准构建镜像和 Testability 最终得到
`108 passed in 9.10s`。本阶段没有调用 LLM，也没有读取或修改 `myapi.json`。

## 实现边界

- 仅在命令是普通 pytest、有界文件切片、且唯一测试依赖来源为宽泛 dev/qa requirements 时
  启用。项目自有 `requirements-test.txt`、extras、Poetry/PDM groups、tox/nox 继续使用原契约。
- 分析 module/class collection 路径上的 import，跳过 `TYPE_CHECKING`、函数体惰性 import 和
  `ImportError` fallback；排除标准库、本地模块以及标准项目构建已安装的 runtime dependencies。
- 识别常见 distribution/import 差异和 fixture 插件，例如 `PyYAML -> yaml`、
  `requests-cache -> requests_cache`、`pytest-mock -> mocker`、`pytest-git -> git_repo`；
  `pytest.ini` 的 `env` 配置会引入 `pytest-env`。
- 初始切片存在未声明 import 时，从 parser 已判定安全的最多 64 个测试文件中重新选择最多 8 个
  dependency-complete 代表文件。分析上限默认每个目标 256 个 Python 文件。
- `-r`/constraint/hash/URL/续行或无法安全解析的 requirements 不做裁剪，保守回退到项目原依赖
  命令。无法闭包的 import 也不会被猜测安装。
- metadata 记录模式、来源、分析文件、import roots、选中 requirements、排除目标、未解析 import
  和 required executables。`pytest-git` 额外形成系统 `git` 的构建及运行前置条件。

## 真实数据集目录

M6 对 M0 中来自另外三个系统数据集的 8 个 Python 项目执行了只读规划审计：

| 项目 | 数据集目录 | M6 行为 |
|---|---|---|
| yubico/yubikey-manager | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/envbench-python-paper-yubico-yubikey-manager-fbdae2bc12ba` | 保留项目声明来源；不把 CI 的 `pip download` 当测试/运行入口 |
| dagshub/client | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/envbench-python-paper-dagshub-client-f8d89c53c733` | 启用 `minimal-slice`，并执行真实构建与测试 |
| piccolo-orm/piccolo | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/envbench-python-paper-piccolo-orm-piccolo-17c0a8859c19` | 保留专用测试依赖/项目契约 |
| compserv/hknweb | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/envbench-python-paper-compserv-hknweb-422acacc4b1a` | Django runner，保留项目声明来源 |
| gamesdonequick/donation-tracker | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/envbench-python-paper-gamesdonequick-donation-tracker-63411a9fd9d8` | 仓库 runner，保留项目声明来源 |
| tmux-python/tmuxp | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/envbench-python-paper-tmux-python-tmuxp-3e0fec3596cc` | 保留项目声明来源及 tmux executable 契约 |
| tiangolo/fastapi | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/installamatic-tiangolo-fastapi-212fd5e` | 使用项目 package-manager 契约，不裁剪为 pip dev slice |
| django/django | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/executionagent-django-django-e95468ed97b1` | 使用仓库 `runtests.py`，不套用 pytest closure |

该审计中只有 Dagshub 同时满足“宽 dev requirements + 有界 pytest”激活条件；其余项目的保守
结果是设计行为，避免把最小闭包扩大成跨包管理器的猜测。

## Dagshub 闭包证据

原始 8 文件切片包含 `data_engine`/Voxel 测试，其中有宽 dev requirements 无法覆盖的 `pytz`
和 `starlette` import。M6 将它们替换为以下 dependency-complete 普通测试文件：

- `tests/test_dagshub_logger.py`
- `tests/common/test_determine_repo.py`
- `tests/dda/test_tokens.py`
- `tests/model_loading/test_locate.py`
- `tests/dda/filesystem/test_listdir.py`
- `tests/dda/upload/test_wrapper.py`
- `tests/test_errors.py`
- `tests/dda/filesystem/test_misc.py`

从 `requirements-dev.txt` 保留的原始约束只有：

```text
pytest==8.2.1
respx==0.21.1
pytest-git==1.7.0
pytest-env==1.1.3
```

`fiftyone==0.23.8`、`datasets==2.19.1`、`setuptools` 和未使用的 `pytest-mock` 均未安装。
Parser 同时把 `gql[requests]` 正确识别为 runtime dependency `gql`，避免 extras 中的方括号破坏
PEP 621/setup.py 依赖数组解析。

## 验证结果

- 当前生产 Builder 对 Dagshub：`template build succeeded`，97.18 秒；生成的标准
  `python:3.11-slim` 计划确定性安装 `git`，然后执行 `python -m pip install .`。
- 当前生产 Testability 对同一新镜像：只安装上述 4 个 requirements，执行 8 个普通测试文件，
  `108 passed in 9.10s`，无超时。
- 临时 `dprauto-m6/*` 镜像已清理；验证结束后未留下本阶段镜像。
- M6 最终聚焦回归：`80 passed, 45 subtests passed in 1.37s`。
- 全仓库最终回归：`341 passed, 5 skipped, 136 subtests passed in 75.00s`。
- `compileall` 与 `git diff --check` 通过。当前基础环境没有可调用的 Ruff 模块；新增 closure
  模块在实现期间的可用 Ruff 环境中已通过检查，全仓库既有告警未作无关全局修复。

## 已解决的失败类型

| 原问题 | M6 行为 |
|---|---|
| 宽 dev requirements 拉入 FiftyOne/PyArrow | 按所选测试 import/fixture 裁剪为直接需求 |
| 测试真正 import 的可选依赖未知 | AST 收集 collection-time import，并递归本地模块/conftest |
| runtime 与 dev 依赖混在同一 metadata | 新增 `runtime_dependency_names`，只排除标准构建已安装项 |
| 初始代表测试需要未声明依赖 | 在安全候选中重选 dependency-complete 文件，并记录排除理由 |
| pytest plugin 不直接 import | fixture 和 pytest 配置映射回 plugin distribution |
| pytest-git Python 包存在但系统 git 缺失 | Parser/build plan 安装 git，Testability 再验证 executable |
| hash/include requirements 被错误拆分 | 检测不安全语法并回退，不生成不完整安装命令 |

## 尚未声称解决

- 当前 import/distribution、fixture/plugin 映射是小型确定性白名单，不是完整 PyPI 元数据解析；
  未知项会安全回退，后续可用仓库 lock metadata 扩充。
- 本阶段只裁剪 Python pip 风格宽 requirements。Java、C/C++ 已由 M4 构建与验证，但其测试依赖
  裁剪仍由 Maven/Gradle/CMake 等原生依赖图负责。
- 动态 import、pytest 插件自定义 hook、生成代码和测试运行时才触发的可选功能不能仅靠静态 AST
  完整判断；这些路径需要受控 collection probe/反馈轮次，而不是盲装全部 dev dependencies。
- Docker Compose、GPU/设备/硬件和跨容器多进程拓扑仍在后续阶段边界内。

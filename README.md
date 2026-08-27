# dprauto

`dprauto` 是面向可复现环境构建的 Agent 工程。当前已包含通用领域模型、配置、错误
体系、Python/JVM/C/C++ 项目解析、Python 确定性 Docker 构建、规则优先的失败分类，以及基于 LangGraph
的受控环境修复闭环。

预期主流程：

```text
确定性构建 -> 失败分析 -> Agent 修复 -> 分层验证 -> 回归和安全检查
```

## 开发环境

- Python 3.10+
- LangGraph 1.x（由项目依赖安装）

```bash
python3 -m pip install -e .
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 目录边界

- `domain`：与框架和外部工具无关的领域数据结构。
- `ports`：ProjectParser、BuildStrategy 等外部能力接口。
- `inspection`：语言无关、有扫描边界的文件发现和命令提取。
- `intelligence`：后端无关的全仓库 AST/文本知识图谱模型和有界构建器。
- `adapters/multilang`：语言检测、parser registry、Maven/Gradle/CMake/Meson/Autotools 画像。
- `adapters/intelligence`：Tree-sitter、内存图存储和 Neo4j 图存储实现。
- `adapters/python`：Python 清单、版本、包管理器和项目类型识别规则。
- `adapters/execution`：本地子进程执行和完整合并日志采集。
- `adapters/storage`：带 SHA-256 校验的本地不可变构建产物存储。
- `adapters/failure`：Git、网络、Docker 基础设施和项目构建失败的规则分类。
- `adapters/llm`：API 模型传输和结构化修复规划。
- `adapters/persistence`：LangGraph SQLite checkpoint 与完整修复历史索引。
- `observability`：LLM 请求/响应的按小时审计日志。
- `strategies`：Docker、Python Template 和最小 CNB/Pack 构建策略。
- `application`：策略选择、构建执行和失败分类的应用服务。
- `agent`：LangGraph 状态、修复计划、独立 Tool 和有界工作流。
- `config.py`：集中配置及环境变量加载。
- `errors.py`：统一错误代码和异常类型。
- `tests`：独立的标准库单元测试。

## 多语言项目解析

解析只读取目标目录，不会安装依赖或执行目标项目中的命令：

```python
from pathlib import Path

from dprauto.adapters.multilang import MultiLanguageProjectParser
from dprauto.domain.models import SourceReference

profile = MultiLanguageProjectParser().parse(
    SourceReference("local-project"),
    Path("/path/to/project"),
)
```

结果统一写入 `ProjectProfile`。生产 parser registry 当前识别 Python、Maven/Gradle JVM 和
CMake/Meson/Autotools/Make Native 根项目，记录语言、版本/标准、真实子项目与工作目录、
依赖与构建文件、包管理器以及按 build/test/run/install 分类的有界命令候选。JVM/Native
解析不会被仓库中的辅助 Python 脚本抢占；这一阶段只建立项目智能画像，实际确定性容器构建
仍仅支持 Python，Java/C/C++ 构建策略在后续阶段接入。

## Tree-sitter 全仓库知识图谱

仓库智能服务只读扫描代码、配置和文档，为目录、文件、AST named node、声明/import 与文本
分块建立稳定 ID 和有类型的边。当前 Tree-sitter bundle 覆盖 Bash、C、C++、C#、Go、Java、
JavaScript、Kotlin、PHP、Python、Ruby、Rust、SQL、TypeScript/TSX 和 YAML。单文件大小、扫描
深度、AST 深度、每文件 AST 节点数及全图节点数均有硬上限；语法错误与截断记录在图元数据中，
不会伪装成完整索引。

```python
from pathlib import Path

from dprauto.application import create_repository_intelligence
from dprauto.domain.models import SourceReference
from dprauto.intelligence import KnowledgeNodeKind, KnowledgeQuery

service = create_repository_intelligence()
graph = service.index(SourceReference("local-project"), Path("/path/to/project"))
declarations = service.query(
    graph.graph_id,
    KnowledgeQuery(kinds=(KnowledgeNodeKind.DECLARATION,), text="settings"),
)
```

默认工厂使用进程内存存储；生产环境可向工厂传入 `Neo4jKnowledgeGraphStore`。图模型与查询
port 不依赖 Neo4j，写入按 `graph_id` 隔离并在单事务中替换，节点和边按批次参数化写入。
M2 的结构化查询仍保留；M3 在其上增加下述离线语义检索和多轮 Agent 上下文。
Java/C/C++ 的确定性容器构建仍属于后续阶段。

## 离线语义检索与多轮上下文

`RepositoryContextRetrievalService` 将结构化图过滤、代码感知 feature-hashing 向量、字面词项
证据和图邻域传播组合为稳定排序。编码器拆分路径、snake_case、camelCase，并对 build/test/run、
依赖/import、settings/environment、PostgreSQL/Redis、workdir/monorepo 等环境构建概念做对称
扩展。默认实现不下载模型、不访问外部 API；`SemanticEncoder` port 可替换为部署方的密集向量
模型。

```python
from pathlib import Path

from dprauto.application import create_repository_context_retrieval
from dprauto.domain.models import SourceReference
from dprauto.intelligence import SemanticSearchQuery

service = create_repository_context_retrieval()
graph = service.index(SourceReference("local-project"), Path("/path/to/project"))
first = service.query(
    graph.graph_id,
    "repair-session",
    SemanticSearchQuery("test file optional dependency imports"),
)
second = service.query(
    graph.graph_id,
    "repair-session",
    SemanticSearchQuery("framework settings initialization"),
)
```

同一 session 默认最多 4 轮、32 个唯一节点和 12,000 个上下文字符；重复查询不会再次返回已经
见过的节点。生产 Agent 的 `query_repository_context` 是只读调查 Tool，索引最多 2,000 个文件
和 20,000 个图节点，不执行候选代码。`.env`、私钥、keystore、credentials/secrets 配置不会
进入图或普通文本搜索，example/sample/template 文件仍可作为无凭据证据。

## 确定性构建

默认使用有界策略组合：仓库自带 Dockerfile 时先运行 `DockerStrategy`，可恢复的项目构建
失败再依次尝试 `TemplateStrategy` 和 `CNBStrategy`。没有 Dockerfile 的 Python 项目从
Template 开始；CNB 仅在 `pack` 实际可执行且项目含标准 Python manifest 时进入候选。
任一策略成功即停止，网络/Docker 基础设施错误、超时、取消或共享截止时间耗尽也立即停止，
普通构建不会调用 LLM。`DPRAUTO_BUILD_STRATEGY_PORTFOLIO_ENABLED=false` 可恢复旧的单策略
行为，`DPRAUTO_BUILD_MAX_STRATEGY_ATTEMPTS` 控制单次组合最多尝试数（默认 3）。

```python
from pathlib import Path

from dprauto.adapters.storage import LocalArtifactStorage
from dprauto.application import create_deterministic_builder

storage = LocalArtifactStorage(Path("runs"))
builder = create_deterministic_builder(storage)
execution = builder.build(profile, Path("/path/to/project"))
```

每次执行会保存：

```text
runs/
├── plans/<plan-id>/plan.json
├── attempts/<attempt-id>/generated/Dockerfile
├── attempts/<attempt-id>/generated/setup.sh
├── attempts/<attempt-id>/inputs/Dockerfile
├── attempts/<attempt-id>/commands/*.txt
├── attempts/<attempt-id>/result.json
├── build-portfolios/<first-attempt-id>/selection.json
└── command-logs/*.log
```

`BuildExecution.attempts` 与 `selection.json` 保留所有实际策略尝试、每次分类结果、最终选择和
停止原因；各策略共用调用方传入的总截止时间。`BuildResult` 同时保留开始/结束时间、耗时、
退出码、命令结果、日志 Artifact、输出 Artifact 和成功镜像引用。失败由
`FailureInfo.kind` 明确区分 `git`、`network`、`docker_infrastructure` 和
`project_build`。

Docker 构建网络可通过 `DPRAUTO_BUILD_DOCKER_NETWORK` 设置，验证容器网络可通过
`DPRAUTO_VERIFICATION_DOCKER_NETWORK` 独立设置。当前评估环境使用 `host` 完成
BuildKit 构建、使用预先创建的 `dprauto-eval` 用户自定义 bridge 运行验证容器；这是因为
本机默认 bridge 无法出站，而 BuildKit 的逐构建 `--network` 只接受
`default`、`none`、`host`，不接受普通自定义 bridge。评估 harness 会幂等创建并复用
`dprauto-eval`，不会重启 Docker daemon 或切换全局 builder。

联网构建默认通过 `DPRAUTO_BUILD_FORWARD_PROXY_ENVIRONMENT=true` 按名称转发宿主的
`HTTP_PROXY`、`HTTPS_PROXY`、`NO_PROXY` 及其小写形式。Docker/BuildKit 命令只记录
`--build-arg HTTP_PROXY` 这类变量名，不把代理地址写入 BuildPlan、命令 Artifact 或镜像；
验证容器仅在其临时生命周期内接收这些变量。关闭该配置或禁用构建网络时不会转发，Pack
构建的隐式代理继承也会被清空。

Poetry 工具版本由 `DPRAUTO_BUILD_POETRY_VERSION` 固定。部署环境可通过
`DPRAUTO_BUILD_POETRY_TOOL_IMAGE` 配置含 `{version}` 和 `{poetry_version}` 的镜像模板；
配置后 Poetry 项目直接基于该工具镜像构建，不再逐项目安装 Poetry。评估 harness 会在
项目计时前仅为本次选中样本所需的 Python 版本构建工具镜像，以配方 SHA-256 标签校验后
复用，并把预检日志和镜像清单写入输出目录的 `preflight/`。首次工具构建上限由
`DPRAUTO_BUILD_POETRY_TOOL_TIMEOUT_SECONDS` 控制。

模板构建默认只安装运行时依赖：Poetry 使用 `--only main`，uv 禁用默认开发组，PDM 使用
`--prod`，而 `requirements-test.txt`、test/dev extras 和包管理器测试组会延后到
testability 阶段，和项目自己的测试命令在同一个临时验证容器内执行。Poetry 模板同时
禁用虚拟环境，uv/PDM 模板把项目 `.venv/bin` 放入 `PATH`，确保后续验证实际使用构建出的
依赖环境。`include_test_dependencies_in_build=true` 元数据仍可为旧流程显式恢复构建期安装。

如果成功镜像在 Testability 中因为缺少测试插件或测试专用 Python 包失败，Agent 可使用
`patch_verification_dependencies` 更新固定的
`.dprauto/requirements-verification.txt` Overlay。该文件中的有界字面 requirements 仅在
临时验证容器中安装，不进入 Dockerfile 或最终运行镜像。Overlay-only 候选跳过 Docker
rebuild，直接复用已经成功的镜像重新验证；验证通过后才事务化提升 Overlay。URL、marker、
索引选项、shell 片段和非 Testability 阶段调用都会被拒绝。

启用 `DPRAUTO_BUILD_USE_CACHE` 时，生成的 Dockerfile 使用 BuildKit cache mount 复用
Python 包下载；验证容器把同一类下载缓存保存在 Docker volume
`dprauto-python-package-cache`。禁用该配置时，两种缓存都不会挂载。缓存只保存包管理器的
下载/构建缓存，不保存项目虚拟环境或最终镜像内容。

模板策略还会在标准构建前执行有证据边界的确定性修复：依赖清单出现 `git+...` VCS URL
时安装 Git；声明 `pyscard` 时安装其白名单 native 工具和 PC/SC headers；Poetry 项目先
执行 `poetry check --lock`，仅在锁文件不一致时于镜像内部执行不升级已锁版本的 refresh；
源码快照缺少 VCS 元数据、但 manifest 明确配置 `setuptools-scm` 时，会按严格证据优先级
恢复版本：配置的 `version_file`/`write_to` 生成文件、根目录 `PKG-INFO`、标准根目录
changelog 的第一个数字版本标题（仅限明确标注 unreleased/development）。前两类证据保留
精确版本；未发布 changelog 基线生成 `<version>.dev0+g<revision>`；没有源码版本证据时
才从 `SourceReference.revision` 的 7–64 位十六进制提交号生成 `0.0+g<revision>`。文档中
较后的版本示例、已发布的首个 changelog 版本和非标准路径不会被当作证据。策略只设置
`SETUPTOOLS_SCM_PRETEND_VERSION_FOR_<规范化项目名>`；它要求 manifest 明确声明项目名，
不使用工作区目录名，也不设置会影响依赖包的全局 pretend-version 变量。这些动作及证据
来源记录在 `BuildPlan.metadata`，并为 apt lists/packages 使用独立 BuildKit cache。没有
明确 manifest 证据时不会猜测或安装通用 `build-essential` 工具集。

测试命令选择会静态展开有界的 tox brace envlist、nox session/Python 参数和简单 CI
matrix（最多 12 个字面值组合），但不会执行项目配置代码。验证时优先选择与构建镜像
Python 版本一致的单个环境；lint、docs、fuzz、benchmark、外部服务和参数化集成会话不会
作为默认项目测试。tox/nox 只临时安装对应 runner，由所选环境安装自己的依赖，不会再在
启动 matrix runner 前预装所有 dev/test requirements、extras 和包管理器组。

项目命令只在相同用途内按规范化文本去重。若 README 章节把某条命令识别为安装命令，而
`__main__.py` 或 manifest 同时证明它是运行入口，install 与 run 两条记录都会保留；运行
验证因此使用项目入口，而不是无依据地执行基础镜像默认命令。

直接运行 pytest 等非 matrix 测试时也只选择一个测试依赖来源：Poetry/uv/PDM 优先使用其
原生测试组和 extras，普通 pip 项目优先使用声明的 test extra；没有 extra 时仅选择一个
最窄的 test/testing、tox 或 dev requirements 文件。不会把 `requirements-dev.txt`、
`.[tests]` 和临时测试工具全部作为相互独立的依赖集合重复安装。

当唯一可用来源是宽泛的 `requirements-dev.txt`/`requirements-qa.txt`，且 Testability 已选择
有界 pytest 文件切片时，验证器会静态构造最小依赖闭包：解析所选测试、沿目录向上的
`conftest.py` 以及递归可达的本地模块；排除标准库、项目包和标准构建已安装的 runtime
dependencies；再把剩余 import、pytest 配置项和 fixture 名映射回该 requirements 文件中的
原始版本约束。安装命令只包含闭包中的直接 requirements，不会把未使用的 FiftyOne、PyArrow、
datasets、文档或 lint 工具带入临时验证容器。

`TYPE_CHECKING` 分支、函数体内的惰性 import 和 `ImportError` fallback 不会被误当成 pytest
collection 前置条件。若初始切片含项目未声明的 import，系统会在最多 64 个安全测试候选中重新
选择依赖可闭包的代表文件；若找不到完整闭包，或 requirements 使用 `-r`、URL、hash mode、
续行等不能安全裁剪的语法，则保守回退到原项目依赖命令，而不是猜测未固定包。分析默认每个
候选最多递归 256 个文件，可通过 `DPRAUTO_VERIFICATION_MAX_DEPENDENCY_ANALYSIS_FILES`
设置为 16–2048，也可用 `DPRAUTO_VERIFICATION_MINIMAL_TEST_DEPENDENCY_CLOSURE_ENABLED=off`
关闭。闭包模式、证据文件、import roots、选中 requirements、未解析 import 和被排除目标均写入
Verification metadata。

Parser 还会在有界文件扫描中记录真实测试文件、明显的 integration/e2e/remote/notebook/
live-network 文件，以及 conftest 在 module/class scope 强制读取的环境变量名（只记录变量名，
从不读取值）。存在直接 pytest 证据时，Testability 不会为了运行项目测试而启动同时包含
lint、mypy 或其他质量任务的完整 tox/nox 环境。安全本地测试文件超过预算，或同一套件混有
明显外部测试时，会按目录轮转选择最多 8 个真实文件；上限可通过
`DPRAUTO_VERIFICATION_MAX_TEST_FILES_PER_SLICE` 设置为 1–32。原始命令、选择后的目标、
数量和原因都会写入 Verification metadata。

coverage 只属于观测能力，因此 `--cov`、`--cov-report` 等 coverage-only 参数不会成为
Testability 成败的前置条件。若 conftest 在收集阶段明确需要 secret，或者只发现外部服务/
昂贵测试，Testability 会带 `skip_reason` 明确标记 SKIPPED；系统不会伪造密钥、调用付费
服务，也不会把未执行测试写成 PASSED。Installability 和 Runnability 仍必须通过。

临时安装的验证 runner 使用固定版本，默认分别为 `pytest==8.3.5`、`tox==4.23.2` 和
`nox==2024.10.9`，避免上游最新版改变导致同一项目在不同时间得到不同结果。版本可通过
`DPRAUTO_VERIFICATION_PYTEST_VERSION`、`DPRAUTO_VERIFICATION_TOX_VERSION` 和
`DPRAUTO_VERIFICATION_NOX_VERSION` 覆盖，并要求是以数字开头、不包含范围运算符的固定
版本。pip 项目会把 runner pin 合并进所选依赖安装命令，让 resolver 一次处理项目测试
依赖和 runner 约束。

如果 tox 同时声明了 pytest-xdist 依赖、`--numprocesses` 参数和对应 parallel factor，且
项目 CI 环境列表实际使用该 factor，直接 pytest 验证会复用这项项目自有并行策略。`auto`
不会原样传入，而是由 `DPRAUTO_VERIFICATION_MAX_PARALLEL_TEST_WORKERS` 限制，默认最多 4
个 worker；临时 xdist 固定为 `pytest-xdist==3.6.1`，可通过
`DPRAUTO_VERIFICATION_PYTEST_XDIST_VERSION` 覆盖。缺少任一证据、命令已有并行参数或命令
含复合 shell 控制符时均不会重写。tox 4 的 `env_list` 和旧版 `envlist` 都会静态解析，大型
brace factor 矩阵会优先保留短的、覆盖不同 Python 版本的代表环境。

Testability 的前置条件由所选测试命令单独规划。Parser 会记录 CI 中 PostgreSQL/Redis
服务证据，但 `available_test_services` 不等于当前测试需要服务；只有
`test_service_requirements` 或精确的 `test_service_requirements_by_command` 绑定才会触发编排。
这使 Piccolo 的普通 SQLite pytest 不会因为同一 CI 文件存在 PostgreSQL job 而被扩大成
数据库测试。需要服务时，Docker adapter 创建唯一 bridge network，启动最多 2 个白名单
服务容器，执行 `pg_isready`/`redis-cli ping` 健康检查，再把测试容器接入同一网络；测试完成、
超时或初始化失败都会在 `finally` 中删除临时容器和网络，不会复用或修改宿主机已有服务。
默认服务镜像为固定的 `postgres:16-alpine` 和 `redis:7-alpine`，可通过
`DPRAUTO_VERIFICATION_POSTGRES_SERVICE_IMAGE`、`DPRAUTO_VERIFICATION_REDIS_SERVICE_IMAGE`
覆盖；服务启动超时和容器上限分别由
`DPRAUTO_VERIFICATION_SERVICE_STARTUP_TIMEOUT_SECONDS` 与
`DPRAUTO_VERIFICATION_MAX_SERVICE_CONTAINERS` 控制。

Django 项目优先使用仓库自己的 bootstrap：根目录 `manage.py` 会产生
`python manage.py test` 并静态提取 `DJANGO_SETTINGS_MODULE`；包含 `django.setup()` 或
`settings.configure()` 的根目录 `runtests.py` 会优先于通用 pytest；Django 源码仓库则根据
tox 的 `changedir = tests` 选择 `python runtests.py --verbosity=1` 和真实工作目录。默认不盲目
执行 migration；只有显式、受限的 `test_setup_commands` 契约才允许 `manage.py migrate`、
`loaddata` 或 `check`。`libtmux` 与 CI 的 `tmux -V` 同时出现时，构建策略安装白名单系统包
`tmux`，从而满足会启动 tmux server/client 多进程的测试，而不把 import 成功误报为
Testability 成功。运行测试前还会以 `command -v` 验证契约中的 executable，缺失时测试明确
失败，不会只依赖 Python import probe。

## 失败日志分类

`RuleBasedBuildFailureClassifier` 最多读取配置上限内的日志尾部，去除 ANSI/CNB 前缀，
再按确定性规则生成 `FailureInfo`：

- `failure_stage` 和 `category`
- 实际 `failed_command`
- 只包含命中位置附近内容的 `key_log`
- Python、包管理器、基础镜像和构建策略等 `environment`
- `possible_cause`、稳定 fingerprint 和置信度

分类覆盖网络、Docker、系统依赖、Python 版本、Python 依赖、依赖冲突、构建工具、
编译、测试、运行、外部服务和 Unknown。Docker 基础设施规则优先于项目规则；已经恢复
的网络重试不会覆盖日志末尾明确的依赖冲突或编译错误。

`FailureClassifierChain` 只在规则结果为 Unknown 时调用可选的
`FailureFallbackClassifier`。fallback 接口接收结构化 `FailureInfo`，不接收完整
`BuildResult` 或日志 Artifact，为后续 LLM classifier 保留了受控边界。

Docker 集成测试需要正在运行的 Docker daemon 和本地 `python:3.11-slim` 镜像；条件
不满足时测试会明确跳过，而不是伪造成功。

## Agent 修复闭环

`AgentWorkflow` 直接以 `AgentState` 作为 `StateGraph` schema，节点与边为：

```text
START -> investigate/read-only evidence -> analyze_failure -> plan_fix
      -> apply_fix -> preflight -> execute/rebuild -> evaluate
                            + verification overlay -> verify -> evaluate
                                                    | success -> END
                                                    + repairable failure -> investigate
```

- LLM 先通过最多三轮只读调查选择 `list_project_files`、`read_file` 或
  `search_project`，Evidence Pack 达到充分条件或预算上限后才分析失败并输出严格 JSON 的
  `FixPlan`/ToolCall。调查阶段不能运行命令、构建或修改文件；重复调查动作会停止。
- `read_file`、`search_project`、`modify_build_script`、`run_command` 等 Tool 执行真实操作；
  每个 Tool 都声明严格参数 schema，并在副作用发生前校验。重建由 LangGraph 的
  `execute/rebuild` 节点统一调度，不允许 LLM 在修复动作内重复触发。
- 默认只允许修改 `Dockerfile`、`setup.sh` 和固定的 Testability Overlay。路径穿越、symlink
  目标、业务源码修改、shell 命令和未被项目解析/构建计划识别的命令会被拒绝。
- `.env`、私钥和证书文件不会被只读调查读取或被项目搜索返回；`.env.example`、
  `.env.sample` 和 `.env.template` 仍可作为无凭据配置证据。
- 常见环境修复优先使用 `patch_system_packages`、`patch_python_dependencies` 和
  `patch_base_image`：它们只接收有界字面值，在 Dockerfile 中生成或更新一个带标记的最小
  块，并继续保存 unified diff。系统包工具不接收 shell/options，Python 工具不接收 URL、
  environment marker 或索引参数，基础镜像工具要求旧镜像精确匹配且拒绝 `latest`。
- 构建/运行依赖仍由 Dockerfile 结构化工具处理；成功构建后的测试依赖只能使用
  `patch_verification_dependencies`。计划阶段和实际 EnvironmentDiff 都会阻止把测试依赖
  添加到最终运行镜像。
- 每轮只允许修改一个高风险环境维度。计划阶段拒绝 system/Python/runtime 工具混用，实际
  `EnvironmentDiff` 再复核 runtime、system packages、Python dependencies 和 startup；
  `modify_build_script` 仅作为无法用结构化工具表达的单动作兜底，不能用来绕过上述限制。
- 计划前会按失败阶段、类别和直接日志证据生成有界 repair search space。Testability 缺模块、
  插件或明确的 collection 兼容问题只暴露临时 verification overlay；系统包、Python 包、运行时
  和超时缩减各自只暴露相关工具。pytest/tox/nox 控制面冲突、Django 初始化和系统 executable
  不会被泛化成任意 Dockerfile 修改。
- 计划门禁拒绝或命中已经失败的方法时，不立即浪费整个修复轮次；拒绝原因会作为绑定反馈传回
  planner，默认最多生成 2 个候选计划。可通过 `DPRAUTO_AGENT_MAX_PLAN_FEEDBACK_ROUNDS`
  调整这个正整数上限。
- 每次真实 preflight/build/test 后保留有界 `RepairRoundFeedback`：前后失败 fingerprint/family、
  实际环境维度和 `succeeded`/`stagnant`/`advanced-stage`/`changed-failure`/`regressed` 进展。
  下一轮 LLM context 会收到这些执行反馈；即使日志中的时间、worker id 等使 fingerprint 改变，
  连续相同因果失败族仍会触发无进展停止。
- 每个修复轮次保存 unified diff 与 `environment-diff.json`，并在成功、最大轮数、最大
  总时长、重复错误、基础设施失败或无实际环境变更时终止。
- 每次完整重建前，对候选目录中的 `setup.sh` 执行 shell 语法检查，对 Dockerfile 执行
  `docker build --check --network none`。该步骤不执行 Dockerfile 的 `RUN` 指令；只有明确
  语法/解析失败才拒绝候选，Docker 不可用、镜像网络失败或预检超时会记录为 inconclusive，
  继续交给正式构建裁决。报告保存为每轮的 `repair-preflight.json`，单次总预算由
  `DPRAUTO_AGENT_PREFLIGHT_TIMEOUT_SECONDS` 控制（默认 30 秒）。
- 在进入 Agent/LLM 前执行修复资格门控：Docker/Git/网络基础设施失败、超时时仍有明确
  依赖下载进度、Testability 超时时仍有 pytest/unittest/TAP 的明确测试进度、无项目自有
  构建脚本的模板安装超时，以及成功构建后的纯测试断言失败，都会直接返回结构化
  `stop_reason`。其中活动下载和活动测试记为预算耗尽，测试断言保留为验证失败；这些情况
  均不消耗 LLM 调用或修复轮次。普通百分比文本不会被当成测试进度。

生产装配入口：

```python
from dprauto.application import create_agent_workflow

workflow = create_agent_workflow(storage, llm_client, config)
final_state = workflow.run(initial_state, project_workspace)
```

Agent Docker 集成测试从以下此前已成功构建的 fixture 复制临时工作区，再人为注入简单
Dockerfile 构建错误：

- `tests/fixtures/build/docker_project`
- `tests/fixtures/build/requirements_script`

修复过程不会修改原始 fixture。

## 状态、上下文与恢复

活跃 `AgentState` 只保留有界信息：项目画像、当前失败、当前构建脚本、ContextSummary、
最近修改和少量 Artifact 引用。完整 BuildResult、FailureInfo、FixPlan、EnvironmentDiff 和
日志引用按轮次写入外部存储：

```text
runs/
├── agent-state.sqlite
└── agent-runs/<run-id>/
    ├── history/<round>-<method-fingerprint>.json
    └── rounds/<round>/...
```

SQLite checkpointer 会在 LangGraph 每个 super-step 保存状态。使用相同 `run_id/thread_id`
可以在进程重启后继续：

```python
paused = workflow.run(
    initial_state,
    project_workspace,
    interrupt_after=("evaluate",),
)
workflow.close()

new_workflow = create_agent_workflow(storage, llm_client, config)
final_state = new_workflow.resume(initial_state["run_id"])
new_workflow.close()
```

修复方法会计算稳定 fingerprint；已经失败并写入历史索引的方法会在再次执行前被拒绝。
LLM 请求不包含完整历史日志，只包含：

- `ProjectProfile`（过大时转换为紧凑但字段稳定的表示）
- 当前有效 Dockerfile/setup.sh
- 当前 `FailureInfo` 和最多 3000 字符关键日志
- 文件树、按需文件内容和搜索命中的有界 Evidence Pack
- 已解决问题、最近修改及有界的失败方法摘要

`DPRAUTO_AGENT_MAX_CONTEXT_CHARACTERS` 控制上下文硬上限，其余历史只存在外部存储。
`DPRAUTO_AGENT_MAX_INVESTIGATION_ROUNDS`、`DPRAUTO_AGENT_MAX_INVESTIGATION_ACTIONS`
和 `DPRAUTO_AGENT_MAX_EVIDENCE_CHARACTERS` 分别控制只读调查轮数、动作总数和证据字符预算。

## API LLM 与小时日志

`create_api_agent_workflow` 从仓库根目录的 `myapi.json` 按文件顺序读取模型池：

```json
{
  "fast-model": {
    "api_key": "...",
    "model": "provider/fast-model",
    "base_url": "https://.../v1/chat/completions"
  },
  "fallback-model": {
    "api_key": "...",
    "model": "provider/fallback-model",
    "base_url": "https://.../v1/chat/completions"
  }
}
```

单模型旧格式仍兼容。生产 HTTP 请求在可终止的隔离进程中执行；父进程按配置 timeout 和
Agent 剩余总预算的较小值执行绝对墙钟回收，因此代理握手、状态行读取或持续少量响应不能
绕过截止时间。响应体上限为 8 MiB，超时和强制回收仍进入同一小时审计日志。

一次请求超时后先切换下一个配置模型，模型池轮转一遍后才重试同一模型。调查、失败分析和
修复规划的每次 LLM 调用默认最多执行 2 次传输尝试；多模型池下优先落到不同模型，避免一次
故障转移遍历整个模型池；上限可
通过 `DPRAUTO_LLM_MAX_TIMEOUT_ATTEMPTS_PER_OPERATION` 设置为 1–8。默认输出上限为 4096
token。其他对应配置为 `DPRAUTO_LLM_TIMEOUT_RETRIES_PER_MODEL`、
`DPRAUTO_LLM_MAX_OUTPUT_TOKENS` 和 `DPRAUTO_LLM_TIMEOUT_SECONDS`。

`myapi.json` 已加入 `.gitignore`，建议权限保持为 `0600`。API key 只进入 Authorization
请求头，不会写入日志。每次提问和模型返回结果作为一条 JSONL 记录写到：

```text
logs/llm/YYYY-MM-DD-HH.log
```

同一个小时追加到同一文件；跨小时自动创建新文件。日志路径可通过
`DPRAUTO_LLM_REQUEST_LOG_ROOT` 调整。

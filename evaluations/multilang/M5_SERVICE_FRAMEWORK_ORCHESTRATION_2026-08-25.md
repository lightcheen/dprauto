# M5 服务与框架前置条件编排结果（2026-08-25）

## 结论

M5 已把 Testability 从“单个无状态测试容器”扩展为“所选测试命令 + 有界前置条件计划”。
当前确定性路径支持 PostgreSQL、Redis、Django settings/仓库 runner、受限 migration/fixture
命令、tmux 系统 executable，以及需要短生命周期服务/多进程的测试。服务仅在与当前命令
存在显式绑定时启动；CI 中出现服务不再等价于普通测试必须使用该服务。

本阶段没有调用 LLM，也没有读取或修改 `myapi.json`。

## 实现边界

- `ServiceSpec` / `TestEnvironmentSpec` 表达服务镜像、alias、健康检查、命令环境、初始化命令、
  executable 和证据；结果 metadata 只记录环境变量名，不记录值。
- `TestEnvironmentPlanner` 只接受 PostgreSQL/Redis 白名单。默认最多 2 个服务；Django setup
  只接受无 shell 控制符的 `manage.py migrate`、`loaddata`、`check` 契约。
- Docker runtime 为每次验证创建唯一 `dprauto-test-*` bridge，服务不发布宿主端口；服务与
  测试容器通过内部 alias 通信。健康检查通过后才运行测试，并在所有返回/失败/超时路径删除
  测试容器、服务容器和网络。
- CI 中的服务写入 `available_test_services`；只有 `test_service_requirements` 或精确
  `test_service_requirements_by_command` 会触发服务。这个区分防止普通 SQLite/unit test
  被扩大为 CI 数据库矩阵。
- Django 根项目优先 `python manage.py test`，并从 `manage.py` 静态提取
  `DJANGO_SETTINGS_MODULE`；仓库 `runtests.py` 的 `django.setup()` / `settings.configure()`
  bootstrap 优先于 pytest；Django 源码按 tox 的 `changedir = tests` 使用真实 cwd。
- `libtmux` manifest 证据与 CI `tmux -V` 同时存在时生成 `tmux-executable`，模板策略只安装
  白名单 `tmux` 包；Testability 在项目测试前通过 `command -v` 验证声明的 executable。

## 真实数据集项目与选择结果

| 项目 | 数据集目录 | M5 选择/证据 |
|---|---|---|
| piccolo-orm/piccolo | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/envbench-python-paper-piccolo-orm-piccolo-17c0a8859c19` | 普通路径选择 8 个真实 pytest 文件；记录 PostgreSQL CI 服务和 `./scripts/test-postgres.sh -> postgresql`，但普通命令不启动服务 |
| compserv/hknweb | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/envbench-python-paper-compserv-hknweb-422acacc4b1a` | `python manage.py test`；settings 为 `hknweb.settings` |
| gamesdonequick/donation-tracker | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/envbench-python-paper-gamesdonequick-donation-tracker-63411a9fd9d8` | `python runtests.py`；初始化归仓库 runner 所有 |
| tmux-python/tmuxp | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/envbench-python-paper-tmux-python-tmuxp-3e0fec3596cc` | 普通有界 pytest；`tmux-executable` 与构建期 tmux 安装计划 |
| django/django | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/executionagent-django-django-e95468ed97b1` | `python runtests.py --verbosity=1`，cwd=`tests`，不误选 pytest/PostgreSQL job |

这些目录来自 `evaluations/multilang/README.md` 中列出的 EnvBench-Python、Installamatic 和
ExecutionAgent 本地快照；没有创建替代性的玩具项目来证明解析选择。

## 验证结果

### 真实 Docker 服务

本机 Docker 28.0.1 和已有本地镜像 `postgres:15-alpine`、`redis:7-alpine` 上执行：

- PostgreSQL：启动隔离服务，`pg_isready` 成功，第二个容器通过内部 alias 执行
  `psql ... -c 'SELECT 1'` 成功。
- Redis：启动隔离服务，`redis-cli ping` 健康检查成功，第二个容器通过内部 alias 得到
  `PONG`。
- 两个测试都检查结束后不存在 `dprauto-service-*` 容器或 `dprauto-test-*` 网络。
- 结果：`2 passed in 14.86s`；格式化后的重跑为相关 9 项测试 `9 passed in 11.58s`。

### 回归

- M5 聚焦单元/adapter 测试：`110 passed, 54 subtests passed`。
- 全仓库最终回归：`335 passed, 5 skipped, 133 subtests passed in 75.35s`。
- 新增文件与 Docker adapter 的 Ruff 检查：`All checks passed!`。
- 全仓库 Ruff 仍有 M5 之前已存在的 159 项风格告警，因此没有用全局 `--fix` 改写用户代码。

## 已解决的失败类型

| 原问题 | M5 行为 |
|---|---|
| 缺 PostgreSQL/Redis | 显式服务契约、健康检查、内部 DNS alias、确定性环境 |
| Django settings/init | 优先仓库 runner，静态 settings，保留 runner 自有 `django.setup()` |
| migration/fixture | 仅显式、白名单 `manage.py` setup 命令，不盲跑 migration |
| tmuxp import 成功但无 tmux | build plan 安装 tmux executable，并在 metadata 声明 test requirement |
| CI 服务扩大普通测试 | available 与 required 分离；按选中命令精确绑定 |
| 服务污染/端口冲突 | 唯一网络、无 host publish、`finally` 清理 |

## 尚未声称解决

- Docker Compose 多服务拓扑、GPU/设备/硬件、特权容器和跨平台 runner 仍需后续阶段。
- 任意数据库 schema/role/extension 不从 shell 文本自动执行；应先增加结构化、可审计的初始化
  schema，再覆盖 Piccolo 等明确要求额外 role/extension 的 CI slice。
- donation-tracker 的前端 bundle/yarn 契约、tmux 多版本矩阵和完整 Django 测试套件的运行成本
  仍需在后续最小依赖闭包/预算阶段继续收敛。
- 本阶段证明了服务编排与正确命令选择，不把受外部依赖下载影响的整项目构建结果虚报为成功。

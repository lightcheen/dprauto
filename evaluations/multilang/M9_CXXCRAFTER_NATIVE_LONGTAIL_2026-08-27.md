# M9 CXXCrafter 固定源与原生长尾验证（2026-08-27）

## 结论

M9 把 M0 预留的四个 CXXCrafter C/C++ 项目从 `fetch_required` 变成可复现的固定源，
并用真实构建暴露的问题修正原生项目的命令语义、测试依赖契约和容器策略。严格数据集校验现在
覆盖全部 21 个案例；对直接抓取且带 URL 的源，还会执行 `git rev-parse HEAD` 并与 manifest
中的固定提交比较，不接受浮动分支或目录名冒充版本证明。

生产解析器现在把 8cc、mold 识别为 CLI，把 LevelDB、simdjson 识别为 library。只有根目标
确实是可执行程序时才产生版本/帮助 Runnability 命令；原生 library 的 README quickstart、
CI `docker run`、`run_docker.sh` 和 `linkandrun` 不再成为应用入口。library 使用必须找到至少一个
真实可执行文件或静态/动态库的 artifact probe，探针任一步失败都会传递非零状态。

## 固定测试源

来源目录均为 CXXCrafter `top100_dataset.csv` 中的仓库 URL；该 CSV 没有 revision，因此 M9
只解析一次远端 HEAD，保存完整提交 ID，并在之后的严格运行中检查本地 HEAD。

| 项目 | 固定提交 | 实际测试目录 |
|---|---|---|
| rui314/8cc | `b480958396f159d3794f0d4883172b21438a8597` | `/home/master/auto-build/CXXCrafter/datasets/top100-src/8cc` |
| rui314/mold | `e633272dc92c83c6c56c4d8449279da468701193` | `/home/master/auto-build/CXXCrafter/datasets/top100-src/mold` |
| google/leveldb | `7ee830d02b623e8ffe0b95d59a74db1e58da04c5` | `/home/master/auto-build/CXXCrafter/datasets/top100-src/leveldb` |
| simdjson/simdjson | `3839ac681a4a6b4fd09b4d7e03229b35bcd909d7` | `/home/master/auto-build/CXXCrafter/datasets/top100-src/simdjson` |

LevelDB 的测试源依赖也按父提交固定并初始化：

- `third_party/benchmark`: `1a54956777ba672764db09a51960056ea042af7e`
- `third_party/googletest`: `a35bc7693c117a048152beeb34f6aac354b9423f`

数据集目录、URL、revision、普通 build/test/run 命令和前置条件分别保存在 `manifest.json` 与
`ground-truth/cases.json`。`python3 evaluations/multilang/run_evaluation.py --strict-sources`
结果为 21 个 ready、21 个 reviewed、0 个 fetch-required，并验证通过。

## 真实构建驱动的修正

- 生成的 JVM/native Dockerfile 旁写入 `Dockerfile.dockerignore`，只排除 `.git` 与 `.dprauto`。
  Docker 会让该文件覆盖仓库根 `.dockerignore`；这解决了 simdjson 的专用规则排除根
  `CMakeLists.txt`、导致 DPRAuto 生成 Dockerfile 无法配置的问题。
- native 模板显式使用 `USER root`。本次本地基镜像继承了非 root 用户；旧模板因此能完成镜像
  构建，却在 Testability 临时工作区写入对象文件时权限失败。
- CMake 解析器识别测试/回归开关，也识别伴随 `enable_testing()` 的 `*_DEVELOPER_MODE`；
  simdjson 因而使用 `-DSIMDJSON_DEVELOPER_MODE=ON`。
- 若根 `CMakeLists.txt` 声明 `all_tests`、`tests` 或 `check` 自定义目标，只构建这个测试闭包。
  simdjson 不再为普通 Testability 连带构建 benchmark 与 fuzz 目标。
- CTest 选择器在命令没有 `-j/--parallel` 时追加有界 `--parallel 4`；默认构建并行度也统一为
  4，普通验证命令上限为 300 秒。已有显式并行参数不会被覆盖。mold 的默认全架构构建持续
  有效编译并超过原 1800 秒上限，因此通用 build 上限扩大到仍有界的 3600 秒。
- 原生测试脚本的 shebang 只有在 Make/CMake/Meson 普通测试驱动实际引用该脚本时才进入
  executable 契约。8cc 的 `test/ast.sh` 与 `test/negative.py` 因而得到 `bash`、`python2`；
  未被测试目标调用的可选辅助脚本不会制造假依赖。

## 四项目真实结果

测试使用本机已有且包含 cc/c++/make/cmake/ctest 的
`detect-penetration-repair-ai-system-api:latest` 作为离线 native 基镜像；没有调用 LLM。

| 项目 | Build | 普通 Testability | Runnability | 结论 |
|---|---|---|---|---|
| 8cc | `make -j4` 成功 | `make test LDFLAGS=-no-pie` 运行到 `test/negative.py`，因 `/usr/bin/python2` 不存在失败 | `./8cc -h` 成功 | 诚实暴露 EOL Python 2 契约；不能静默改用 Python 3 |
| LevelDB | CMake + `LEVELDB_BUILD_TESTS=ON` 成功 | `--parallel 4` 下 CTest 3/3 通过，122.49 秒 | library artifact probe 通过，count=16 | 成功 |
| simdjson | developer mode + `all_tests` 成功，约 797 秒 | `--parallel 4` 下 CTest 131/131 通过，120.56 秒；顺序基线 484.61 秒 | library artifact probe 通过，count=443 | 成功 |
| mold | 默认 20 架构 CMake 构建成功，约 34 分钟 | `--parallel 4` 下 CTest 486 项中 445 通过、41 能力跳过、0 失败，54.10 秒 | `build/mold --version` 返回 2.42.0 | 成功；证明 build 需要独立 3600 秒上限 |

8cc 的第一次测试还暴露了两个独立问题：继承用户导致写权限失败，以及现代 PIE 默认链接器
拒绝测试对象。显式 root 修正了前者，`LDFLAGS=-no-pie` 修正后者；之后唯一剩余阻断是被实际
测试目标调用的 Python 2 脚本。这一失败保留为平台/遗留运行时选择问题，而不是通过跳过
`negative.py` 或选择 `fulltest` 之外的特殊命令来提高表面成功率。

CXXCrafter 自带的 `docs/validation_results.md` 声称 8cc、mold、LevelDB 测试成功，但没有记录
固定提交、镜像或失败契约，并把 simdjson 标为没有测试文件。M9 没有复用这些布尔标签：它对当前
固定源执行普通仓库测试，并为 simdjson 找到和运行 131 项 CTest。两组结果因此不能直接视为
同一版本、同一环境下的成功率对照。

## 自动回归

- 原生 parser、策略、命令分类、分层验证、配置与数据集聚焦回归：111 项通过；加入测试驱动
  引用闭包后相关 93 项再次通过。
- 最终全仓库回归：`373 passed, 5 skipped in 91.09s`；唯一 warning 来自既有 OpenLane
  回归数据中的无效转义弃用提示，与 M9 无关。
- `compileall`、`git diff --check` 与严格 21 项数据源校验最终通过。

## 尚未声称解决

- DPRAuto 现在能准确说明 8cc 为什么失败，但没有自动选取可信的 Python 2 EOL 基镜像、移植
  `negative.py` 或修改上游源码。这些都需要显式平台/源码修改策略，不能由依赖安装器猜测。
- 本阶段验证 Linux CPU 容器内的 C/C++ Make/CMake/CTest；没有宣称 GPU、硬件设备、跨平台
  toolchain 或 Docker Compose 已由这一阶段覆盖。
- `Dockerfile.dockerignore` 保护生成模板的输入完整性，但大型仓库仍需要后续基于解析图生成更细的
  allowlist，以减少构建上下文而不遗漏真实子项目。
- 本阶段没有读取或修改 `myapi.json`，没有调用外部 LLM，也没有把下载、lint、benchmark、fuzz、
  docs 或 release 命令计作 Testability/Runnability 成功。

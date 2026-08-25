# M1 多语言项目智能与命令语义结果

日期：2026-08-25（Asia/Shanghai）

## 目标

M1 解除生产组合根对 `PythonProjectParser` 的硬编码，在实际构建策略之前建立统一、只读、
可审计的多语言 `ProjectProfile`。本阶段不执行候选项目命令、不构建镜像，也不修改测试源码。

## 完成能力

- 增加显式优先级 parser registry；根 Maven/Gradle 或 Native 构建系统不会被辅助 Python
  脚本误判为 Python 项目。
- 增加与 HerAgent Tree-sitter 语言族对齐的后缀检测基础，覆盖 Python、Java、Kotlin、
  Groovy、C、C++、C#、Go、Rust、JavaScript、TypeScript、PHP、Ruby、Bash、SQL、YAML。
- Maven/Gradle：识别 wrapper、Java toolchain、模块、动态 Gradle build-file 子项目、真实工作
  目录以及常用 build/test 命令。
- C/C++：识别 CMake、Meson、Autotools、Make、C/C++ standard、`add_subdirectory`、有序
  build pipeline 和 ctest/make check 等普通测试入口。
- 命令分类新增 Maven、Gradle、CMake、ctest、Meson、Autotools；`pip download` 被归为
  dependency preparation，lint/format/fuzz/docs/release/publish 被归为非 Testability 目标。
- JVM/Native 命令候选按用途有界，build/test 各最多 12 条；优先保留常用推断入口，防止
  CI matrix 扩张搜索空间。nlohmann/json 的 build 候选由审计时的 84 条限制到 12 条。

## M0 真实源码验证

对 [M0 列出的 17 个本地源码目录](README.md)进行了只读解析：

| 语言/生态 | 数量 | parser | 结果 |
|---|---:|---|---|
| Python | 8 | `python-rules-v1` | 8/8 路由成功并发现普通测试候选 |
| Maven/Gradle JVM | 4 | `jvm-rules-v1` | 4/4 路由成功并发现 build/test 候选 |
| C/C++ Native | 5 | `native-rules-v1` | 5/5 路由成功并发现 build/test 候选 |

代表性人工核对：

- Apache Commons CSV：Maven、Java 8；
- Spring Security：Gradle、Java 17，并发现 34 个真实子项目工作目录，未把
  `include '**/*.gradle'` glob 当成子项目；
- ccache：CMake、C99/C++17、ctest；
- distcc：Autotools、有序 `autogen -> configure -> make -> make check`；
- YubiKey Manager：CI 的 `pip download -r cryptography.txt` 为 install/setup 语义，不再是
  test 或 run 候选。

## 阶段边界

M1 尚不表示 Java/C/C++ 已可完成容器构建与分层验证。后续仍需实现：

1. Tree-sitter AST 与语言声明/import/call 关系索引；
2. 图存储 port、内存测试实现和 Neo4j adapter；
3. Java/C/C++ 确定性容器策略及对应 Installability/Testability；
4. PostgreSQL、Redis、Django migration/init、多进程等服务编排。

## 自动化验证

- M1 定向：36 tests、21 subtests passed；
- 仓库全量：290 tests、109 subtests passed；
- 既有 1 条 `DeprecationWarning`，无新增失败或错误。

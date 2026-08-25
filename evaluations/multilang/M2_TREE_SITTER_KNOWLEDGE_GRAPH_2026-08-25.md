# M2 Tree-sitter 全仓库知识图谱结果

日期：2026-08-25（Asia/Shanghai）

## 目标与安全边界

M2 将 HerAgent 的 Tree-sitter 与全仓库图思想移植为 DPRAuto 的后端无关能力。索引过程只读取
源码、配置和文档，不执行候选项目代码、不安装候选项目依赖、不启动数据库，也不修改数据集。

实现包括：

- `SyntaxTreeParser` 与 `KnowledgeGraphStore` port；
- 稳定、不可变的 repository/directory/file/AST/declaration/text 节点和有类型边；
- 有界全仓库构建器以及按源码内容生成的 fingerprint/graph ID；
- 线程安全内存 store，供本地和测试使用；
- 按 `graph_id` 隔离、单事务 replace、批量参数化写入的 Neo4j adapter；
- 应用服务统一编排 index/load/query/delete，上层不依赖 Neo4j driver；
- 路径、语言、节点类型、节点种类和文本的有界结构化查询。

默认上限为 5,000 个文件、16 层目录、单文件 512 KiB、AST 深度 10、每文件 2,000 个 AST
节点、全图 100,000 个节点。语法错误、大文件跳过、AST/文本/全图截断都会进入图 metadata。

## 语言与依赖兼容性

生产映射实际加载并解析了 16 个 grammar：Bash、C、C++、C#、Go、Java、JavaScript、Kotlin、
PHP、Python、Ruby、Rust、SQL、TypeScript、TSX、YAML。版本固定为：

- `tree-sitter==0.21.3`；
- `tree-sitter-languages==1.10.2`；
- `neo4j==5.28.4`。

前两个版本沿用 HerAgent 的相互兼容组合。HerAgent 固定的 `neo4j==5.20.0` 在当前配置的软件源
返回无有效版本 metadata，因此选择最后一个兼容的 Neo4j 5.x 版本 5.28.4，避免未经适配直接
跨到 6.x。所有外部包均惰性导入；未安装 Tree-sitter 时普通 DPRAuto import 不受影响，真正
请求 AST 时返回明确依赖错误。

## 真实测试项目目录

本阶段从其余项目已落地的数据集中选择三种目标语言的代表项目：

| 语言 | 项目 | 数据集目录 |
|---|---|---|
| Python | YubiKey Manager | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/envbench-python-paper-yubico-yubikey-manager-fbdae2bc12ba` |
| Java | Apache Commons CSV | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/executionagent-apache-commons-csv-2d44689ec75e` |
| C++ | ccache | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/executionagent-ccache-ccache-7f3e822efb1b` |

真实文件根节点分别验证为 Python `module`、Java `program` 和 C++ `translation_unit`。ccache
文件包含当前 grammar 无法完全消解的预处理语法，构建器将其保留为 `parse_error_files`，没有
把“生成了树”误报成“无语法错误”。

## 有界图测量

采用统一上限：最多 8 个文件、AST 深度 8、每文件 300 个 AST 节点、全图 3,000 个节点。

| 项目 | 文件 | 节点 | 边 | AST | 声明/import | 语法错误文件 | AST 截断文件 |
|---|---:|---:|---:|---:|---:|---:|---:|
| YubiKey Manager | 8 | 2,136 | 2,263 | 1,999 | 128 | 0 | 7 |
| Apache Commons CSV | 8 | 1,768 | 1,966 | 1,560 | 199 | 0 | 6 |
| ccache | 7 | 1,512 | 1,749 | 1,265 | 238 | 1 | 6 |

上述测量证明 Python、Java、C++ 使用真实 Tree-sitter AST，而不是正则伪 AST；同时也说明上限
会实际触发并可审计。全仓库默认预算高于此测量预算，但仍是硬上限。

## Neo4j 与查询验证

没有在本阶段启动外部 Neo4j 服务。adapter 通过可记录 driver 验证了 schema、作用域删除、
节点/边 batch、参数化 Cypher、单事务 replace、load 重建和 bounded query；Python driver
5.28.4 已完成导入验证。真实 Neo4j 服务连通、容量与事务恢复仍需在后续集成环境验证。

## 当前阶段边界

M2 已具备结构化代码/配置/文档检索基础，但尚未完成 embedding 语义向量、图与向量混合排序、
多轮上下文记忆，以及将图查询作为 Agent 调查 Tool 接入修复闭环。它也不等同于 Java/C/C++
容器构建支持；对应构建策略、服务编排和最小测试依赖闭包仍按后续里程碑推进。

## 自动化验证

- 无可选运行时依赖的系统环境：10 passed、3 skipped；
- Tree-sitter/Neo4j 兼容依赖环境：13 passed、19 subtests passed；
- 系统环境全仓库回归：300 passed、3 skipped、109 subtests passed；
- 兼容依赖环境全仓库回归：303 passed、128 subtests passed；
- 两次全量回归均只有既有 OpenLane fixture 的 1 条 `DeprecationWarning`，无新增 warning。

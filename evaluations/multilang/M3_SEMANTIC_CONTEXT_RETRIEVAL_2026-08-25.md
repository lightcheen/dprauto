# M3 离线语义检索与多轮上下文结果

日期：2026-08-25（Asia/Shanghai）

## 目标与实现

M3 在 M2 的 Tree-sitter/Neo4j 后端无关图之上增加代码、配置和文档的统一检索，并将其作为
只读调查 Tool 接入 Agent。实现不执行候选项目、不修改项目、不启动外部服务，也不要求在线
embedding API。

检索排序由四部分组成：

1. `SemanticSearchQuery` 的路径、语言和节点种类结构化过滤；
2. 路径、snake_case、camelCase、代码和自然语言词项的字面覆盖；
3. 稳定 feature hashing 稀疏向量和环境构建概念的对称同义扩展；
4. `HAS_AST`、`DECLARES`、`HAS_TEXT` 等已有图边的邻域分数传播。

`SemanticEncoder` 是独立 port。默认 `CodeAwareHashingEncoder` 不下载模型且完全确定性，生产
部署可替换成密集 embedding adapter，而无需改变图模型、会话或 Agent Tool。当前结果因此是
离线、领域感知的语义近似，不宣称等价于经过训练的代码 embedding 模型。

## 搜索空间和反馈轮次

- 默认最多索引 20,000 个可检索图节点，每节点文本最多 4,000 字符；
- 每条路径最多 3 个结果，避免同一大型 AST 淹没候选；
- 单次查询最多 20 个结果，Agent Tool 进一步限制为 12；
- session 最多 4 轮、32 个唯一节点、12,000 个累计上下文字符；
- 后续轮自动排除已经返回的节点；图 fingerprint 改变时自动建立新会话视图；
- index cache 和 session cache 均有数量上限；
- Agent 侧仍受原有 3 轮、6 动作、10,000 evidence 字符和总 deadline 二次约束。

`query_repository_context` 仅属于 `observe` 调查工具。检索结果进入 `EvidencePack` 后再供诊断和
修复规划使用，不能修改文件、运行命令或绕过原有 mutation policy。

## 敏感文件边界

语义索引会主动扫描配置，因此 M3 统一了普通文本搜索和图构建的敏感路径策略：`.env`、私钥、
PKCS/Java keystore、password database，以及 credentials/secrets 命名的配置不会被读取或索引。
带 example/sample/template 标记的无凭据模板仍可检索。测试确认凭据内容不会出现在 Tool context。

## 真实测试项目目录

| 探针 | 数据集目录 | 查询目标 |
|---|---|---|
| Django PostgreSQL driver | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/executionagent-django-django-e95468ed97b1/django/db/backends/postgresql` | 可选 PostgreSQL driver import |
| Apache Commons CSV | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/executionagent-apache-commons-csv-2d44689ec75e/src/main/java/org/apache/commons/csv` | duplicate-header strictness 实现 |
| ccache | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/executionagent-ccache-ccache-7f3e822efb1b/unittest` | compiler dependency argument 测试 |
| Testfixtures | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/envbench-python-paper-simplistix-testfixtures-608b0532dbbe` | `conftest.py` 实际 import 的 Sybil |
| Django settings | `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/executionagent-django-django-e95468ed97b1/tests/settings_tests` | settings 初始化前置条件 |

前三项属于 M0 已列出的多语言代表项目。Testfixtures 是同一 CNB/EnvBench-Python 本地数据集中的
补充长尾样本，用于复现 M9 缺 `sybil` 的测试依赖类型；目录在本报告中显式固定。

## 真实检索测量

| 探针 | 文件 | 图节点 | 图边 | 目标排名 | 目标文件 | 分数 | 图截断 |
|---|---:|---:|---:|---:|---|---:|---|
| Django driver | 10 | 4,658 | 4,825 | 1 | `base.py` | 0.602 | 否 |
| Apache Commons CSV | 12 | 4,597 | 4,944 | 1 | `CSVFormat.java` | 0.674 | 否 |
| ccache | 19 | 8,594 | 8,812 | 1 | `test_argprocessing.cpp` | 0.569 | 否 |
| Testfixtures | 76 | 10,000 | 10,644 | 1 | `conftest.py` | 0.481 | 是 |
| Django settings | 2 | 704 | 746 | 1 | `tests.py` | 0.451 | 否 |

Testfixtures 在 10,000 节点硬上限处明确标记截断，但目标仍排第 1；系统没有把截断索引误报为
完整全仓库覆盖。这五项是针对已知长尾的代表性探针，不是通用信息检索 benchmark，也不能单独
推导所有仓库的 recall。

## 当前阶段边界

- 多轮 session 的去重状态保存在当前进程；Agent 的结果 Evidence 会随既有 checkpoint 保存，
  但进程重启后语义检索器的 unseen-node 集合会重新建立；
- Neo4j 中的图可通过应用服务 load 后检索，但尚未使用 Neo4j vector index；
- 尚未实现学习型 dense embedding、跨仓库模型评估或 reranker；
- M3 改善的是调查上下文，不等同于 Java/C/C++ 确定性容器构建和服务编排。

## 自动化验证

- M3 离线单元测试：10 passed、3 subtests passed；
- 无 Tree-sitter 运行时依赖的系统环境全仓库回归：311 passed、5 skipped、112 subtests passed；
- Tree-sitter/Neo4j 兼容依赖环境全仓库回归：316 passed、136 subtests passed；
- 两次全量回归均只有既有 OpenLane fixture 的 1 条 `DeprecationWarning`，无新增 warning。

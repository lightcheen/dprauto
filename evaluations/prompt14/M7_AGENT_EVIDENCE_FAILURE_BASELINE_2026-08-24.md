# M7 Agent 证据一致性失败基线

日期：2026-08-24（Asia/Shanghai）

## 样本边界

样本来自 M6 自然结束的真实运行：
`evaluations/prompt14/runs/m6-hard-llm-youget-rerun-20260824`。项目为
`soimort/you-get`，最终状态为 `verification_failed`，两轮候选均被重新验证拒绝。

M7 不把“让模型再猜一次”作为方案，而是检查从 Tool 读取、上下文压缩、诊断、计划校验到
重新验证的每个确定性边界。

## 已确认的真实原因

被选中的两个本地测试导入 `you_get.common`。该模块在 import 时执行：

```python
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf8')
```

pytest capture 已经替换 `sys.stdout`；项目再次基于其 buffer 创建 wrapper，旧 wrapper 被回收
时会关闭 pytest 的临时流，随后 pytest 在 collection/capture teardown 中得到
`ValueError: I/O operation on closed file`。运行 `pytest -s` 禁用 capture 可以验证这个因果链，
不需要新增项目依赖，也不能修改项目业务源码。

## 四个证据断点

### 1. 文件已读取，但关键内容没有进入分析请求

Agent 第三轮调查读取了 `src/you_get/common.py`。M6 的实际 analyze request 同时满足：

- 包含路径 `src/you_get/common.py`；
- 不包含 `sys.stdout = io.TextIOWrapper`；
- 包含 Dockerfile/setup.sh 中的 `python -m pip install .`。

原因是 `AgentContextManager._build_scripts` 把所有读取过的文件误当成 build script，并在压缩时
只保留每个文件尾部；`_evidence` 又优先消耗较早 observation 的字符预算。路径存在造成
“已经给过源码”的假象，文件头部真正的因果行却丢失。

### 2. ReadFile 没有分段读取协议

`read_file` 只能读取完整文件。大文件虽然在 Tool 层成功读取，进入 LLM context 时仍会被
二次截断；模型也无法按 `start_line/end_line` 请求下一段。因此“Tool 成功”不等于“模型拿到
所需内容”。

### 3. 诊断可以直接违背已知构建事实

第一轮分析请求明确包含：

- `requirements.txt` 声明 `dukpy`；
- `setup.py install_requires = ['dukpy']`；
- Dockerfile 和 setup.sh 均先安装 requirements，再执行 `pip install .`。

LLM 仍断言“项目/dukpy 未安装”。当前诊断 contract 只要求非空字符串，没有要求列出支持
证据和反证，也没有在计划前拒绝与 manifest/build plan 明确冲突的缺包主张。

### 4. Overlay 没有 runner 冲突校验

第二轮计划加入 `pytest<8.3.5`，而 Testability 固定安装 `pytest==8.3.5`。Overlay 的 requirement
正则允许任意合法版本范围，计划因此执行并产生必然的 `ResolutionImpossible`。固定 runner
属于 DPRAuto 控制面，冲突应在 mutation/preflight 之前拒绝并反馈模型，而不是消耗一次验证。

## 已正确工作的边界

- LLM 不能修改 `src/you_get/common.py` 业务源码；
- verification dependency 只进入临时测试容器，没有污染运行镜像；
- 两个候选都必须重新执行 Testability；
- 无效候选没有被接受，最终以最大修复轮次明确失败。

## M7 验收目标

1. 大文件 observation 在上下文中保留有界的头部和尾部，并优先保留最近调查证据；实际 build
   scripts 与普通源码/manifest 分区，不再互相挤占预算。
2. `read_file` 支持有界行范围，返回 `start_line/end_line/total_lines/truncated`，模型可继续读取
   未提供的部分。
3. 诊断输出结构化 `claims`，每条关键主张引用 observation/path/log 证据；“缺少依赖”计划在
   项目声明、构建计划或验证日志已经证明依赖存在时被拒绝。
4. verification overlay 在写文件前拒绝与固定 pytest/tox/nox runner pin 冲突的 requirement。
5. 使用同一 `you-get` canary 验证关键 `sys.stdout` 行确实进入分析上下文；任何修复仍需真实
   Testability 重新验证，不能用 prompt 声称成功。


# M8 构建脚本完整读取证明与 CAS 修改结果（2026-08-27）

## 结论

M8 关闭了旧交接记录中 whole-file replacement 的高优先级安全/正确性缺口。Agent 不能再根据
分页、截断或过期的构建脚本内容覆盖整个文件：`read_file` 返回原始字节快照的 SHA-256、字节数、
总行数和 `complete_file`；LLM context 发生二次裁剪时会撤销完整标记。已有文件的
`modify_build_script` 必须同时携带完整读取证明和相同 `source_sha256`。

对于超过单页或 context 上限的文件，新增 `patch_build_script`。它只接受 prompt 中真实可见的
`old_content`，要求该片段在同一 SHA 版本中恰好出现一次，并只完成这一个替换。所有 whole-file、
exact patch 和结构化依赖/镜像工具在最终写入时再次执行 compare-and-swap；候选通过构建、测试和
回归后，promotion 还会核对调用方工作区的原始 digest，避免验证期间的外部改动被静默覆盖。

## 实现契约

- `ReadFileTool` 对文件原始 bytes 计算 SHA-256；分页之间共享同一源版本标识。
- `complete_file=true` 只表示从首行读取到 EOF 且传给 planner 的内容没有再次被 context 裁剪。
- `modify_build_script` 对已有文件要求 64 位小写 SHA-256；创建新文件只能使用字面量 `absent`。
- `patch_build_script` 要求已有文件、非空且唯一的旧片段、实际变化的新片段，并限制单个片段
  最大 64 KiB；它不会附带修改文件中其他 apt 命令。
- workflow 在执行前验证 whole-file 完整证明或 exact patch 的 prompt-visible 锚点；策略拒绝
  进入 M7 同轮反馈，不消耗 rebuild/test 机会。
- 结构化 system/Python/base-image/Testability overlay 工具从同一字节快照生成内容和 digest，
  避免“读旧内容、给新哈希”的 TOCTOU。
- 多轮候选合并保留每个路径最早的 `before_digest` 和最终的 `after_digest`；promotion 同时验证
  accepted workspace 的 before digest 与 candidate 的 after digest。
- Testability 仍不暴露任何 build-script mutation；`patch_build_script` 只进入 M7 已许可的
  build/install/runtime fallback 空间，且 timeout 场景优先 exact patch 而非 whole-file。

## 真实数据集隔离验证

使用 M0 中来自 ExecutionAgent/CNB 的真实 C++ 项目：

- 项目：`ccache/ccache`
- 项目目录：
  `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos/executionagent-ccache-ccache-7f3e822efb1b`
- 验证文件：`dockerfiles/ubuntu-24.04/Dockerfile`
- 原始 SHA-256：`1363b76cbf074e962c32c702fd432fac724b55e7ceb787ab39e499633d57dcf2`

验证只复制该文件到 `/tmp/dprauto-m8-ccache-JSCJ00` 的隔离目录。生产 `ReadFileTool` 返回：

```text
complete_file = true
total_lines = 28
source_sha256 = 1363b76cbf074e962c32c702fd432fac724b55e7ceb787ab39e499633d57dcf2
```

随后用生产 `PatchBuildScriptTool` 精确替换一条注释。保存的 unified diff 只有该注释变化，
Dockerfile 中已有 apt 命令没有被连带改写。再次以旧 SHA 修改另一个片段时返回
`source changed before mutation`。原始 ccache 数据集文件保持上述 SHA 和原文不变；临时目录及
artifact 已移入回收站，可恢复。

## 自动验证

- CAS、分页读取、prompt-visible 锚点、候选提升、多轮 checkpoint、环境策略聚焦回归：
  `97 passed, 10 subtests passed in 17.16s`。
- Docker Agent 集成测试使用本地镜像时继续通过，并更新 scripted plan 携带真实源 SHA。
- `compileall` 和 `git diff --check` 在提交前执行。
- 全仓库回归：`366 passed, 5 skipped, 136 subtests passed in 74.96s`；唯一 warning 来自
  `test_regression_checker.py` 加载的既有 OpenLane 数据中的无效转义弃用提示，与 M8 无关。

## 尚未声称解决

- `patch_build_script` 是有界精确文本替换，不是任意 unified diff、AST rewrite 或模糊 patch；
  多个不相邻修改必须重新读取新 SHA 后进入下一修复轮。
- 默认仍禁止业务源码修改；M8 没有扩大 Agent 到任意文件或任意 shell。
- 文件系统没有跨进程事务锁。promotion 在替换前验证 accepted digest，并以临时文件原子替换；
  不把它描述为分布式锁或多主编辑协议。
- 本阶段没有调用外部 LLM，也没有读取或修改 `myapi.json`；scripted/工具验证不冒充在线 Agent
  成功案例。

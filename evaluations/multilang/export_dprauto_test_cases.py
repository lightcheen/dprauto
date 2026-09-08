#!/usr/bin/env python3
"""Export reproducible source archives and M21 verification contracts."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable


_SCRIPT_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_SCRIPT_REPO_ROOT))
sys.path.insert(0, str(_SCRIPT_REPO_ROOT / "src"))

from evaluations.prompt12.run_evaluation import copy_workspace, source_tree_digest


RUN_NAME = "m21-full51-agent-workspacefix-20260907"
IGNORED_INPUT_NAMES = (
    ".git",
    ".agents",
    ".claude",
    ".codex",
    ".kiro",
    ".openclaw",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".tox",
    "dist",
    "build",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--start", type=int, default=2)
    parser.add_argument("--end", type=int, default=51)
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def clean(value: Any, default: str = "-") -> str:
    if value is None or value == "":
        return default
    return str(value)


def md_cell(value: Any) -> str:
    return clean(value).replace("|", "\\|").replace("\n", "<br>")


def seconds(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value):.3f} 秒"


def status(value: Any) -> str:
    return clean(value, "not_run").upper()


def command_text(result: dict[str, Any] | None) -> str:
    if not result:
        return ""
    command = result.get("command") or {}
    argv = command.get("argv") or []
    return " ".join(str(item) for item in argv)


def fenced(text: str, language: str = "sh") -> str:
    return f"````{language}\n{text}\n````"


def record_entries(run_root: Path) -> list[tuple[int, str, Path]]:
    entries: list[tuple[int, str, Path]] = []
    for local_index, path in enumerate(
        sorted((run_root / "original21" / "records").glob("*.json")), start=1
    ):
        entries.append((local_index, "original21", path))
    for local_index, path in enumerate(
        sorted((run_root / "highstar30" / "records").glob("*.json")), start=1
    ):
        entries.append((21 + local_index, "highstar30", path))
    if len(entries) != 51:
        raise RuntimeError(f"expected 51 records, found {len(entries)}")
    return entries


def slug_from_record(path: Path) -> str:
    return re.sub(r"^\d+-", "", path.stem).lower()


def project_basename(repo: str) -> str:
    value = repo.rsplit("/", 1)[-1].lower()
    return re.sub(r"[^a-z0-9._-]+", "-", value).strip("-") or "source"


def evidence_refs(report: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not report:
        return []
    found: dict[str, dict[str, Any]] = {}
    containers: list[Iterable[dict[str, Any]]] = [report.get("evidence") or []]
    for check in report.get("checks") or []:
        containers.append(check.get("evidence") or [])
        for stream_name in ("stdout", "stderr"):
            stream = (check.get("command_result") or {}).get(stream_name)
            if stream:
                containers.append([stream])
    for stream_name in ("stdout", "stderr"):
        stream = (report.get("command_result") or {}).get(stream_name)
        if stream:
            containers.append([stream])
    for container in containers:
        for item in container:
            key = item.get("key")
            if key:
                found[key] = item
    return [found[key] for key in sorted(found)]


def expected_check(name: str) -> str:
    expectations = {
        "project-tests": "测试准备和项目测试命令完成；不超时；退出码 0；有真实测试证据且无 failed/error",
        "cli-exit-code": "安全 CLI 探针在超时内完成并返回退出码 0",
        "cli-output": "CLI 产生非空且有语义的帮助、版本或命令输出",
        "library-import": "项目库可在容器内被解释器导入，命令退出码为 0",
        "library-api-or-tests": "观察到有效库 API，或存在通过的强项目测试证据",
        "library-artifact": "容器内存在预期的已编译库产物，产物探针退出码为 0",
        "library-artifact-or-tests": "观察到已编译产物内容，或存在通过的强项目测试证据",
        "web-process": "服务进程在启动观察期内保持运行",
        "web-port": "容器目标端口能够接受连接",
        "web-http": "HTTP 探针在启动预算内获得可接受响应状态",
        "runnability": "运行探针基础设施成功完成，并产生足以判断运行性的证据",
    }
    return expectations.get(name, "检查成功完成并满足该检查在评测记录中定义的运行契约")


def render_evidence(
    report: dict[str, Any] | None, dataset_root: Path, repo_root: Path
) -> list[str]:
    refs = evidence_refs(report)
    if not refs:
        return ["没有独立日志制品引用。"]
    lines = [
        "| 日志制品 | SHA-256 | 大小 | 本地校验 |",
        "| --- | --- | ---: | --- |",
    ]
    for item in refs:
        relative = Path("artifacts") / item["key"]
        absolute = dataset_root / relative
        valid = absolute.is_file() and sha256_file(absolute) == item.get("digest")
        display = absolute.relative_to(repo_root) if absolute.is_relative_to(repo_root) else absolute
        lines.append(
            f"| `{md_cell(display)}` | `{md_cell(item.get('digest'))}` | "
            f"{md_cell(item.get('size_bytes'))} | {'通过' if valid else '失败/缺失'} |"
        )
    return lines


def render_output_excerpts(report: dict[str, Any]) -> list[str]:
    excerpts: list[str] = []
    for check in report.get("checks") or []:
        excerpt = clean((check.get("metadata") or {}).get("output_excerpt"), "")
        if excerpt and excerpt not in excerpts:
            excerpts.append(excerpt)
    if not excerpts:
        return []
    lines = ["### 阶段输出摘录", ""]
    for index, excerpt in enumerate(excerpts, start=1):
        if len(excerpt) > 4000:
            excerpt = "[评测记录仅保留末尾/有界输出]\n" + excerpt[-4000:]
        lines.extend([f"输出摘录 {index}：", "", fenced(excerpt.rstrip(), "text"), ""])
    return lines


def render_testability(
    report: dict[str, Any] | None,
    final_result: dict[str, Any],
    dataset_root: Path,
    repo_root: Path,
) -> list[str]:
    lines = ["## 3. Testability 阶段", ""]
    if not report:
        lines.extend(
            [
                "**状态：NOT RUN**",
                "",
                "本项目在获得可供验证的最终 Docker 镜像之前已经终止，因此 Testability 阶段没有执行任何测试命令，也没有实际测试结果。",
                "",
                "预期的阶段入口条件是 Docker 镜像构建成功。只有满足该条件后，DPRAuto 才能选择项目测试命令，并以“不超时、退出码 0、存在真实测试执行证据、没有 failed/error”作为通过标准。本次不能把“未执行”记作通过或失败。",
                "",
                f"- 最终状态：`{clean(final_result.get('final_status'))}`",
                f"- 终止原因：{clean(final_result.get('stop_reason'))}",
                "",
            ]
        )
        return lines

    metadata = report.get("metadata") or {}
    targets = metadata.get("selection_targets") or []
    attempts = metadata.get("command_attempts") or []
    checks = report.get("checks") or []
    lines.extend(
        [
            f"**状态：{status(report.get('status'))}**",
            "",
            "### 测试选择与范围",
            "",
            f"- verification ID：`{clean(report.get('verification_id'))}`",
            f"- 原始候选命令：`{clean(metadata.get('original_command'))}`",
            f"- 命令来源：`{clean(metadata.get('command_source') or metadata.get('source'))}`",
            f"- 选择方式：`{clean(metadata.get('selection_kind'))}`",
            f"- 选择原因：{clean(metadata.get('selection_reason'))}",
            f"- 候选数 / 实际尝试数：{clean(metadata.get('candidate_count'))} / {clean(metadata.get('attempted_candidate_count'))}",
            f"- fallback：{clean(metadata.get('candidate_fallback_used'))}",
            f"- 有效超时预算：{clean(metadata.get('effective_timeout_seconds'))} 秒",
            "",
        ]
    )
    if targets:
        lines.extend(
            [
                "实际选择的全部测试目标：",
                "",
                "| # | 测试目标 | 每个目标的预期结果 |",
                "| ---: | --- | --- |",
            ]
        )
        for index, target in enumerate(targets, start=1):
            lines.append(
                f"| {index} | `{md_cell(target)}` | 目标存在并可被测试工具收集/执行；阶段不得因该目标产生 failed/error |"
            )
        lines.append("")
    else:
        lines.extend(
            [
                "评测记录没有文件级选择目标；测试范围由下方项目命令自身定义。",
                "",
            ]
        )

    lines.extend(["### 实际执行的全部测试尝试", ""])
    if attempts:
        for position, attempt in enumerate(attempts, start=1):
            executed = clean(attempt.get("executed_command") or attempt.get("command"))
            lines.extend(
                [
                    f"#### 尝试 {position}（候选 {clean(attempt.get('candidate'))}）",
                    "",
                    fenced(executed),
                    "",
                    "预期：准备步骤和测试命令成功，在预算内结束，退出码为 0；必须观察到真实测试证据，且不得出现 failed/error。",
                    "",
                    "| 实际字段 | 值 |",
                    "| --- | --- |",
                    f"| 状态 | `{md_cell(attempt.get('status'))}` |",
                    f"| 摘要 | {md_cell(attempt.get('summary'))} |",
                    f"| 退出码 / 超时 | `{md_cell(attempt.get('exit_code'))}` / `{md_cell(attempt.get('timed_out'))}` |",
                    f"| 耗时 | {seconds(attempt.get('duration_seconds'))} |",
                    f"| 观测测试数 | {md_cell(attempt.get('observed_test_count'))} |",
                    f"| 结果类别 | `{md_cell(attempt.get('outcome_category'))}` |",
                    f"| 证据强度 | `{md_cell(attempt.get('evidence_strength'))}` |",
                    "",
                ]
            )
    else:
        exact = clean(metadata.get("command") or command_text(report.get("command_result")), "")
        if exact:
            lines.extend([fenced(exact), ""])
        lines.extend(["记录中没有独立的 `command_attempts` 条目。", ""])

    lines.extend(
        [
            "### 全部判定检查及预期结果",
            "",
            "| # | 检查 | 预期结果 | 实际状态 | 实际摘要 |",
            "| ---: | --- | --- | --- | --- |",
        ]
    )
    for index, check in enumerate(checks, start=1):
        name = clean(check.get("name"))
        lines.append(
            f"| {index} | `{md_cell(name)}` | {md_cell(expected_check(name))} | "
            f"`{md_cell(check.get('status'))}` | {md_cell(check.get('summary'))} |"
        )
    if not checks:
        lines.append("| - | 无 | 没有可执行检查 | - | - |")
    lines.extend(
        [
            "",
            "### 阶段结果",
            "",
            "| 字段 | 实际值 |",
            "| --- | --- |",
            f"| 状态 | `{md_cell(report.get('status'))}` |",
            f"| 摘要 | {md_cell(report.get('summary'))} |",
            f"| 结果类别 | `{md_cell(metadata.get('outcome_category'))}` |",
            f"| 观测测试数 | {md_cell(metadata.get('observed_test_count'))} |",
            f"| 证据范围 | `{md_cell(metadata.get('test_evidence_scope'))}` |",
            f"| 证据强度 | `{md_cell(metadata.get('test_evidence_strength'))}` |",
            f"| 最终命令退出码 / 超时 | `{md_cell((report.get('command_result') or {}).get('exit_code'))}` / `{md_cell((report.get('command_result') or {}).get('timed_out'))}` |",
            f"| 最终命令耗时 | {seconds((report.get('command_result') or {}).get('duration_seconds'))} |",
            "",
            "### 原始证据",
            "",
            *render_evidence(report, dataset_root, repo_root),
            "",
        ]
    )
    lines.extend(render_output_excerpts(report))
    return lines


def render_runnability(
    report: dict[str, Any] | None,
    final_result: dict[str, Any],
    dataset_root: Path,
    repo_root: Path,
) -> list[str]:
    lines = ["## 4. Runnability 阶段", ""]
    if not report:
        lines.extend(
            [
                "**状态：NOT RUN**",
                "",
                "本项目没有获得可供运行验证的最终 Docker 镜像，因此 Runnability 阶段没有启动容器、服务或 CLI，也没有实际运行结果。",
                "",
                "预期的阶段入口条件是镜像构建成功。进入阶段后，具体预期由项目运行契约决定，例如 CLI 必须正常退出并产生有效输出，服务必须保持进程、开放端口并通过 HTTP 探针，库项目必须可导入或产生可验证的编译产物。",
                "",
                f"- 最终状态：`{clean(final_result.get('final_status'))}`",
                f"- 终止原因：{clean(final_result.get('stop_reason'))}",
                "",
            ]
        )
        return lines

    metadata = report.get("metadata") or {}
    checks = report.get("checks") or []
    exact = clean(
        metadata.get("executed_command")
        or metadata.get("selected_command")
        or command_text(report.get("command_result")),
        "",
    )
    lines.extend(
        [
            f"**状态：{status(report.get('status'))}**",
            "",
            "### 运行契约与命令",
            "",
            f"- verification ID：`{clean(report.get('verification_id'))}`",
            f"- 运行契约：`{clean(metadata.get('runtime_contract'))}`",
            f"- 探针来源：`{clean(metadata.get('runtime_probe_source'))}`",
            f"- 证据范围：`{clean(metadata.get('runtime_evidence_scope'))}`",
            f"- 选择命令：`{clean(metadata.get('selected_command'))}`",
            "",
        ]
    )
    if exact:
        lines.extend(["实际执行或启动的命令：", "", fenced(exact), ""])
    else:
        lines.extend(["评测记录没有单一命令文本；判定来自下列容器/服务检查。", ""])

    lines.extend(
        [
            "### 全部检查及预期结果",
            "",
            "| # | 检查 | 预期结果 | 实际状态 | 实际摘要 |",
            "| ---: | --- | --- | --- | --- |",
        ]
    )
    for index, check in enumerate(checks, start=1):
        name = clean(check.get("name"))
        lines.append(
            f"| {index} | `{md_cell(name)}` | {md_cell(expected_check(name))} | "
            f"`{md_cell(check.get('status'))}` | {md_cell(check.get('summary'))} |"
        )
    if not checks:
        lines.append("| - | 无 | 没有可执行检查 | - | - |")
    command_result = report.get("command_result") or {}
    lines.extend(
        [
            "",
            "### 阶段结果",
            "",
            "| 字段 | 实际值 |",
            "| --- | --- |",
            f"| 状态 | `{md_cell(report.get('status'))}` |",
            f"| 摘要 | {md_cell(report.get('summary'))} |",
            f"| 结果类别 | `{md_cell(metadata.get('runtime_outcome_category'))}` |",
            f"| 证据强度 | `{md_cell(metadata.get('runtime_evidence_strength'))}` |",
            f"| 语义证明 | `{md_cell(metadata.get('runtime_semantically_proven'))}` |",
            f"| 退出码 / 超时 | `{md_cell(metadata.get('exit_code', command_result.get('exit_code')))}` / `{md_cell(metadata.get('timed_out', command_result.get('timed_out')))}` |",
            f"| 耗时 | {seconds(metadata.get('duration_seconds', command_result.get('duration_seconds')))} |",
            f"| HTTP 状态 | {md_cell(metadata.get('http_status'))} |",
            f"| 容器端口 | {md_cell(metadata.get('container_port'))} |",
            f"| 产物数量 | {md_cell(metadata.get('artifact_count'))} |",
            "",
            "### 原始证据",
            "",
            *render_evidence(report, dataset_root, repo_root),
            "",
        ]
    )
    lines.extend(render_output_excerpts(report))
    return lines


def failure_summary(record: dict[str, Any], final_result: dict[str, Any]) -> str:
    failure = final_result.get("failure") or record.get("error") or record.get("initial_failure")
    if not failure:
        return "-"
    if isinstance(failure, str):
        return failure
    for key in ("summary", "message", "reason", "category", "error"):
        if failure.get(key):
            return str(failure[key])
    return json.dumps(failure, ensure_ascii=False, sort_keys=True)[:1000]


def render_document(
    *,
    record: dict[str, Any],
    record_path: Path,
    dataset: str,
    dataset_root: Path,
    repo_root: Path,
    zip_name: str,
    zip_sha256: str,
    zip_size: int,
    zip_entries: int,
    archive_root: str,
) -> str:
    final_result = record.get("final_result") or {}
    identity = record.get("evaluation_identity") or {}
    payload = identity.get("payload") or {}
    source_identity = payload.get("source") or {}
    reference = record.get("source_reference") or source_identity.get("reference") or {}
    profile = final_result.get("project_profile") or {}
    build_result = final_result.get("build_result") or record.get("standard_build") or {}
    command_results = build_result.get("command_results") or []
    build_duration = sum(float(item.get("duration_seconds") or 0) for item in command_results)
    image = build_result.get("image_reference") or (build_result.get("metadata") or {}).get("target_image")
    record_display = record_path.relative_to(repo_root)
    languages = ", ".join(profile.get("languages") or []) or "-"
    package_managers = ", ".join(profile.get("package_managers") or []) or "-"
    tree_sha = clean(source_identity.get("tree_sha256"))
    ignored = "、".join(f"`{item}`" for item in IGNORED_INPUT_NAMES)

    lines = [
        f"# {record.get('repo')}：Testability 与 Runnability 测试说明",
        "",
        "## 1. 项目、构建与样本身份",
        "",
        "| 字段 | 值 |",
        "| --- | --- |",
        f"| 项目 | `{md_cell(record.get('repo'))}` |",
        f"| 语言 | {md_cell(languages)} |",
        f"| 项目类型 | `{md_cell(profile.get('project_type'))}` |",
        f"| 包管理器 | {md_cell(package_managers)} |",
        f"| 数据集 | `{dataset}` |",
        f"| 固定 revision | `{md_cell(reference.get('revision'))}` |",
        f"| DPRAuto 评测运行 | `{RUN_NAME}` |",
        f"| 评测身份摘要 | `{md_cell(identity.get('digest'))}` |",
        f"| 源码树 SHA-256 | `{tree_sha}` |",
        f"| 标准构建状态 | `{md_cell((record.get('standard_build') or {}).get('status'))}` |",
        f"| 最终状态 | `{md_cell(final_result.get('final_status'))}` |",
        f"| 构建策略 | `{md_cell(final_result.get('build_strategy') or (build_result.get('metadata') or {}).get('strategy'))}` |",
        f"| Agent / LLM | Agent={md_cell(final_result.get('agent_participated'))}；LLM calls={md_cell(final_result.get('llm_call_count'))} |",
        f"| Docker 镜像 | `{md_cell(image)}` |",
        f"| 构建命令累计耗时 | {seconds(build_duration) if command_results else '-'} |",
        f"| 终止原因 | {md_cell(final_result.get('stop_reason'))} |",
        f"| 失败摘要 | {md_cell(failure_summary(record, final_result))} |",
        "",
        "本文件只描述上述固定 revision 在 M21 中的实际执行情况和验收契约，不代表其他 revision。`FAILED`、`ERROR` 与 `NOT RUN` 会原样保留，不会改写为成功。",
        "",
        "## 2. 源码压缩包",
        "",
        f"同目录文件：`{zip_name}`",
        "",
        "| 校验项 | 值 |",
        "| --- | --- |",
        f"| ZIP SHA-256 | `{zip_sha256}` |",
        f"| ZIP 大小 | {zip_size:,} bytes |",
        f"| ZIP 条目数 | {zip_entries} |",
        f"| 解压后的顶层目录 | `{archive_root}/` |",
        f"| 解压源码树 SHA-256 | `{tree_sha}` |",
        "",
        "该 ZIP 是环境自动构建的源码输入快照。它使用与 M21 隔离工作区相同的复制规则，保留源码和符号链接，并排除以下非输入目录：",
        "",
        ignored,
        "",
        "归档前已重新计算解压源码树摘要，并确认与项目评测身份中的源码树摘要完全一致。若原始输入包含 `.cnb-benchmark-source-ready`，该来源标记会保留；DPRAuto 生成 Docker 构建上下文时会单独排除它。",
        "",
    ]
    lines.extend(
        render_testability(
            final_result.get("testability"), final_result, dataset_root, repo_root
        )
    )
    lines.extend(
        render_runnability(
            final_result.get("runnability"), final_result, dataset_root, repo_root
        )
    )
    lines.extend(
        [
            "## 5. 总结与边界",
            "",
            f"- 构建最终状态：`{clean(final_result.get('final_status'))}`。",
            f"- Testability：`{clean((final_result.get('testability') or {}).get('status'), 'not_run')}`。",
            f"- Runnability：`{clean((final_result.get('runnability') or {}).get('status'), 'not_run')}`。",
            "- Testability 的命令和目标只代表 M21 当次实际范围；有界抽样不等同于仓库全量回归测试。",
            "- Runnability 只证明所记录运行契约；CLI help、库导入或编译产物探针不等同于完整业务/硬件/集成测试。",
            "",
            "## 6. 原始记录定位",
            "",
            f"- 项目记录：`{record_display}`",
            f"- 制品根目录：`{dataset_root.relative_to(repo_root)}/artifacts`",
            "",
        ]
    )
    return "\n".join(lines)


def export_one(
    global_index: int,
    dataset: str,
    record_path: Path,
    repo_root: Path,
    output_root: Path,
) -> None:
    record = json.loads(record_path.read_text(encoding="utf-8"))
    repo = clean(record.get("repo"))
    reference = record.get("source_reference") or {}
    revision = clean(reference.get("revision"))
    revision_short = re.sub(r"[^a-zA-Z0-9._-]+", "-", revision[:12])
    record_slug = slug_from_record(record_path)
    source_name = f"{project_basename(repo)}-{revision_short}"
    folder = output_root / f"{global_index:02d}-{record_slug}"
    folder.mkdir(parents=True, exist_ok=True)
    expected_names = {f"{source_name}-source.zip", "TESTABILITY_AND_RUNNABILITY.md"}
    unexpected = {item.name for item in folder.iterdir()} - expected_names
    if unexpected:
        raise RuntimeError(f"unexpected files in {folder}: {sorted(unexpected)}")

    source = Path(record["source_path"])
    expected_tree_sha = record["evaluation_identity"]["payload"]["source"]["tree_sha256"]
    zip_name = f"{source_name}-source.zip"
    zip_path = folder / zip_name
    if zip_path.exists():
        zip_path.unlink()

    with tempfile.TemporaryDirectory(prefix=f"dprauto-export-{global_index:02d}-") as temp_name:
        temp_root = Path(temp_name)
        archive_source = temp_root / source_name
        copy_workspace(source, archive_source)
        actual_tree_sha = source_tree_digest(archive_source)
        if actual_tree_sha != expected_tree_sha:
            raise RuntimeError(
                f"{repo}: source tree mismatch {actual_tree_sha} != {expected_tree_sha}"
            )
        subprocess.run(
            ["zip", "-y", "-q", "-r", str(zip_path), archive_source.name],
            cwd=temp_root,
            check=True,
        )

    subprocess.run(["unzip", "-tq", str(zip_path)], check=True, stdout=subprocess.DEVNULL)
    zip_sha = sha256_file(zip_path)
    names = subprocess.run(
        ["unzip", "-Z", "-1", str(zip_path)],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.splitlines()
    if not names or not all(name.startswith(f"{source_name}/") for name in names):
        raise RuntimeError(f"{repo}: invalid archive root")

    dataset_root = record_path.parent.parent
    document = render_document(
        record=record,
        record_path=record_path,
        dataset=dataset,
        dataset_root=dataset_root,
        repo_root=repo_root,
        zip_name=zip_name,
        zip_sha256=zip_sha,
        zip_size=zip_path.stat().st_size,
        zip_entries=len(names),
        archive_root=source_name,
    )
    (folder / "TESTABILITY_AND_RUNNABILITY.md").write_text(document, encoding="utf-8")
    if sorted(item.name for item in folder.iterdir()) != sorted(expected_names):
        raise RuntimeError(f"{repo}: output folder does not contain exactly two files")
    print(
        f"[{global_index:02d}/51] {repo}: zip={zip_path.stat().st_size:,} "
        f"testability={clean(((record.get('final_result') or {}).get('testability') or {}).get('status'), 'not_run')} "
        f"runnability={clean(((record.get('final_result') or {}).get('runnability') or {}).get('status'), 'not_run')}",
        flush=True,
    )


def main() -> None:
    args = parse_args()
    repo_root = args.repo_root.resolve()
    run_root = repo_root / "evaluations" / "multilang" / "runs" / RUN_NAME
    output_root = repo_root / "dprauto_test"
    output_root.mkdir(parents=True, exist_ok=True)
    for global_index, dataset, record_path in record_entries(run_root):
        if args.start <= global_index <= args.end:
            export_one(
                global_index,
                dataset,
                record_path,
                repo_root,
                output_root,
            )


if __name__ == "__main__":
    main()

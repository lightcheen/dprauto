#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
MANIFEST="$SCRIPT_DIR/manifest-high-star-30-20260903.json"
RUN_ID="${DPRAUTO_RUN_ID:-m16-phase6-deterministic-c9c629c-20260904}"
INDICES="${DPRAUTO_INDICES:-1-30}"
NATIVE_BASE_IMAGE="${DPRAUTO_NATIVE_BASE_IMAGE:-debian:bookworm-slim}"
RUN_DIR="$SCRIPT_DIR/runs/$RUN_ID"
SUMMARY="$RUN_DIR/summary.json"
RECORD_DIR="$RUN_DIR/records"
LOG_FILE="$RUN_DIR/background.log"
PID_FILE="$RUN_DIR/background.pid"
LOCK_FILE="$RUN_DIR/background.lock"
if [[ ! "$INDICES" =~ ^([1-9][0-9]*)-([1-9][0-9]*)$ ]]; then
  echo "DPRAUTO_INDICES 必须是连续范围，例如 1-30 或 21-30。" >&2
  exit 2
fi
START_INDEX="${BASH_REMATCH[1]}"
END_INDEX="${BASH_REMATCH[2]}"
if (( END_INDEX < START_INDEX || END_INDEX > 30 )); then
  echo "DPRAUTO_INDICES 超出 manifest 范围: $INDICES" >&2
  exit 2
fi
EXPECTED=$((END_INDEX - START_INDEX + 1))

runner_pattern="[r]un_execution.py .*--manifest .*manifest-high-star-30-20260903.json .*--output .*$RUN_ID"

is_complete() {
  [[ -f "$SUMMARY" ]] && jq -e --argjson expected "$EXPECTED" \
    '.complete == true and .record_count == $expected' "$SUMMARY" >/dev/null 2>&1
}

runner_pids() {
  pgrep -f "$runner_pattern" 2>/dev/null || true
}

record_count() {
  if [[ ! -d "$RECORD_DIR" ]]; then
    echo 0
    return 0
  fi
  find "$RECORD_DIR" -maxdepth 1 -type f -name '*.json' -printf '.' 2>/dev/null | wc -c
}

show_status() {
  local done_count running next_index next_repo
  done_count="$(record_count | tr -d ' ')"
  running="$(runner_pids | paste -sd, -)"

  echo "运行目录: $RUN_DIR"
  echo "进度: ${done_count}/${EXPECTED}"
  if [[ -n "$running" ]]; then
    echo "执行器: 运行中 (PID: $running)"
  elif is_complete; then
    echo "执行器: 已完成"
  else
    echo "执行器: 未运行或已中断，可执行: $0 start"
  fi

  if (( done_count < EXPECTED )); then
    next_index=$((START_INDEX + done_count))
    next_repo="$(jq -r --argjson index "$next_index" '.cases[$index - 1].repo' "$MANIFEST")"
    echo "当前/下一项目: [$next_index] $next_repo"
  fi

  if [[ -f "$SUMMARY" ]]; then
    jq -r '
      "构建成功: \(.standard_build_success)/\(.record_count)",
      "完整环境成功: \(.environment_success)/\(.record_count)",
      "严格测试成功: \(.strict_test_success)/\(.record_count)",
      "最终状态: " + (.outcomes | to_entries | map("\(.key)=\(.value)") | join(", ")),
      "Installability: " + (.verification_layers.installability | to_entries | map("\(.key)=\(.value)") | join(", ")),
      "Testability: " + (.verification_layers.testability | to_entries | map("\(.key)=\(.value)") | join(", ")),
      "Runnability: " + (.verification_layers.runnability | to_entries | map("\(.key)=\(.value)") | join(", ")),
      "LLM: " + (if .llm_enabled then "启用" else "未启用" end)
    ' "$SUMMARY"
  fi
}

run_evaluation() {
  cd "$REPO_DIR"
  PYTHONPATH=src python3 evaluations/multilang/run_execution.py \
    --manifest "$MANIFEST" \
    --output "$RUN_DIR" \
    --indices "$INDICES" \
    --docker-network host \
    --image-repository "dprauto/multilang-$RUN_ID" \
    --native-base-image "$NATIVE_BASE_IMAGE" \
    --maven-base-image 'maven:3.9-eclipse-temurin-{version}' \
    --gradle-base-image 'maven:3.9-eclipse-temurin-{version}'
}

worker() {
  mkdir -p "$RUN_DIR" "$RECORD_DIR"
  exec 9>"$LOCK_FILE"
  if ! flock -n 9; then
    echo "[$(date -Is)] 已有守护进程持有锁，退出。"
    exit 0
  fi
  echo "$$" >"$PID_FILE"
  trap 'rm -f "$PID_FILE"' EXIT

  local attempt=0 existing
  while ! is_complete; do
    existing="$(runner_pids | paste -sd, -)"
    if [[ -n "$existing" ]]; then
      echo "[$(date -Is)] 检测到现有执行器 PID=$existing，等待并监护。"
      while [[ -n "$(runner_pids)" ]] && ! is_complete; do
        sleep 10
      done
      continue
    fi

    attempt=$((attempt + 1))
    if (( attempt > 3 )); then
      echo "[$(date -Is)] 执行器连续异常退出 3 次，停止自动重启；请检查日志。"
      exit 1
    fi
    echo "[$(date -Is)] 启动或断点续跑（第 $attempt 次启动）。"
    if run_evaluation; then
      echo "[$(date -Is)] 执行器正常退出。"
    else
      rc=$?
      echo "[$(date -Is)] 执行器异常退出，退出码=$rc；10 秒后从已有 records 续跑。"
      sleep 10
    fi
  done
  echo "[$(date -Is)] $EXPECTED 个项目已全部生成记录。"
}

start_background() {
  mkdir -p "$RUN_DIR" "$RECORD_DIR"
  if is_complete; then
    echo "$EXPECTED 个项目已经完成，不重复执行。"
    show_status
    return 0
  fi
  if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "后台守护进程已经运行，PID=$(cat "$PID_FILE")。"
    show_status
    return 0
  fi
  nohup "$0" _worker >>"$LOG_FILE" 2>&1 </dev/null &
  echo "后台守护进程已启动，PID=$!"
  echo "查看进度: $0 status"
  echo "跟踪日志: $0 log"
}

case "${1:-status}" in
  start|resume)
    start_background
    ;;
  status)
    show_status
    ;;
  log)
    mkdir -p "$RUN_DIR"
    touch "$LOG_FILE"
    tail -n 80 -f "$LOG_FILE"
    ;;
  foreground)
    if is_complete; then
      echo "$EXPECTED 个项目已经完成，不重复执行。"
      show_status
    else
      run_evaluation
    fi
    ;;
  _worker)
    worker
    ;;
  *)
    echo "用法: $0 {start|resume|status|log|foreground}" >&2
    exit 2
    ;;
esac

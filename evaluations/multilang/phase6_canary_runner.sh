#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
MANIFEST="$SCRIPT_DIR/manifest-high-star-30-20260903.json"
RUN_ID="${DPRAUTO_RUN_ID:-m18-phase6-runnability-canary-c9c629c-20260906}"
INDICES="${DPRAUTO_INDICES:-10,14,28}"
NATIVE_BASE_IMAGE="${DPRAUTO_NATIVE_BASE_IMAGE:-docker.io/library/debian:bookworm-slim@sha256:88200866dfff7ea7f5cbcb6ec7c8a701889efe6fe859fe64d6990e4b07ea4171}"
RUN_DIR="$SCRIPT_DIR/runs/$RUN_ID"
SUMMARY="$RUN_DIR/summary.json"
RECORD_DIR="$RUN_DIR/records"
LOG_FILE="$RUN_DIR/background.log"
PID_FILE="$RUN_DIR/background.pid"
LOCK_FILE="$RUN_DIR/background.lock"

mapfile -t SELECTED < <(
  python3 - "$INDICES" <<'PY'
import re
import sys

selected = set()
for value in sys.argv[1].split(","):
    match = re.fullmatch(r"(\d+)(?:-(\d+))?", value.strip())
    if not match:
        raise SystemExit(f"invalid index or range: {value!r}")
    start = int(match.group(1))
    end = int(match.group(2) or start)
    if start < 1 or end > 30 or start > end:
        raise SystemExit(f"index range must be within 1-30: {value!r}")
    selected.update(range(start, end + 1))
print(*sorted(selected), sep="\n")
PY
)
EXPECTED="${#SELECTED[@]}"
if (( EXPECTED == 0 )); then
  echo "DPRAUTO_INDICES 至少需要一个索引。" >&2
  exit 2
fi

runner_pattern="[r]un_execution.py .*--output $RUN_DIR"

record_count() {
  if [[ ! -d "$RECORD_DIR" ]]; then
    echo 0
    return
  fi
  find "$RECORD_DIR" -maxdepth 1 -type f -name '*.json' -printf '.' 2>/dev/null | wc -c
}

is_complete() {
  [[ -f "$SUMMARY" ]] && jq -e --argjson expected "$EXPECTED" \
    '.complete == true and .record_count == $expected' "$SUMMARY" >/dev/null 2>&1
}

runner_pids() {
  pgrep -f "$runner_pattern" 2>/dev/null || true
}

show_status() {
  local done_count running next_index next_repo
  done_count="$(record_count | tr -d ' ')"
  running="$(runner_pids | paste -sd, -)"
  echo "运行目录: $RUN_DIR"
  echo "选定索引: $INDICES"
  echo "进度: ${done_count}/${EXPECTED}"
  if [[ -n "$running" ]]; then
    echo "执行器: 运行中 (PID: $running)"
  elif is_complete; then
    echo "执行器: 已完成"
  else
    echo "执行器: 未运行或已中断，可执行: $0 start"
  fi
  if (( done_count < EXPECTED )); then
    next_index="${SELECTED[$done_count]}"
    next_repo="$(jq -r --argjson index "$next_index" '.cases[$index - 1].repo' "$MANIFEST")"
    echo "当前/下一项目: [$next_index] $next_repo"
  fi
  if [[ -f "$SUMMARY" ]]; then
    jq -r '
      "构建成功: \(.standard_build_success)/\(.record_count)",
      "环境状态成功: \(.environment_success)/\(.record_count)",
      "严格三层成功: \(.strict_test_success)/\(.record_count)",
      "语义 Runnability: \(.runnability_semantically_proven)/\(.record_count)",
      "仅产物证据: \(.artifact_only_runnability_pass)",
      "运行证据强度: " + (.runtime_evidence_strength_distribution | to_entries | map("\(.key)=\(.value)") | join(", "))
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
    --gradle-base-image 'maven:3.9-eclipse-temurin-{version}' \
    --retain-images
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
  local attempt=0
  while ! is_complete; do
    attempt=$((attempt + 1))
    if (( attempt > 3 )); then
      echo "[$(date -Is)] 连续异常退出 3 次，停止自动重启。"
      exit 1
    fi
    echo "[$(date -Is)] 启动或断点续跑（第 $attempt 次）。"
    if run_evaluation; then
      echo "[$(date -Is)] 执行器正常退出。"
    else
      rc=$?
      echo "[$(date -Is)] 执行器退出码=$rc，10 秒后断点续跑。"
      sleep 10
    fi
  done
  echo "[$(date -Is)] 三语言 canary 已完成。"
}

start_background() {
  mkdir -p "$RUN_DIR" "$RECORD_DIR"
  if is_complete; then
    show_status
    return
  fi
  if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    show_status
    return
  fi
  nohup "$0" _worker >>"$LOG_FILE" 2>&1 </dev/null &
  echo "后台守护进程已启动，PID=$!"
  echo "查看进度: $0 status"
}

case "${1:-status}" in
  start|resume) start_background ;;
  status) show_status ;;
  log)
    mkdir -p "$RUN_DIR"
    touch "$LOG_FILE"
    tail -n 80 -f "$LOG_FILE"
    ;;
  foreground) run_evaluation ;;
  _worker) worker ;;
  *) echo "用法: $0 {start|resume|status|log|foreground}" >&2; exit 2 ;;
esac

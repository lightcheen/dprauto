#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
RUN_ID="${DPRAUTO_RUN_ID:-m20-full51-agent-now-20260907}"
RUN_DIR="$SCRIPT_DIR/runs/$RUN_ID"
ORIGINAL_DIR="$RUN_DIR/original21"
HIGHSTAR_DIR="$RUN_DIR/highstar30"
LOG_FILE="$RUN_DIR/background.log"
PID_FILE="$RUN_DIR/background.pid"
LOCK_FILE="$RUN_DIR/background.lock"

runner_pids() {
  pgrep -f "[r]un_agent_canary.py .*--output $RUN_DIR/" 2>/dev/null || true
}

summary_complete() {
  local path="$1"
  local expected="$2"
  [[ -f "$path" ]] && jq -e --argjson expected "$expected" \
    '.complete == true and .record_count == $expected and (.batch_stop_reason // "") == ""' \
    "$path" >/dev/null 2>&1
}

show_batch() {
  local label="$1"
  local directory="$2"
  local expected="$3"
  local summary="$directory/summary.json"
  local records=0
  if [[ -d "$directory/records" ]]; then
    records="$(find "$directory/records" -maxdepth 1 -type f -name '*.json' | wc -l | tr -d ' ')"
  fi
  echo "$label: $records/$expected"
  if [[ -f "$summary" ]]; then
    jq -r '
      "  确定性构建成功: \(.build_capability.standard_build_success)/\(.record_count)",
      "  进入 Agent: \(.agent_capability.entered_agent)",
      "  Agent 构建修复成功: \(.agent_capability.build_repair_success // 0)/\(.agent_capability.build_repair_candidates // 0)",
      "  最终环境成功: \(.agent_capability.final_environment_success)/\(.record_count)",
      "  严格 Testability: \(.verification.strict_testability_pass // .verification.testability_pass // 0)",
      "  语义 Runnability: \(.verification.semantic_runnability_pass // .verification.runnability_pass // 0)",
      "  LLM 调用: \(.cost.llm_calls)",
      (if (.batch_stop_reason // "") != "" then "  批次停止: \(.batch_stop_reason)" else empty end)
    ' "$summary"
  fi
}

show_status() {
  local running
  mkdir -p "$RUN_DIR"
  running="$(runner_pids | paste -sd, -)"
  echo "运行目录: $RUN_DIR"
  if [[ -n "$running" ]]; then
    echo "执行器: 运行中 (PID: $running)"
  elif summary_complete "$ORIGINAL_DIR/summary.json" 21 && \
       summary_complete "$HIGHSTAR_DIR/summary.json" 30; then
    echo "执行器: 51 项已完成"
  else
    echo "执行器: 未运行或已中断，可执行 $0 resume"
  fi
  show_batch "原 21 项" "$ORIGINAL_DIR" 21
  show_batch "新增 30 项" "$HIGHSTAR_DIR" 30
}

run_batch() {
  local manifest="$1"
  local output="$2"
  local indices="$3"
  PYTHONPATH=src python3 evaluations/multilang/run_agent_canary.py \
    --manifest "$manifest" \
    --output "$output" \
    --indices "$indices"
}

worker() {
  mkdir -p "$RUN_DIR"
  exec 9>"$LOCK_FILE"
  flock -n 9 || exit 0
  echo "$$" >"$PID_FILE"
  trap 'rm -f "$PID_FILE"' EXIT
  cd "$REPO_DIR"

  if ! summary_complete "$ORIGINAL_DIR/summary.json" 21; then
    run_batch evaluations/multilang/manifest.json "$ORIGINAL_DIR" 1-21
  fi
  if ! summary_complete "$ORIGINAL_DIR/summary.json" 21; then
    echo "原 21 项未完整结束；保留现有记录，停止批次。"
    exit 1
  fi

  if ! summary_complete "$HIGHSTAR_DIR/summary.json" 30; then
    run_batch evaluations/multilang/manifest-high-star-30-20260903.json \
      "$HIGHSTAR_DIR" 1-30
  fi
  if ! summary_complete "$HIGHSTAR_DIR/summary.json" 30; then
    echo "新增 30 项未完整结束；保留现有记录，停止批次。"
    exit 1
  fi

  echo "原 21 项和新增 30 项均已完成。"
}

start_background() {
  mkdir -p "$RUN_DIR"
  if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "后台执行器已经运行。"
    show_status
    return 0
  fi
  if summary_complete "$ORIGINAL_DIR/summary.json" 21 && \
     summary_complete "$HIGHSTAR_DIR/summary.json" 30; then
    echo "51 项已经完成，不重复执行。"
    show_status
    return 0
  fi
  nohup env DPRAUTO_RUN_ID="$RUN_ID" bash "$0" _worker \
    >>"$LOG_FILE" 2>&1 </dev/null &
  echo "后台 51 项 Agent/LLM 评测已启动，PID=$!"
  echo "查看状态: DPRAUTO_RUN_ID=$RUN_ID $0 status"
  echo "查看日志: DPRAUTO_RUN_ID=$RUN_ID $0 log"
}

case "${1:-status}" in
  start|resume) start_background ;;
  status) show_status ;;
  log)
    mkdir -p "$RUN_DIR"
    touch "$LOG_FILE"
    tail -n 100 -f "$LOG_FILE"
    ;;
  foreground) worker ;;
  _worker) worker ;;
  *) echo "用法: $0 {start|resume|status|log|foreground}" >&2; exit 2 ;;
esac

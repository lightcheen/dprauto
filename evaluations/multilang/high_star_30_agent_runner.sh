#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
RUN_ID="${DPRAUTO_RUN_ID:-m17-llm-canary-c9c629c-20260906}"
INDICES="${DPRAUTO_INDICES:-23,29,30}"
RUN_DIR="$SCRIPT_DIR/runs/$RUN_ID"
LOG_FILE="$RUN_DIR/background.log"
PID_FILE="$RUN_DIR/background.pid"
LOCK_FILE="$RUN_DIR/background.lock"
SUMMARY="$RUN_DIR/summary.json"

expected_count() {
  python3 - "$INDICES" <<'PY'
import sys
selected = set()
for part in sys.argv[1].split(','):
    bounds = [int(value) for value in part.strip().split('-', 1)]
    selected.update(range(bounds[0], bounds[-1] + 1))
print(len(selected))
PY
}

is_complete() {
  local expected
  expected="$(expected_count)"
  [[ -f "$SUMMARY" ]] && jq -e --argjson expected "$expected" \
    '.complete == true and .record_count == $expected' "$SUMMARY" >/dev/null 2>&1
}

runner_pids() {
  pgrep -f "[r]un_agent_canary.py .*--output $RUN_DIR" 2>/dev/null || true
}

show_status() {
  local expected done_count running
  mkdir -p "$RUN_DIR"
  expected="$(expected_count)"
  if [[ -d "$RUN_DIR/records" ]]; then
    done_count="$(find "$RUN_DIR/records" -maxdepth 1 -type f -name '*.json' | wc -l)"
  else
    done_count=0
  fi
  running="$(runner_pids | paste -sd, -)"
  echo "运行目录: $RUN_DIR"
  echo "进度: $done_count/$expected"
  if [[ -n "$running" ]]; then
    echo "执行器: 运行中 (PID: $running)"
  elif is_complete; then
    echo "执行器: 已完成"
  else
    echo "执行器: 未运行或已中断，可执行: $0 start"
  fi
  if [[ -f "$SUMMARY" ]]; then
    jq -r '
      "确定性构建成功: \(.build_capability.standard_build_success)/\(.record_count)",
      "进入 Agent: \(.agent_capability.entered_agent)",
      (if .agent_capability.build_repair_success != null then
        "Agent 构建修复成功: \(.agent_capability.build_repair_success)/\(.agent_capability.build_repair_candidates)"
       else "Agent 构建修复成功: 旧版报告未单独统计" end),
      "Agent 最终环境修复成功: \(.agent_capability.environment_repair_success // .agent_capability.agent_repair_success)",
      "LLM 调用: \(.cost.llm_calls)",
      "最终环境成功: \(.agent_capability.final_environment_success)/\(.record_count)",
      (if (.batch_stop_reason // "") != "" then
        "批次停止原因: \(.batch_stop_reason)"
       else empty end)
    ' "$SUMMARY"
  fi
}

run_canary() {
  cd "$REPO_DIR"
  PYTHONPATH=src python3 evaluations/multilang/run_agent_canary.py \
    --output "$RUN_DIR" \
    --indices "$INDICES"
}

worker() {
  mkdir -p "$RUN_DIR"
  exec 9>"$LOCK_FILE"
  flock -n 9 || exit 0
  echo "$$" >"$PID_FILE"
  trap 'rm -f "$PID_FILE"' EXIT
  run_canary
}

case "${1:-status}" in
  start|resume)
    mkdir -p "$RUN_DIR"
    if is_complete; then
      echo "LLM canary 已完成，不重复执行。"
      show_status
    elif [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "后台执行器已经运行。"
      show_status
    else
      nohup env \
        DPRAUTO_RUN_ID="$RUN_ID" \
        DPRAUTO_INDICES="$INDICES" \
        bash "$SCRIPT_DIR/high_star_30_agent_runner.sh" _worker \
        >>"$LOG_FILE" 2>&1 </dev/null &
      echo "后台 LLM canary 已启动，PID=$!"
      echo "查看进度: $0 status"
      echo "跟踪日志: $0 log"
    fi
    ;;
  status) show_status ;;
  log)
    mkdir -p "$RUN_DIR"
    touch "$LOG_FILE"
    tail -n 80 -f "$LOG_FILE"
    ;;
  foreground) run_canary ;;
  _worker) worker ;;
  *) echo "用法: $0 {start|resume|status|log|foreground}" >&2; exit 2 ;;
esac

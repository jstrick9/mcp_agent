#!/usr/bin/env bash
# Drives each Ollama bridge agent end-to-end against a mock Ollama endpoint.
# Verifies the agent really lists MCP tools, really calls one, and really
# feeds the tool result back to the model.
set -uo pipefail
cd "$(dirname "$0")/.."
REPO="$(pwd)"
PY="$REPO/.venv/bin/python"

failures=0
declare -a PIDS=()

start_mock() { # port tool args_json final log
  MOCK_PORT="$1" MOCK_TOOL="$2" MOCK_ARGS="$3" MOCK_FINAL="$4" MOCK_LOG="$5" \
    nohup "$PY" tests/mock_ollama.py >/dev/null 2>&1 &
  PIDS+=("$!")
  for _ in $(seq 1 50); do
    if curl -s -o /dev/null -X POST "http://127.0.0.1:$1/v1/chat/completions" \
         -H 'Content-Type: application/json' -d '{"messages":[]}'; then
      return 0
    fi
    sleep 0.2
  done
  echo "mock on $1 failed to start"
  return 1
}

stop_mocks() { for p in "${PIDS[@]}"; do kill "$p" 2>/dev/null; done; }
trap stop_mocks EXIT

run_case() { # label script extra_flag dir port tool args final expect_fragment
  local label="$1" script="$2" flag="$3" dir="$4" port="$5" tool="$6" args="$7" final="$8" frag="$9"
  local log="tests/.mock-$port.jsonl"
  rm -f "$log"
  start_mock "$port" "$tool" "$args" "$final" "$log" || { failures=$((failures+1)); return; }

  echo "=== $label ($script) ==="
  out="$(OLLAMA_URL="http://127.0.0.1:$port/v1/chat/completions" \
    "$PY" "$script" "test prompt" --model mock-model "$flag" "$dir" 2>"tests/.stderr-$port.txt")"
  rc=$?
  echo "  exit code: $rc"
  echo "  stdout: $(echo "$out" | tail -1 | cut -c1-160)"
  echo "  stderr: $(grep -E 'connected to MCP server|calling ' "tests/.stderr-$port.txt" | sed 's/^/    /' | cut -c1-160)"

  [ "$rc" -eq 0 ] || { echo "  FAIL: non-zero exit"; failures=$((failures+1)); }
  echo "$out" | grep -q "$final" || { echo "  FAIL: final answer not printed"; failures=$((failures+1)); }
  "$PY" tests/check_tool_result.py "$log" "$frag" || failures=$((failures+1))

  # confirm data really landed on disk
  echo "  files created: $(find "$dir" -type f 2>/dev/null | wc -l | tr -d ' ') in $dir"
  echo
}

WORK="$(mktemp -d)"
run_case "web research"  agent.py         --notes-dir "$WORK/notes"   11435 save_note   '{"filename":"e2e.md","content":"# E2E\n\nBridge test."}' "RESEARCH-DONE" '"saved": true'
run_case "planner"       planner_agent.py --data-dir  "$WORK/planner" 11436 create_task '{"project":"E2E","title":"Ship it","priority":"high"}' "PLANNER-DONE" '"id"'
run_case "health"        health_agent.py  --data-dir  "$WORK/health"  11437 create_habit '{"name":"Water","target_per_week":7,"unit":"glasses"}' "HEALTH-DONE" 'Water'
run_case "knowledge base" kb_agent.py     --data-dir  "$WORK/kb"      11438 save_snippet '{"content":"MCP servers speak JSON-RPC over stdio.","title":"MCP transport","tags":"mcp,stdio"}' "KB-DONE" '"saved": true'

rm -rf "$WORK"
echo "-----------------------------------------"
if [ "$failures" -eq 0 ]; then echo "ALL BRIDGE AGENT CHECKS PASSED"; else echo "$failures FAILURE(S)"; fi
exit "$([ "$failures" -eq 0 ] && echo 0 || echo 1)"

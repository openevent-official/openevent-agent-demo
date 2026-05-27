#!/usr/bin/env bash
set -euo pipefail

# Shared paths, runtime directories, and PYTHONPATH for all openevent-stack scripts.

STACK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="$STACK_DIR/config"

# shellcheck source=env.sh
source "$STACK_DIR/env.sh"
if [ -f "$CONFIG_DIR/env.sh" ]; then
  # shellcheck source=config/env.sh
  source "$CONFIG_DIR/env.sh"
fi

DATA_DIR="$STACK_DIR/data"
LOG_DIR="$STACK_DIR/logs"
RUN_DIR="$STACK_DIR/run"
SPEC_PATH="$STACK_DIR/stack.yaml"

mkdir -p "$CONFIG_DIR" "$DATA_DIR" "$LOG_DIR" "$RUN_DIR"

join_by_colon() {
  local result=""
  local item
  for item in "$@"; do
    [ -z "$item" ] && continue
    if [ -z "$result" ]; then
      result="$item"
    else
      result="$result:$item"
    fi
  done
  printf '%s' "$result"
}

stack_pythonpath() {
  local paths=("$DEMO_ROOT")
  [ -n "$EXTRA_PYTHONPATH" ] && paths+=("$EXTRA_PYTHONPATH")
  [ -n "${PYTHONPATH:-}" ] && paths+=("$PYTHONPATH")
  join_by_colon "${paths[@]}"
}

export PYTHONPATH
PYTHONPATH="$(stack_pythonpath)"

start_view() {
  "$STACK_DIR/render-view-config.sh" >/dev/null
  "$STACK_DIR/process.sh" start openevent-view
}

print_start_result() {
  printf '\nprocess start result:\n'
  "$STACK_DIR/process.sh" status all
}

#!/usr/bin/env bash
set -euo pipefail

# Shared paths, runtime directories, and PYTHONPATH for all openevent-stack scripts.

# Match Runtime's resolved paths so directory aliases identify the same processes.
STACK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
mkdir -p "$STACK_DIR/runtime"
STACK_RUNTIME_DIR="$(cd "$STACK_DIR/runtime" && pwd -P)"
CONFIG_DIR="$STACK_RUNTIME_DIR/config"

if [ -f "$CONFIG_DIR/env.sh" ]; then
  # shellcheck source=runtime/config/env.sh
  source "$CONFIG_DIR/env.sh"
fi
# Generated deployment paths fill unset values before local defaults do.
# shellcheck source=env.sh
source "$STACK_DIR/env.sh"

DATA_DIR="$STACK_RUNTIME_DIR/data"
LOG_DIR="$STACK_RUNTIME_DIR/logs"
RUN_DIR="$STACK_RUNTIME_DIR/run"
WORK_DIR="$STACK_RUNTIME_DIR/workdir"
SPEC_PATH="$STACK_DIR/stack.yaml"

mkdir -p "$CONFIG_DIR" "$DATA_DIR" "$LOG_DIR" "$RUN_DIR" "$WORK_DIR"

export PYTHONDONTWRITEBYTECODE=1

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
  local paths=()
  [ -n "$EXTRA_PYTHONPATH" ] && paths+=("$EXTRA_PYTHONPATH")
  [ -n "${PYTHONPATH:-}" ] && paths+=("$PYTHONPATH")
  join_by_colon "${paths[@]}"
}

export PYTHONPATH
PYTHONPATH="$(stack_pythonpath)"

print_start_result() {
  printf '\nServices:\n'
  "$STACK_DIR/process.sh" status all || return $?
  "$PYTHON_BIN" -B - "$CONFIG_DIR" <<'PY'
import json
import sys
from pathlib import Path
import yaml

root = Path(sys.argv[1])
chat = json.loads((root / 'chat-server.json').read_text(encoding='utf-8'))
view = yaml.safe_load((root / 'openevent-view.yaml').read_text(encoding='utf-8'))['server']

def local_origin(server):
    host = {'0.0.0.0': '127.0.0.1', '::': '::1'}.get(server['host'], server['host'])
    if ':' in host and not host.startswith('['):
        host = f'[{host}]'
    return f"http://{host}:{server['port']}"

chat_origin = chat.get('origin') or local_origin(chat)
print(f"\nChat: {chat_origin.rstrip('/')}/api/chat/")
print(f"View: {local_origin(view)}/")
PY
}

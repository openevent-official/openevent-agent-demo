#!/usr/bin/env bash
set -euo pipefail

# Minimal local process manager used by bootstrap/start/stop/status.

# shellcheck source=common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/common.sh"

programs=(openevent model-proxy chat-server openevent-view chat-model-agent cmd-worker)

pid_file() {
  printf '%s/%s.pid' "$RUN_DIR" "$1"
}

log_file() {
  printf '%s/%s.log' "$LOG_DIR" "$1"
}

is_running() {
  managed_pid "$1" >/dev/null
}

pid_alive() {
  local pid="$1" state
  if [ -r "/proc/$pid/status" ]; then
    state="$(awk '/^State:/ {print $2}' "/proc/$pid/status" 2>/dev/null || true)"
    [ "$state" = "Z" ] && return 1
    [ -n "$state" ] && return 0
  fi
  kill -0 "$pid" 2>/dev/null
}

command_for() {
  case "$1" in
    openevent)
      printf '%s\0' "$OPENEVENT_SERVER_BIN" "$CONFIG_DIR/openevent-server.yaml"
      ;;
    chat-server)
      printf '%s\0' "$PYTHON_BIN" "-B" "-m" "openevent.chat_app" "--config" "$CONFIG_DIR/chat-server.json"
      ;;
    model-proxy)
      printf '%s\0' "$PYTHON_BIN" "-m" "openevent.model_proxy.cli" "--config" "$CONFIG_DIR/model-proxy.yaml"
      ;;
    cmd-worker)
      printf '%s\0' "$PYTHON_BIN" "-m" "openevent.cmd_worker.cli" "--config" "$CONFIG_DIR/cmd-worker.yaml"
      ;;
    chat-model-agent)
      printf '%s\0' "$PYTHON_BIN" "-m" "chat_model_agent.cli" "--config" "$CONFIG_DIR/chat-model-agent.json"
      ;;
    openevent-view)
      printf '%s\0' "$PYTHON_BIN" "-m" "openevent.view" "--config" "$CONFIG_DIR/openevent-view.yaml"
      ;;
    *)
      printf 'unknown program: %s\n' "$1" >&2
      return 2
      ;;
  esac
}

config_path_for() {
  case "$1" in
    openevent) printf '%s\n' "$CONFIG_DIR/openevent-server.yaml" ;;
    chat-server) printf '%s\n' "$CONFIG_DIR/chat-server.json" ;;
    model-proxy) printf '%s\n' "$CONFIG_DIR/model-proxy.yaml" ;;
    cmd-worker) printf '%s\n' "$CONFIG_DIR/cmd-worker.yaml" ;;
    chat-model-agent) printf '%s\n' "$CONFIG_DIR/chat-model-agent.json" ;;
    openevent-view) printf '%s\n' "$CONFIG_DIR/openevent-view.yaml" ;;
    *) return 2 ;;
  esac
}

pid_matches_program() {
  local program="$1" pid="$2" config_path="${3:-}" module index found_module=0 found_config=0
  local -a argv=()
  [ -r "/proc/$pid/cmdline" ] || return 1
  if [ -z "$config_path" ]; then
    config_path="$(config_path_for "$program")" || return $?
  fi
  mapfile -d '' -t argv < "/proc/$pid/cmdline" 2>/dev/null || return 1
  case "$program" in
    openevent) [[ "${argv[0]:-}" == "$OPENEVENT_SERVER_BIN" && "${argv[1]:-}" == "$config_path" ]]; return ;;
    chat-server) module=openevent.chat_app ;;
    model-proxy) module=openevent.model_proxy.cli ;;
    cmd-worker) module=openevent.cmd_worker.cli ;;
    chat-model-agent) module=chat_model_agent.cli ;;
    openevent-view) module=openevent.view ;;
    *) return 2 ;;
  esac
  for ((index = 0; index + 1 < ${#argv[@]}; index++)); do
    if [[ "${argv[index]}" == -m && "${argv[index+1]}" == "$module" ]]; then found_module=1; fi
    if [[ "${argv[index]}" == --config && "${argv[index+1]}" == "$config_path" ]]; then found_config=1; fi
  done
  [ "$found_module" = 1 ] && [ "$found_config" = 1 ]
}

matching_pids() {
  local program="$1" proc pid config_path
  # Resolve once per scan instead of forking a subshell for every system PID.
  config_path="$(config_path_for "$program")" || return $?
  for proc in /proc/[0-9]*; do
    pid="${proc##*/}"
    [ "$pid" != "$$" ] || continue
    pid_matches_program "$program" "$pid" "$config_path" && printf '%s\n' "$pid"
  done
}

managed_pid() {
  local pid_path pid
  pid_path="$(pid_file "$1")"
  [ -f "$pid_path" ] || return 1
  pid="$(cat "$pid_path")"
  [[ "$pid" =~ ^[1-9][0-9]*$ ]] && pid_alive "$pid" || return 1
  pid_matches_program "$1" "$pid" || return 1
  printf '%s\n' "$pid"
}

extra_pids() {
  local program="$1" keep="${2:-}" pid
  while IFS= read -r pid; do
    [ -n "$pid" ] || continue
    [ "$pid" = "$keep" ] && continue
    pid_alive "$pid" || continue
    printf '%s\n' "$pid"
  done < <(matching_pids "$program")
}

wait_pid_exit() {
  local pid="$1" deadline=$((SECONDS + $2))
  while pid_alive "$pid"; do
    [ "$SECONDS" -lt "$deadline" ] || return 1
    sleep 0.2
  done
}

stop_pid() {
  local pid="$1" program="${2:-}"
  pid_alive "$pid" || return 0
  if ! kill "$pid" 2>/dev/null; then
    pid_alive "$pid" || return 0
    printf '%s failed to send SIGTERM to pid %s; process is still running\n' "$program" "$pid" >&2
    return 1
  fi
  if [ "$program" = "chat-model-agent" ]; then
    while pid_alive "$pid"; do sleep 0.2; done
    return 0
  fi
  wait_pid_exit "$pid" 10 && return 0
  if ! kill -9 "$pid" 2>/dev/null; then
    pid_alive "$pid" || return 0
    printf '%s failed to send SIGKILL to pid %s; process is still running\n' "$program" "$pid" >&2
    return 1
  fi
  if ! wait_pid_exit "$pid" 2; then
    printf '%s pid %s is still running 2 seconds after SIGKILL\n' "$program" "$pid" >&2
    return 1
  fi
}

stop_unmanaged() {
  local program="$1" keep="${2:-}" pid
  while IFS= read -r pid; do
    [ -n "$pid" ] || continue
    stop_pid "$pid" "$program" || return $?
    printf '%s cleaned stale pid %s\n' "$program" "$pid" >&2
  done < <(extra_pids "$program" "$keep")
}

status_one() {
  local program="$1" pid extras
  pid="$(managed_pid "$program" || true)"
  extras="$(extra_pids "$program" "$pid" | paste -sd, -)"
  if is_running "$program"; then
    if [ -n "$extras" ]; then
      printf '%s RUNNING pid %s DUPLICATE pids %s\n' "$program" "$pid" "$extras"
      return 4
    fi
    printf '%s RUNNING pid %s\n' "$program" "$pid"
    return 0
  fi
  if [ -n "$extras" ]; then
    printf '%s ORPHANED pids %s\n' "$program" "$extras"
    return 4
  fi
  printf '%s STOPPED\n' "$program"
  return 3
}

cmd_environment_value() {
  "$PYTHON_BIN" -B - "$CONFIG_DIR/state.yaml" "$1" <<'PYENV'
import sys, yaml
from pathlib import Path
try:
    value = yaml.safe_load(Path(sys.argv[1]).read_text(encoding='utf-8'))['cmd_environment'][sys.argv[2]]
    if not isinstance(value, str) or not value:
        raise ValueError()
except (OSError, ValueError, TypeError, KeyError, yaml.YAMLError):
    sys.exit('could not load Cmd object reader environment; run bootstrap.sh --apply')
print(value)
PYENV
}

start_one() {
  local program="$1" keep pid object_target
  if is_running "$program"; then
    keep="$(managed_pid "$program")"
    stop_unmanaged "$program" "$keep" || return $?
    status_one "$program" || return $?
    return 0
  fi
  stop_unmanaged "$program" || return $?
  rm -f "$(pid_file "$program")"

  if [ "$program" = "openevent-view" ] && ! is_running openevent; then
    printf 'openevent-view depends on openevent; start the stack with bootstrap.sh --apply or start.sh first\n' >&2
    return 1
  fi

  local -a cmd=()
  while IFS= read -r -d '' part; do
    cmd+=("$part")
  done < <(command_for "$program")

  if [ "$program" = "openevent" ] && [ ! -x "$OPENEVENT_SERVER_BIN" ]; then
    printf 'OpenEvent server binary not executable: %s\n' "$OPENEVENT_SERVER_BIN" >&2
    return 1
  fi
  if [ "$program" = "cmd-worker" ]; then
    object_target="$(cmd_environment_value object_target)" || return 1
  fi

  (
    if [ "$program" = "cmd-worker" ]; then
      export OPENEVENT_OBJECT_TARGET="$object_target"
      export PATH="$(dirname "$PYTHON_BIN"):$PATH"
    fi
    cd "$WORK_DIR"
    exec setsid nohup "${cmd[@]}"
  ) </dev/null >>"$(log_file "$program")" 2>&1 &
  pid="$!"
  printf '%s\n' "$pid" >"$(pid_file "$program")"
  sleep 0.2

  if ! is_running "$program"; then
    printf '%s failed to start; see %s\n' "$program" "$(log_file "$program")" >&2
    tail -n 40 "$(log_file "$program")" >&2 || true
    return 1
  fi
  status_one "$program"
}

stop_one() {
  local program="$1"
  local pid_path pid
  pid_path="$(pid_file "$program")"
  if is_running "$program"; then
    pid="$(cat "$pid_path")"
    stop_pid "$pid" "$program" || return $?
  fi
  stop_unmanaged "$program" || return $?
  rm -f "$pid_path"
  printf '%s STOPPED\n' "$program"
}

restart_one() {
  stop_one "$1" >/dev/null || return $?
  start_one "$1"
}

action="${1:-status}"
target="${2:-all}"

if [ "$target" = "all" ] && [ "$action" != "status" ]; then
  printf 'use start.sh or stop.sh for dependency-ordered stack operations\n' >&2
  exit 2
fi
if [ "$target" = "all" ]; then
  targets=("${programs[@]}")
else
  targets=("$target")
fi

rc=0
for program in "${targets[@]}"; do
  case "$action" in
    status) status_one "$program" || rc=$? ;;
    start) start_one "$program" || rc=$? ;;
    stop) stop_one "$program" || rc=$? ;;
    restart) restart_one "$program" || rc=$? ;;
    *) printf 'unknown action: %s\n' "$action" >&2; exit 2 ;;
  esac
done
exit "$rc"

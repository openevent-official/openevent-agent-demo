#!/usr/bin/env bash
set -euo pipefail

# Minimal local process manager used by bootstrap/start/stop/status.

# shellcheck source=common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

programs=(openevent im-p2p-syncer model-proxy im-model-agent openevent-view)

pid_file() {
  printf '%s/%s.pid' "$RUN_DIR" "$1"
}

log_file() {
  printf '%s/%s.log' "$LOG_DIR" "$1"
}

is_running() {
  local pid_path pid
  pid_path="$(pid_file "$1")"
  [ -f "$pid_path" ] || return 1
  pid="$(cat "$pid_path")"
  [ -n "$pid" ] && pid_alive "$pid"
}

pid_alive() {
  local pid="$1" state
  kill -0 "$pid" 2>/dev/null || return 1
  if [ -r "/proc/$pid/status" ]; then
    state="$(awk '/^State:/ {print $2}' "/proc/$pid/status" 2>/dev/null || true)"
    [ "$state" = "Z" ] && return 1
  fi
  return 0
}

command_for() {
  case "$1" in
    openevent)
      printf '%s\0' "$OPENEVENT_SERVER_BIN" "$CONFIG_DIR/openevent-server.yaml"
      ;;
    im-p2p-syncer)
      printf '%s\0' "$PYTHON_BIN" "-m" "openevent.im_p2p_syncer.cli" "--config" "$CONFIG_DIR/im-p2p-syncer.yaml"
      ;;
    model-proxy)
      printf '%s\0' "$PYTHON_BIN" "-m" "openevent.model_proxy.cli" "--config" "$CONFIG_DIR/model-proxy.yaml"
      ;;
    im-model-agent)
      printf '%s\0' "$PYTHON_BIN" "-m" "im_model_agent.cli" "--config" "$CONFIG_DIR/im-model-agent.yaml"
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
    im-p2p-syncer) printf '%s\n' "$CONFIG_DIR/im-p2p-syncer.yaml" ;;
    model-proxy) printf '%s\n' "$CONFIG_DIR/model-proxy.yaml" ;;
    im-model-agent) printf '%s\n' "$CONFIG_DIR/im-model-agent.yaml" ;;
    openevent-view) printf '%s\n' "$CONFIG_DIR/openevent-view.yaml" ;;
    *) return 2 ;;
  esac
}

matching_pids() {
  local program="$1" config_path proc pid cmdline
  config_path="$(config_path_for "$program")"
  for proc in /proc/[0-9]*; do
    pid="${proc##*/}"
    [ "$pid" != "$$" ] || continue
    [ -r "$proc/cmdline" ] || continue
    cmdline="$(tr '\0' ' ' < "$proc/cmdline" 2>/dev/null || true)"
    [ -n "$cmdline" ] || continue
    case "$program" in
      openevent)
        case "$cmdline" in
          *"$OPENEVENT_SERVER_BIN $config_path"*) printf '%s\n' "$pid" ;;
        esac
        ;;
      im-p2p-syncer)
        case "$cmdline" in
          *"openevent.im_p2p_syncer.cli"*"--config $config_path"*) printf '%s\n' "$pid" ;;
        esac
        ;;
      model-proxy)
        case "$cmdline" in
          *"openevent.model_proxy.cli"*"--config $config_path"*) printf '%s\n' "$pid" ;;
        esac
        ;;
      im-model-agent)
        case "$cmdline" in
          *"im_model_agent.cli"*"--config $config_path"*) printf '%s\n' "$pid" ;;
        esac
        ;;
      openevent-view)
        case "$cmdline" in
          *"openevent.view"*"--config $config_path"*) printf '%s\n' "$pid" ;;
        esac
        ;;
    esac
  done
}

managed_pid() {
  local pid_path pid
  pid_path="$(pid_file "$1")"
  [ -f "$pid_path" ] || return 1
  pid="$(cat "$pid_path")"
  [ -n "$pid" ] && pid_alive "$pid" || return 1
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

stop_pid() {
  local pid="$1" deadline
  pid_alive "$pid" || return 0
  kill "$pid" 2>/dev/null || true
  deadline=$((SECONDS + 10))
  while pid_alive "$pid"; do
    if [ "$SECONDS" -ge "$deadline" ]; then
      kill -9 "$pid" 2>/dev/null || true
      break
    fi
    sleep 0.2
  done
}

stop_unmanaged() {
  local program="$1" keep="${2:-}" pid
  while IFS= read -r pid; do
    [ -n "$pid" ] || continue
    printf '%s cleaned stale pid %s\n' "$program" "$pid" >&2
    stop_pid "$pid"
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

start_one() {
  local program="$1" keep
  if is_running "$program"; then
    keep="$(managed_pid "$program")"
    stop_unmanaged "$program" "$keep"
    status_one "$program"
    return 0
  fi
  stop_unmanaged "$program"
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

  (
    cd "$DEMO_ROOT"
    exec setsid nohup "${cmd[@]}"
  ) </dev/null >>"$(log_file "$program")" 2>&1 &
  printf '%s\n' "$!" >"$(pid_file "$program")"
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
    stop_pid "$pid"
  fi
  stop_unmanaged "$program"
  rm -f "$pid_path"
  printf '%s STOPPED\n' "$program"
}

restart_one() {
  stop_one "$1" >/dev/null
  start_one "$1"
}

action="${1:-status}"
target="${2:-all}"

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

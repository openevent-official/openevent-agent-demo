#!/usr/bin/env bash
set -euo pipefail

# Validate or apply stack.yaml, generate runtime configs, and reconcile OpenEvent tokens/channels.

# shellcheck source=common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

mode="${1:---dry-run}"
if [ "$mode" != "--dry-run" ] && [ "$mode" != "--apply" ]; then
  printf 'usage: %s [--dry-run|--apply]\n' "$0" >&2
  exit 2
fi

if [ "$mode" = "--apply" ] && [ ! -x "$OPENEVENT_SERVER_BIN" ]; then
  printf 'OpenEvent server binary not executable: %s\n' "$OPENEVENT_SERVER_BIN" >&2
  exit 1
fi

cd "$DEMO_ROOT"
"$PYTHON_BIN" scripts/reconcile_runtime.py \
  --spec "$SPEC_PATH" \
  --runtime-root "$STACK_DIR" \
  "$mode"

if [ "$mode" = "--apply" ]; then
  start_view
  print_start_result
fi

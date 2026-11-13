#!/usr/bin/env bash
set -euo pipefail

# Validate or apply stack.yaml, generate runtime configs, and reconcile OpenEvent tokens/channels.

# shellcheck source=common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/common.sh"

mode="${1:---dry-run}"
diagnostics=()
if { [ "$mode" != "--dry-run" ] && [ "$mode" != "--apply" ]; } ||
   [ "$#" -gt 2 ] ||
   { [ "$#" -eq 2 ] && { [ "$mode" != "--apply" ] || [ "$2" != "--write-diagnostics" ]; }; }; then
  printf 'usage: %s [--dry-run|--apply [--write-diagnostics]]\n' "$0" >&2
  exit 2
fi
if [ "$#" -eq 2 ]; then
  diagnostics=(--write-diagnostics)
fi

if [ "$mode" = "--apply" ] && [ ! -x "$OPENEVENT_SERVER_BIN" ]; then
  printf 'OpenEvent server binary not executable: %s\n' "$OPENEVENT_SERVER_BIN" >&2
  exit 1
fi

cd "$DEMO_ROOT"
"$PYTHON_BIN" -m runtime_reconciler.cli \
  --spec "$SPEC_PATH" \
  --runtime-root "$STACK_RUNTIME_DIR" \
  "$mode" "${diagnostics[@]}"

if [ "$mode" = "--apply" ]; then
  print_start_result
fi

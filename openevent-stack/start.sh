#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/common.sh"
cd "$DEMO_ROOT"
"$PYTHON_BIN" -B -m runtime_reconciler.cli --spec "$SPEC_PATH" --runtime-root "$STACK_RUNTIME_DIR" --start
print_start_result

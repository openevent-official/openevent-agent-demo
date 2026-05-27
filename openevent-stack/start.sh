#!/usr/bin/env bash
set -euo pipefail

# Start all already-configured local stack processes in dependency order.

# shellcheck source=common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

"$STACK_DIR/process.sh" start openevent
"$STACK_DIR/process.sh" start model-proxy
"$STACK_DIR/process.sh" start im-p2p-syncer
"$STACK_DIR/process.sh" start im-model-agent
start_view
print_start_result

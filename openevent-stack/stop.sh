#!/usr/bin/env bash
set -euo pipefail

# Stop all local stack processes in reverse dependency order.

STACK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

"$STACK_DIR/process.sh" stop openevent-view || true
"$STACK_DIR/process.sh" stop im-model-agent || true
"$STACK_DIR/process.sh" stop im-p2p-syncer || true
"$STACK_DIR/process.sh" stop model-proxy || true
"$STACK_DIR/process.sh" stop openevent || true

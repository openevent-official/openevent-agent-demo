#!/usr/bin/env bash
set -euo pipefail
STACK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
"$STACK_DIR/process.sh" stop chat-model-agent
for program in cmd-worker model-proxy chat-server openevent-view openevent; do
  "$STACK_DIR/process.sh" stop "$program"
done

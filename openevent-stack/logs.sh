#!/usr/bin/env bash
set -euo pipefail

# List logs, or follow one log with: logs.sh <program>.

# shellcheck source=common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

program="${1:-}"
lines="${TAIL_LINES:-120}"

if [ -n "$program" ]; then
  tail -n "$lines" -f "$LOG_DIR/$program.log"
else
  find "$LOG_DIR" -maxdepth 1 -type f -name '*.log' -print | sort
fi

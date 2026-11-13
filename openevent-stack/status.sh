#!/usr/bin/env bash
set -euo pipefail

# Print local stack process status.

"$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/process.sh" status all || true

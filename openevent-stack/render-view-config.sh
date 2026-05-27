#!/usr/bin/env bash
set -euo pipefail

# Generate openevent-view.yaml from stack.yaml. The view is started only after OpenEvent is running.

# shellcheck source=common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

"$PYTHON_BIN" - "$SPEC_PATH" "$CONFIG_DIR/openevent-view.yaml" <<'PY'
from __future__ import annotations

import sys
from pathlib import Path

import yaml

spec = yaml.safe_load(Path(sys.argv[1]).read_text(encoding="utf-8"))
view = spec.get("view") or {}
config = {
    "version": "v1",
    "server": {
        "host": "0.0.0.0",
        "port": int(view.get("port", 8080)),
    },
    "openevent": {"target": spec["openevent"]["grpc_addr"]},
    "history": {
        "default_limit": 100,
        "max_limit": 1000,
        "fetch_batch_size": 1000,
        "max_scan_messages": 10000,
        "default_order": "desc",
    },
    "payload": {"parse_json": True, "include_text": True, "text_max_bytes": 65536},
}
path = Path(sys.argv[2])
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=True), encoding="utf-8")
path.chmod(0o600)
PY

printf 'wrote %s\n' "$CONFIG_DIR/openevent-view.yaml"

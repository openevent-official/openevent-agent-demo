#!/usr/bin/env bash
set -euo pipefail

PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH=".${PYTHONPATH:+:$PYTHONPATH}" \
  "${PYTHON:-python3}" - <<'PY'
import importlib.util
import sys

missing = [
    module
    for module in ("openevent.sdk", "openevent.im_sdk", "openevent.model_proxy_sdk", "openevent.cmd_sdk")
    if importlib.util.find_spec(module) is None
]
if missing:
    print("missing Python dependencies in the current environment: " + ", ".join(missing), file=sys.stderr)
    sys.exit(2)

from im_model_agent.dependencies import RuntimeDependencyError, validate_runtime_dependencies

try:
    validate_runtime_dependencies()
except RuntimeDependencyError as exc:
    print(str(exc), file=sys.stderr)
    sys.exit(2)
PY

PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH=".${PYTHONPATH:+:$PYTHONPATH}" \
  "${PYTHON:-python3}" -m unittest discover -s tests "$@"

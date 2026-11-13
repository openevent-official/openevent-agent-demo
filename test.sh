#!/usr/bin/env bash
set -euo pipefail

PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH=".${PYTHONPATH:+:$PYTHONPATH}" \
  "${PYTHON:-python3}" - <<'PY'
import sys
from chat_model_agent.dependencies import DependencyError, require_dependencies

try:
    require_dependencies()
except DependencyError as exc:
    print(str(exc), file=sys.stderr)
    sys.exit(2)
PY

PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH=".${PYTHONPATH:+:$PYTHONPATH}" \
  "${PYTHON:-python3}" -m unittest discover -s tests "$@"

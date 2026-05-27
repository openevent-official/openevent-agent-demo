#!/usr/bin/env bash
set -euo pipefail

PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH=".${PYTHONPATH:+:$PYTHONPATH}" \
  "${PYTHON:-python3}" -m unittest discover -s tests "$@"

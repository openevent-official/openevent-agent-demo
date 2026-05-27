#!/usr/bin/env bash

# Local machine settings for openevent-stack scripts. Adjust before bootstrap.sh --apply.

DEMO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_ROOT="${RUNTIME_ROOT:-$DEMO_ROOT/runtime}"

PYTHON_BIN="${PYTHON_BIN:-$RUNTIME_ROOT/venv/bin/python}"
OPENEVENT_SERVER_BIN="${OPENEVENT_SERVER_BIN:-$RUNTIME_ROOT/src/openevent/build/openevent_server}"

# Use installed packages from PYTHON_BIN by default. EXTRA_PYTHONPATH is only for
# local debugging and should not be needed for normal runtime.
EXTRA_PYTHONPATH="${EXTRA_PYTHONPATH:-}"

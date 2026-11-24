#!/usr/bin/env bash
set -Eeuo pipefail

# Build a local OpenEvent Agent Demo runtime from an empty Ubuntu environment.
# The script installs system packages, clones all OpenEvent projects from GitHub,
# creates a Python venv, installs Python packages into that venv, and builds the
# OpenEvent server binary.

usage() {
  cat <<'USAGE'
Usage:
  scripts/bootstrap_ubuntu_venv.sh [--workdir DIR] [--skip-apt]

Options:
  --workdir DIR  Runtime workspace to create or update. Default: runtime
  --skip-apt     Do not run apt-get. Use this when system dependencies are already installed.
  -h, --help     Show this help.

Environment overrides:
  OPENEVENT_URL                  default: https://github.com/openevent-official/openevent.git
  OPENEVENT_REF                  default: HEAD (remote default branch); accepts a branch, tag or commit
  OPENEVENT_SDK_URL              default: https://github.com/openevent-official/openevent-sdk.git
  OPENEVENT_SDK_REF              default: HEAD (remote default branch); accepts a branch, tag or commit
  OPENEVENT_MODULES_CHAT_URL       default: https://github.com/openevent-official/openevent-modules-chat.git
  OPENEVENT_MODULES_CHAT_REF       default: HEAD (remote default branch); accepts a branch, tag or commit
  OPENEVENT_MODEL_PROXY_URL      default: https://github.com/openevent-official/openevent-modules-model-proxy.git
  OPENEVENT_MODEL_PROXY_REF      default: HEAD (remote default branch); accepts a branch, tag or commit
  OPENEVENT_MODULES_CMD_URL      default: https://github.com/openevent-official/openevent-modules-cmd.git
  OPENEVENT_MODULES_CMD_REF      default: HEAD (remote default branch); accepts a branch, tag or commit
  OPENEVENT_VIEW_URL             default: https://github.com/openevent-official/openevent-view.git
  OPENEVENT_VIEW_REF             default: HEAD (remote default branch); accepts a branch, tag or commit
  PYTHON_BIN                     Python 3.10–3.12 for source builds (default: python3)
  JOBS                           default: nproc result, or 2

Dependencies are fetched again on every run. This project is built from the
current checkout, including local edits; the script does not pull that checkout.
The script does not start the stack or write real Chat/model credentials.
USAGE
}

fail() {
  printf '\nERROR: %s\n' "$*" >&2
  exit 1
}

on_error() {
  local rc="$1" line="$2" command="$3"
  printf '\nERROR: bootstrap failed at line %s while running:\n  %s\n' "$line" "$command" >&2
  printf 'Exit code: %s\n' "$rc" >&2
  printf 'Fix the error above and rerun this script. Existing clean checkouts are reused.\n' >&2
  exit "$rc"
}
trap 'on_error "$?" "$LINENO" "$BASH_COMMAND"' ERR

log() {
  printf '\n==> %s\n' "$*"
}

run() {
  log "$*"
  "$@"
}

bootstrap_python() {
  # Scope these paths to this script's Python calls. Component make targets
  # continue to select their own build directories.
  local build_dir="$DEMO_SOURCE/build"
  mkdir -p "$build_dir/pip-cache" "$build_dir/tmp" || return
  PIP_CACHE_DIR="$build_dir/pip-cache" TMPDIR="$build_dir/tmp" "$@"
}

abs_path() {
  case "$1" in
    /*) printf '%s\n' "$1" ;;
    *) printf '%s/%s\n' "$PWD" "$1" ;;
  esac
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "required command not found: $1"
}

check_python_version() {
  local python="$1"
  bootstrap_python "$python" - <<'PY'
import sys

if not (3, 10) <= sys.version_info[:2] <= (3, 12):
    raise SystemExit(
        "Building the Demo and OpenEvent SDK from source requires Python 3.10–3.12. "
        "Select a supported interpreter with PYTHON_BIN."
    )
PY
}

check_cmake_version() {
  local version
  version="$(cmake --version | awk 'NR == 1 {print $3}')"
  bootstrap_python "$PYTHON_BIN" - "$version" <<'PY'
import sys

version = sys.argv[1]
parts = version.split(".")
try:
    major = int(parts[0])
    minor = int(parts[1])
except (IndexError, ValueError):
    raise SystemExit(f"could not parse CMake version: {version}")

if (major, minor) < (3, 20):
    raise SystemExit(
        f"CMake 3.20+ is required, found {version}. "
        "Install a newer CMake package and rerun."
    )
PY
}

require_clean_or_empty_checkout() {
  local dir="$1"
  [ -e "$dir/.git" ] || return 0
  # Advancing the SDK changes its parent's gitlink. Check actual files in each
  # repository, so that this expected pointer change does not block a rerun.
  if [ -n "$(git -C "$dir" status --porcelain --ignore-submodules=all)" ]; then
    fail "existing checkout has local changes: $dir
Preserve your edits or choose a different --workdir before rerunning."
  fi
  git -C "$dir" submodule foreach --quiet --recursive '
    if [ -n "$(git status --porcelain --ignore-submodules=all)" ]; then
      printf "local changes in submodule: %s\n" "$PWD" >&2
      exit 1
    fi
  ' || fail "existing checkout has local submodule changes: $dir"
}

clone_or_update() {
  local name="$1" url="$2" ref="$3" dir="$4"
  if [ -e "$dir/.git" ]; then
    require_clean_or_empty_checkout "$dir"
    log "updating $name checkout in $dir"
    git -C "$dir" remote set-url origin "$url"
  else
    log "cloning $name from $url"
    # Git accepts the empty SDK directory created by its parent checkout and
    # rejects nonempty destinations without overwriting their files.
    git clone "$url" "$dir" || fail "failed to clone $name from $url
Check network access and the public GitHub repository URL."
  fi

  git -C "$dir" fetch --prune --tags origin "$ref" || fail "failed to fetch $name ref $ref from $url"
  git -C "$dir" checkout --detach FETCH_HEAD || fail "failed to check out $name ref $ref"
  local actual
  actual="$(git -C "$dir" rev-parse HEAD)"
  log "$name selected $ref at $actual"
}

install_apt_packages() {
  require_command apt-get
  local packages=(
    build-essential
    cmake
    pkg-config
    protobuf-compiler
    protobuf-compiler-grpc
    libprotobuf-dev
    libgrpc++-dev
    librocksdb-dev
    libyaml-cpp-dev
    python3-dev
    python3-venv
    python3-pip
  )

  if [ "$(id -u)" -eq 0 ]; then
    run env DEBIAN_FRONTEND=noninteractive apt-get update
    run env DEBIAN_FRONTEND=noninteractive apt-get install -y "${packages[@]}"
  else
    command -v sudo >/dev/null 2>&1 || fail "apt installation requires root or sudo. Run as root, install sudo, or rerun with --skip-apt after installing system dependencies."
    run sudo env DEBIAN_FRONTEND=noninteractive apt-get update
    run sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y "${packages[@]}"
  fi
}

write_stack_env() {
  local agent_dir="$1" venv_python="$2" server_bin="$3"
  local env_path="$agent_dir/openevent-stack/runtime/config/env.sh"
  mkdir -p "$(dirname "$env_path")"
  log "writing local stack environment: $env_path"
  cat >"$env_path" <<EOF
#!/usr/bin/env bash

# Generated by scripts/bootstrap_ubuntu_venv.sh.
# Local machine overrides for openevent-stack scripts.

PYTHON_BIN="\${PYTHON_BIN:-$venv_python}"
OPENEVENT_SERVER_BIN="\${OPENEVENT_SERVER_BIN:-$server_bin}"
EXTRA_PYTHONPATH="\${EXTRA_PYTHONPATH:-}"
EOF
  chmod +x "$env_path"
}

verify_python_imports() {
  local python="$1"
  log "verifying Python runtime imports"
  bootstrap_python "$python" -I -B - <<'PY'
import importlib

modules = [
    "openevent.sdk",
    "openevent.chat_sdk",
    "openevent.chat_app",
    "openevent.model_proxy",
    "openevent.model_proxy_sdk",
    "openevent.cmd_sdk",
    "openevent.cmd_worker",
    "openevent.view",
    "chat_model_agent",
    "yaml",
]
missing = []
for module in modules:
    try:
        importlib.import_module(module)
    except Exception as exc:
        missing.append(f"{module}: {exc}")
if missing:
    raise SystemExit("Python runtime import check failed:\n" + "\n".join(missing))

from chat_model_agent.dependencies import require_dependencies

require_dependencies()
PY
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEMO_SOURCE="$(cd "$SCRIPT_DIR/.." && pwd)"
WORKDIR="$DEMO_SOURCE/runtime"
INSTALL_APT=1

while [ "$#" -gt 0 ]; do
  case "$1" in
    --workdir)
      [ "$#" -ge 2 ] || fail "--workdir requires a directory"
      WORKDIR="$2"
      shift 2
      ;;
    --skip-apt)
      INSTALL_APT=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      fail "unknown argument: $1"
      ;;
  esac
done

PYTHON_BIN="${PYTHON_BIN:-python3}"
OPENEVENT_URL="${OPENEVENT_URL:-https://github.com/openevent-official/openevent.git}"
OPENEVENT_REF="${OPENEVENT_REF:-HEAD}"
OPENEVENT_SDK_URL="${OPENEVENT_SDK_URL:-https://github.com/openevent-official/openevent-sdk.git}"
OPENEVENT_SDK_REF="${OPENEVENT_SDK_REF:-HEAD}"
OPENEVENT_MODULES_CHAT_URL="${OPENEVENT_MODULES_CHAT_URL:-https://github.com/openevent-official/openevent-modules-chat.git}"
OPENEVENT_MODULES_CHAT_REF="${OPENEVENT_MODULES_CHAT_REF:-HEAD}"
OPENEVENT_MODEL_PROXY_URL="${OPENEVENT_MODEL_PROXY_URL:-https://github.com/openevent-official/openevent-modules-model-proxy.git}"
OPENEVENT_MODEL_PROXY_REF="${OPENEVENT_MODEL_PROXY_REF:-HEAD}"
OPENEVENT_MODULES_CMD_URL="${OPENEVENT_MODULES_CMD_URL:-https://github.com/openevent-official/openevent-modules-cmd.git}"
OPENEVENT_MODULES_CMD_REF="${OPENEVENT_MODULES_CMD_REF:-HEAD}"
OPENEVENT_VIEW_URL="${OPENEVENT_VIEW_URL:-https://github.com/openevent-official/openevent-view.git}"
OPENEVENT_VIEW_REF="${OPENEVENT_VIEW_REF:-HEAD}"
JOBS="${JOBS:-$(nproc 2>/dev/null || printf '2')}"
export PYTHONDONTWRITEBYTECODE=1
# Use one compile setting so an inherited alias cannot override it.
unset PIP_NO_COMPILE
export PIP_COMPILE=0

WORKDIR="$(abs_path "$WORKDIR")"
SRC_DIR="$WORKDIR/sources"
VENV_DIR="$WORKDIR/venv"

if [ "$INSTALL_APT" -eq 1 ]; then
  install_apt_packages
fi

require_command git
require_command cmake
require_command make
require_command "$PYTHON_BIN"
require_command protoc
require_command grpc_cpp_plugin
check_python_version "$PYTHON_BIN"
check_cmake_version

run mkdir -p "$SRC_DIR"

clone_or_update "OPENEVENT" "$OPENEVENT_URL" "$OPENEVENT_REF" "$SRC_DIR/openevent"
clone_or_update "OPENEVENT_MODULES_CHAT" "$OPENEVENT_MODULES_CHAT_URL" "$OPENEVENT_MODULES_CHAT_REF" "$SRC_DIR/openevent-modules-chat"
clone_or_update "OPENEVENT_MODEL_PROXY" "$OPENEVENT_MODEL_PROXY_URL" "$OPENEVENT_MODEL_PROXY_REF" "$SRC_DIR/openevent-modules-model-proxy"
clone_or_update "OPENEVENT_MODULES_CMD" "$OPENEVENT_MODULES_CMD_URL" "$OPENEVENT_MODULES_CMD_REF" "$SRC_DIR/openevent-modules-cmd"
clone_or_update "OPENEVENT_VIEW" "$OPENEVENT_VIEW_URL" "$OPENEVENT_VIEW_REF" "$SRC_DIR/openevent-view"

log "initializing OpenEvent SDK checkout"
# Register the submodule without checking out the parent's recorded SDK commit.
# Both the server's proto generation and SDK installation use the selected SDK.
git -C "$SRC_DIR/openevent" submodule init || fail "failed to initialize OpenEvent submodules"
git -C "$SRC_DIR/openevent" config submodule.openevent-sdk.url "$OPENEVENT_SDK_URL"
clone_or_update "OPENEVENT_SDK" "$OPENEVENT_SDK_URL" "$OPENEVENT_SDK_REF" "$SRC_DIR/openevent/openevent-sdk"

log "creating Python venv: $VENV_DIR"
bootstrap_python "$PYTHON_BIN" -m venv "$VENV_DIR" || fail "failed to create venv. Install python3-venv and rerun."
VENV_PYTHON="$VENV_DIR/bin/python"

run bootstrap_python "$VENV_PYTHON" -m pip install --no-compile 'pip>=22.3' 'setuptools>=68' wheel build

run make -C "$SRC_DIR/openevent" BUILD_DIR=build BUILD_TYPE=Release JOBS="$JOBS" build
SERVER_BIN="$SRC_DIR/openevent/build/openevent_server"
[ -x "$SERVER_BIN" ] || fail "OpenEvent server binary was not created: $SERVER_BIN"

run make -C "$SRC_DIR/openevent/openevent-sdk" PYTHON="$VENV_PYTHON" install
run make -C "$SRC_DIR/openevent-modules-chat" PYTHON="$VENV_PYTHON" install
run make -C "$SRC_DIR/openevent-modules-model-proxy" PYTHON="$VENV_PYTHON" install
run make -C "$SRC_DIR/openevent-modules-cmd" PYTHON="$VENV_PYTHON" install
run make -C "$SRC_DIR/openevent-view" PYTHON="$VENV_PYTHON" install

AGENT_DIR="$DEMO_SOURCE"
run make -C "$AGENT_DIR" PYTHON="$VENV_PYTHON" install
write_stack_env "$AGENT_DIR" "$VENV_PYTHON" "$SERVER_BIN"
verify_python_imports "$VENV_PYTHON"

cat <<EOF

Bootstrap completed.

Workspace:
  $WORKDIR

Python venv:
  $VENV_DIR

OpenEvent server:
  $SERVER_BIN

Next steps:
  source "$VENV_DIR/bin/activate"
  cd "$AGENT_DIR"
  read README.md for stack configuration and startup
EOF

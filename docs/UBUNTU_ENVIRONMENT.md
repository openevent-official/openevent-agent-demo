# Ubuntu Environment Preparation

[中文版](UBUNTU_ENVIRONMENT_cn.md)

This document explains how to build a local OpenEvent Agent Demo runtime from a
fresh Ubuntu environment. It currently covers Ubuntu only.

## Goal

After preparation, the local checkout will contain:

- `runtime/src/`: OpenEvent, IM, model-proxy, cmd, and view source checkouts
  cloned from GitHub and pinned to explicit commits.
- `runtime/venv/`: the Python venv used by Agent Demo.
- `runtime/src/openevent/build/openevent_server`: the locally built OpenEvent
  server binary.
- `openevent-stack/config/env.sh`: generated local path overrides pointing to
  the venv and server binary above.

The script does not start the stack and does not write real IM/LLM credentials.

## Requirements

Ubuntu 24.04 LTS is recommended. It already includes Git and a Python 3.12
runtime. The environment still needs these additional build/runtime packages:

- Python 3.10+
- CMake 3.20+
- C++20 build toolchain
- Protobuf, `protoc`, and `grpc_cpp_plugin`
- RocksDB development library
- yaml-cpp development library

`scripts/bootstrap_ubuntu_venv.sh` installs these system dependencies with
`apt-get` by default. If the machine already has system dependencies installed,
use `--skip-apt`.

To manually install missing dependency packages first, run:

```bash
sudo apt-get update
sudo apt-get install -y \
  build-essential \
  cmake \
  pkg-config \
  protobuf-compiler \
  protobuf-compiler-grpc \
  libprotobuf-dev \
  libgrpc++-dev \
  librocksdb-dev \
  libyaml-cpp-dev \
  python3-dev \
  python3-venv \
  python3-pip
```

## Start From The Repository Root

This document assumes you are already in the `openevent-agent-demo` repository
root. The script clones the other OpenEvent dependency projects from public
GitHub repositories.

Build the full local runtime. By default, the script installs missing system
dependencies:

```bash
scripts/bootstrap_ubuntu_venv.sh --workdir runtime
```

If system dependencies have already been installed manually, skip the script's
apt step:

```bash
scripts/bootstrap_ubuntu_venv.sh --workdir runtime --skip-apt
```

Activate the venv:

```bash
source runtime/venv/bin/activate
```

## What The Script Does

`scripts/bootstrap_ubuntu_venv.sh --workdir runtime` runs these steps in order:

1. Install Ubuntu system dependencies.
2. Clone dependency projects from public GitHub repositories and check out the
   commits declared by the script.
3. Initialize the OpenEvent server Git submodules. The Python SDK comes from its
   pinned `openevent-sdk` submodule.
4. Create `runtime/venv`.
5. Build the OpenEvent server.
6. Install OpenEvent's SDK, IM, model-proxy, cmd, and view into the same venv
   through each subproject's `make install`.
7. Generate `openevent-stack/config/env.sh`.
8. Verify that required runtime modules can be imported from the venv.

Bootstrap dependency repositories:

```bash
https://github.com/openevent-official/openevent.git
https://github.com/openevent-official/openevent-modules-im.git
https://github.com/openevent-official/openevent-modules-model-proxy.git
https://github.com/openevent-official/openevent-modules-cmd.git
https://github.com/openevent-official/openevent-view.git
```

The `*_REF` defaults in the script are commits validated as one combination.
Repository URLs and their commit/tag refs can be overridden together:

```bash
OPENEVENT_URL=https://github.com/openevent-official/openevent.git \
OPENEVENT_REF=a1d2d97d0870dd5fc61af329bf0cb94cd124aafa \
OPENEVENT_MODULES_IM_URL=https://github.com/openevent-official/openevent-modules-im.git \
OPENEVENT_MODULES_IM_REF=8aebcb35506d7e60384b548800830273bc1781f7 \
OPENEVENT_MODEL_PROXY_URL=https://github.com/openevent-official/openevent-modules-model-proxy.git \
OPENEVENT_MODEL_PROXY_REF=60fe36fe30fa5b92b1dffa535082686ea28e75ef \
OPENEVENT_MODULES_CMD_URL=https://github.com/openevent-official/openevent-modules-cmd.git \
OPENEVENT_MODULES_CMD_REF=bc92ef9c4b46295e098021d5a637c8c7b09dc9b0 \
OPENEVENT_VIEW_URL=https://github.com/openevent-official/openevent-view.git \
OPENEVENT_VIEW_REF=9127056bf0a88f7091c3d1b21e4186ea60180706 \
scripts/bootstrap_ubuntu_venv.sh --workdir runtime
```

After overriding `*_REF`, the refs must still form a protocol- and
configuration-compatible set. Only the default combination is validated by the
project.

## Common Failures

Missing Python 3.10+:

```text
Python 3.10+ is required.
```

Fix: install Python 3.10 or newer and pass it through `PYTHON_BIN`:

```bash
PYTHON_BIN=/usr/bin/python3.10 scripts/bootstrap_ubuntu_venv.sh --workdir runtime
```

Missing CMake 3.20+:

```text
CMake 3.20+ is required
```

Fix: upgrade CMake and rerun the script.

Public GitHub repository clone failure:

```text
failed to clone ...
```

Fix: check network access, proxy settings, and repository URLs. If a mirror is
needed, override the repository URL with the corresponding environment variable.

Existing checkout has local changes:

```text
existing checkout has local changes
```

Fix: enter the corresponding `runtime/src/<project>` directory, then commit,
stash, or remove that directory before rerunning the script.

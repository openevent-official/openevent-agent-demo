# Ubuntu Environment Preparation

[中文版](UBUNTU_ENVIRONMENT_cn.md)

This document explains how to build a local OpenEvent Agent Demo runtime from a
fresh Ubuntu environment. It currently covers Ubuntu only.

## Goal

After preparation, the local checkout will contain:

- `runtime/src/`: OpenEvent, SDK, IM, model-proxy, and view source checkouts
  cloned from GitHub.
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
2. Clone dependency projects from public GitHub repositories into `runtime/src/`.
3. Initialize the OpenEvent server Git submodule.
4. Create `runtime/venv`.
5. Build the OpenEvent server.
6. Install SDK, IM, model-proxy, and view into the same venv through each
   subproject's `make install`.
7. Generate `openevent-stack/config/env.sh`.
8. Verify that required runtime modules can be imported from the venv.

Default public dependency repositories:

```bash
https://github.com/openevent-official/openevent.git
https://github.com/openevent-official/openevent-sdk.git
https://github.com/openevent-official/openevent-modules-im.git
https://github.com/openevent-official/openevent-modules-model-proxy.git
https://github.com/openevent-official/openevent-view.git
```

To override repository URLs, pass environment variables before the script
command:

```bash
OPENEVENT_URL=https://github.com/openevent-official/openevent.git \
OPENEVENT_SDK_URL=https://github.com/openevent-official/openevent-sdk.git \
OPENEVENT_MODULES_IM_URL=https://github.com/openevent-official/openevent-modules-im.git \
OPENEVENT_MODEL_PROXY_URL=https://github.com/openevent-official/openevent-modules-model-proxy.git \
OPENEVENT_VIEW_URL=https://github.com/openevent-official/openevent-view.git \
scripts/bootstrap_ubuntu_venv.sh --workdir runtime
```

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

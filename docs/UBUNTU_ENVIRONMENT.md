# Ubuntu Environment Preparation

The environment script prepares OpenEvent, Chat, Model Proxy, Cmd, View and this project's installed package for Chat Agent.

## Preparation and installation

Run from the project directory:

```bash
scripts/bootstrap_ubuntu_venv.sh
# When system dependencies are already installed:
scripts/bootstrap_ubuntu_venv.sh --skip-apt
# Use another environment workspace:
scripts/bootstrap_ubuntu_venv.sh --workdir /opt/openevent/runtime --skip-apt
```

The default workspace is `runtime/` under this project.
The script installs compiler tools, CMake, Protobuf/gRPC, RocksDB, yaml-cpp and Python development/venv support,
creates `venv/`, and fetches the latest commit on each dependency's remote default branch into `sources/` on every run.
The SDK is fetched separately in `sources/openevent/openevent-sdk`; the server build and SDK installation use that same checkout.
Existing checkouts must have no source file changes; source changes are never overwritten.
After a fetch failure, rerun in the same work directory; completed clean clones are reused.
The SDK commit change made by the script may appear as a modified submodule reference in the OpenEvent checkout;
this alone does not prevent another run. The script logs the actual commit selected for each component.

OpenEvent builds through `make build BUILD_TYPE=Release`. Python packages use their own `make install`:
first that SDK checkout, then Chat, Model Proxy, Cmd, View and finally this project.
Only artifacts from the current successful build are installed. Each component's package is replaced even at the same version,
while third-party dependencies are installed only when missing or incompatible with package declarations.
This project's package is built from the workspace containing the script, including uncommitted changes;
the script does not pull or replace that workspace.
The environment script prepares build tools in the deployment venv for the full stack. To build or install the Demo on its own,
see [Build and Install](../README.md#build-and-install).
Temporary build files go under each project's `build/` and release artifacts under `dist/`.
The environment script's own Python operations, including venv creation and build-tool installation, keep caches and
temporary files in this project's `build/pip-cache/` and `build/tmp/`, even with a different `--workdir`;
each component's Makefile continues to manage these directories for its own build and installation.
Installation stops on failure without falling back to old artifacts.

URL/ref overrides are:
`OPENEVENT_URL/REF`, `OPENEVENT_SDK_URL/REF`, `OPENEVENT_MODULES_CHAT_URL/REF`,
`OPENEVENT_MODEL_PROXY_URL/REF`, `OPENEVENT_MODULES_CMD_URL/REF`, `OPENEVENT_VIEW_URL/REF`.
All `*_REF` values default to `HEAD`, meaning the remote default branch's latest commit at each run.
Set an explicit branch, tag or commit to choose another revision; selected versions must continue to meet the
[Runtime dependency requirements](RUNTIME_RECONCILER.md#dependencies).
`PYTHON_BIN` selects the Python interpreter for the venv. This script builds the SDK from source, requires Python 3.10–3.12 and installs pip 22.3 or newer in the venv. `JOBS` controls build concurrency.

Installation verifies module imports, the SDK, Chat `TurnWriter` construction from known state and `serialize_payload`,
plus reset, streaming and explicit-UUID capabilities, without importing an SDK checkout through PYTHONPATH.
The script generates `openevent-stack/runtime/config/env.sh` with deployment Python and OpenEvent server paths.
Stack scripts prefer explicit environment variables, then these generated paths, then the defaults in `env.sh`.
This makes a custom `--workdir` take effect without overriding explicit `PYTHON_BIN` or `OPENEVENT_SERVER_BIN` values.
It does not start services or populate model credentials or the web token.

## Configuration and startup

Edit the single deployment configuration, `openevent-stack/stack.yaml`, following
[Runtime configuration](RUNTIME_RECONCILER.md). Provide four distinct user, Agent, Model Proxy and Cmd Worker identities,
a Chat web token, a model Provider, and service addresses.
Runtime starts OpenEvent from that configuration,
prepares tokens and the Model, Cmd and WAL Channels, saves their bindings, and generates all component configuration.
To adopt existing Channels, specify their IDs as described in Runtime configuration.

### Start the full deployment

```bash
openevent-stack/bootstrap.sh --dry-run
openevent-stack/bootstrap.sh --apply
openevent-stack/status.sh
openevent-stack/logs.sh chat-model-agent
openevent-stack/stop.sh
openevent-stack/start.sh
```

Runtime documentation defines startup order, object-command environment and shutdown steps.
Local stack scripts always keep runtime configurations, data and logs under `openevent-stack/runtime/`, regardless of `runtime.root`.
This is the local scripts' convention; see Runtime documentation for the generic command's runtime directory setting.
It differs from the environment installation script's `--workdir`, which defaults to `runtime/` under the project root and contains dependency checkouts and the Python venv.
Environment preparation does not automatically execute browser, model or command tasks.

## Updating a running deployment

For a code update that keeps the deployment protocols unchanged, stop the services, install the packages, and start the services again.
Installing packages alone does not switch running processes to the new code; with unchanged configuration,
`apply` and `start` do not force those processes to restart either.

To update only this project from its current workspace, run these commands in order from the project directory,
continuing only after each succeeds. Use the current deployment's Python environment:

```bash
openevent-stack/stop.sh
make install PYTHON=/absolute/path/to/deployment/python
openevent-stack/start.sh
```

To fetch this project's latest remote code first, run `git pull --ff-only` in a clean workspace before `make install`.
If the workspace contains local changes, preserve or finish those changes before pulling; installation itself uses them as they stand.

To update dependencies, replace the middle step with the relevant projects' `make install`, or rerun the environment
preparation script using the original workspace path. The script fetches their latest remote default-branch commits by default;
explicit URL/REF options select different sources or revisions.
If configuration also changes, finish with `openevent-stack/bootstrap.sh --apply` instead of `start.sh`.
For stop confirmation and restart prerequisites, see the
[Runtime guide](RUNTIME_RECONCILER.md#startup-updates-and-shutdown).

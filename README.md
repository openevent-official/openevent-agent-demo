# OpenEvent Agent Demo

[中文版](README_cn.md)

OpenEvent Agent Demo shows how to compose OpenEvent, the IM module, the
model-proxy module, and OpenEvent View into one local Agent runtime. The demo
connects a P2P IM conversation to an OpenAI-compatible model provider while all
inputs, model requests, model results, replies, and Agent WAL records flow
through OpenEvent channels.

## Quick Start

1. Build the Ubuntu 24.04 local runtime and activate the venv:

```bash
scripts/bootstrap_ubuntu_venv.sh --workdir runtime
source runtime/venv/bin/activate
```

For system package details, `--skip-apt`, custom repository URLs, and common
failures, see [docs/UBUNTU_ENVIRONMENT.md](docs/UBUNTU_ENVIRONMENT.md).

2. Fill in real IM and model provider values in
   [openevent-stack/stack.yaml](openevent-stack/stack.yaml).
3. Validate and apply the stack template:

```bash
./openevent-stack/bootstrap.sh --dry-run
./openevent-stack/bootstrap.sh --apply
./openevent-stack/status.sh
```

## Full Stack Runtime

`openevent-stack/` is the local template for running the full OpenEvent Agent
demo stack. Configuration templates and scripts live directly in that
directory.

Components:

- `openevent` server
- `im-p2p-syncer`
- `model-proxy`
- `im-model-agent`
- `openevent-view`

Adjust these files for the local environment before applying the stack:

- `openevent-stack/stack.yaml`

`scripts/bootstrap_ubuntu_venv.sh` writes local path overrides to
`openevent-stack/config/env.sh`. The tracked `openevent-stack/env.sh` remains a
portable default template.

`bootstrap.sh --apply` uses `scripts/reconcile_runtime.py` to generate final
configuration for the four core components, start or restart core processes, and
create or reuse OpenEvent tokens/channels. `openevent-view` is a required
component of the Agent demo. It depends on OpenEvent and starts only after the
core flow has completed.

Daily operation usually needs only these scripts:

| Script | Purpose |
| --- | --- |
| `openevent-stack/bootstrap.sh` | Initialize or refresh the full runtime configuration. `--dry-run` validates and prints the plan. `--apply` starts OpenEvent, creates or reuses tokens and channels, writes final configuration, starts or restarts core processes, then starts view after OpenEvent is running and prints process status. |
| `openevent-stack/start.sh` | Start processes from already generated configuration in order: OpenEvent, model-proxy, IM syncer, Agent, then view after OpenEvent. Prints process status. Does not create tokens or channels. |
| `openevent-stack/stop.sh` | Stop all local processes in reverse order. |
| `openevent-stack/status.sh` | Show all local process status. |
| `openevent-stack/logs.sh` | Without arguments, list log files. With a process name, follow that process log, for example `./openevent-stack/logs.sh model-proxy`. |

These helper files are normally only needed for configuration or
troubleshooting:

| File | Purpose |
| --- | --- |
| `openevent-stack/env.sh` | Portable local-path defaults. Generated machine-specific overrides live in `openevent-stack/config/env.sh`. It does not hold component runtime parameters. |
| `openevent-stack/stack.yaml` | Declarative template for the whole stack: ports, OpenEvent storage paths, principals, IM provider, model provider, session/channel names, and view port. |
| `openevent-stack/common.sh` | Shared paths, directories, and `PYTHONPATH` setup for all scripts. |
| `openevent-stack/process.sh` | Local process manager. It acts as a simple `supervisorctl` replacement for `reconcile_runtime.py` and is also called by `start` / `stop` / `status`. It cleans stale duplicate processes with the same configuration to avoid multiple instances on the same port causing token/channel state inconsistency. |
| `openevent-stack/render-view-config.sh` | Generate `config/openevent-view.yaml` from `stack.yaml`. This is an internal step before `bootstrap.sh` / `start.sh` starts view; view depends on OpenEvent and is not started separately. |

Generated configuration, state, plan, and secret files are written to
`openevent-stack/config/`.

Other runtime artifacts are written under `openevent-stack/` by default:

- `data/`
- `logs/`
- `run/`

## Documentation

This repository's main documents:

| Document | Purpose |
| --- | --- |
| [docs/UBUNTU_ENVIRONMENT.md](docs/UBUNTU_ENVIRONMENT.md) | Ubuntu 24.04 environment and venv preparation |
| [docs/RUNTIME_RECONCILER.md](docs/RUNTIME_RECONCILER.md) | Declarative runtime configuration, generated files, token/channel reconciliation |
| [docs/IM_MODEL_AGENT.md](docs/IM_MODEL_AGENT.md) | Agent configuration and message processing flow |
| [docs/AGENT_WAL_PROTOCOL.md](docs/AGENT_WAL_PROTOCOL.md) | Agent WAL channel and payload protocol |

Related project documentation:

| Topic | Project document |
| --- | --- |
| OpenEvent gRPC API and proto | `openevent-sdk` `docs/API.md`, `proto/openevent.proto` |
| OpenEvent server build/configuration | `openevent` `README.md`, `docs/CONFIG.md` |
| IM payload protocol | `openevent-modules-im` `docs/IM_PROTOCOL.md` |
| IM SDK and P2P syncer | `openevent-modules-im` `docs/IM-PROTOCOL-SDK.md`, `docs/IM-P2P-SYNCER.md` |
| LLM payload protocol | `openevent-modules-model-proxy` `docs/LLM_PROTOCOL.md` |
| model-proxy worker and SDK | `openevent-modules-model-proxy` `docs/CONFIGURATION.md`, `docs/SDK_USAGE.md` |
| OpenEvent View | `openevent-view` `README.md` |

## Test

Use the repository test entry point:

```bash
make test
```

Tests use packages installed in the current Python environment. If a dependency
package is missing, install it in that environment first.

## Repository Layout

| Path | Purpose |
| --- | --- |
| `im_model_agent/` | Agent process implementation |
| `scripts/reconcile_runtime.py` | Declarative runtime configuration and channel/token reconciliation |
| `scripts/bootstrap_ubuntu_venv.sh` | Ubuntu 24.04 venv environment preparation |
| `openevent-stack/` | Local full-stack configuration template and process scripts |
| `docs/` | Agent-specific protocol, runtime, and environment documents |
| `tests/` | Unit tests and stack-script checks |

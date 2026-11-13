# OpenEvent Agent Demo

> Implementation status: event input, asynchronous tools, context compression, oversized-message rejection, prompt-file configuration, and WAL v5 are implemented. See [validation scope](#validation-scope) for completed checks and unverified behavior.

[中文版](README_cn.md)

This project combines OpenEvent, Chat, Model Proxy, Cmd, and OpenEvent View into a runnable Chat Agent.
Users ask questions, attach files, or cancel replies in Chat. The Agent streams replies, runs commands when needed,
and records recovery facts through OpenEvent. Conversations are discovered dynamically from Chat;
creating a conversation requires neither a per-session configuration nor another deployment apply.
User messages are complete `turn.single` events; Agent replies still stream. New user messages can trigger inference while tools run,
with model calls and actual tool execution serialized separately within each conversation. Summaries compress old events for later inputs.
Model summaries retain necessary task information while execution records preserve full tool parameters.
History meeting its size target guarantees room for one compliant input; later tool results still enter event history in order.
See the [Agent guide](docs/CHAT_MODEL_AGENT.md#conversations-and-replies) for behavior and allowances.

Conversation discovery uses system messages from Fetch across all readable Channels. Adoption conditions, Chat's joining-order prerequisite,
and restart behavior are specified in the [Agent guide](docs/CHAT_MODEL_AGENT.md#conversation-discovery).
The [WAL protocol](docs/AGENT_WAL_PROTOCOL.md) defines persistent fields. Documentation maintains the current scheme,
without compatibility or migration designs for old code.
Text display batches complete model fragments without splitting them and records their sources as seq ranges.
See [WAL display sources](docs/AGENT_WAL_PROTOCOL.md#5-effects-and-display-sources)
for the contract.
The current WAL is `agent.wal.v5`, with model I/O version `3`, without old-format compatibility or migration.
The public format specification, default prompt files, request examples, implementation, and deployment template use the same configuration and protocols.

The Agent entry point is `chat-model-agent`, using configuration version `chat-agent.v1`.
Deployment uses installed distributions of `openevent-sdk >= 0.13.0`, Chat, Model Proxy, Cmd, and View.

## Quick Start

Prepare the environment on Ubuntu 24.04 and activate the venv:

```bash
scripts/bootstrap_ubuntu_venv.sh --workdir runtime
source runtime/venv/bin/activate
```

The script fetches the latest dependency code by default and installs this project's current workspace;
see the [environment guide](docs/UBUNTU_ENVIRONMENT.md) for version selection and updates.
Set the model provider, user and Agent identities, Chat web token, and service ports in
[openevent-stack/stack.yaml](openevent-stack/stack.yaml).
That configuration also specifies both complete prompt file paths and the context byte budget; see [Runtime prompt files](docs/RUNTIME_RECONCILER.md#prompt-files).
Default texts are provided in [system.txt](openevent-stack/prompts/system.txt) and [context_compression.txt](openevent-stack/prompts/context_compression.txt).
Read the [model input/output format](docs/MODEL_IO.md) before writing or editing prompts.
Runtime prepares tokens and all three auxiliary Channels and generates component configuration.
Existing Channels can be explicitly selected as described in [Runtime configuration](docs/RUNTIME_RECONCILER.md). Then run:

```bash
./openevent-stack/bootstrap.sh --dry-run
./openevent-stack/bootstrap.sh --apply
./openevent-stack/status.sh
```

Open the configured Chat page and create a conversation. The model receives attachment object IDs;
when it needs their contents, it selects the needed attachments in `exec.object_ids` and invokes
`openevent-read-object <object_id>`. See the [Agent guide](docs/CHAT_MODEL_AGENT.md) for usage.

Stop all services:

```bash
./openevent-stack/stop.sh
```

## Build, Install, and Test

Use a Python environment with the SDK and the Chat, Model Proxy, Cmd, and View distributions installed:

```bash
make build PYTHON=python3
make install PYTHON=python3
make test PYTHON=python3
```

Building also requires `setuptools >= 68` and `wheel`. `make build` builds under `build/` and writes the current wheel
to `dist/`. `make install` rebuilds, then checks the wheel's declared dependencies and installs only missing or
incompatible ones. After that succeeds, it force-replaces only this project's installed package, even when its version
is unchanged. Dependency resolution or download failures stop before replacing the installed project package.
Generated files, caches, and test data stay under `build/`; Python bytecode is not generated in source directories.

`make test-install` checks installation order and same-version replacement offline in temporary virtual environments
under `build/`; it requires Python's `venv` support and does not require the OpenEvent components.

For cross-module changes, use `make test-current-dependencies`: explicitly select the five dependency source directories,
rebuild and install them in a separate environment, then run tests.
See [development validation](docs/UBUNTU_ENVIRONMENT.md#development-validation) for arguments and the environment-only target.
An older installed package with the same version does not establish compatibility with current source code.

For WAL indexes and Runtime local state writes only, run `make test-unit PYTHON=python3`.
This target uses the same dependency environment and runs only a subset of local tests; it does not replace the full dependency and integration checks in `make test`.

Installation provides two commands:

```bash
chat-model-agent --config /etc/openevent-agent/agent.json
openevent-read-object 12345
```

The latter obtains attachment credentials from its Cmd task environment, supplied with the run request, and uses the object service address configured by Runtime. It does not require Agent to be online.
See the [Agent guide](docs/CHAT_MODEL_AGENT.md) for details.

## Validation Scope

Automated validation includes unit tests, Runtime and process-script tests, and Agent integration tests using the real Chat, Model, and Cmd SDKs with an in-memory event service.
Coverage includes new input while tools run, cancellation isolation between replies, compaction checkpoints and recovery,
history targets across multiple batches, independent rejection of oversized messages, failure pauses and new-input resumption,
and recovery using frozen budgets. These tests run through the project's `make` targets and do not call an external model Provider.

On 2026-11-13, a request containing the actual Agent prompts, tool schemas, and historical input was tested against a real external model Provider,
which returned HTTP 200 and a complete streaming response. This check established that the Provider accepted and completed that request;
it did not exercise the full Agent and OpenEvent event-processing path or execute model tools.
Production end-to-end validation of Chat, Agent, OpenEvent, Model Proxy, and Cmd remains incomplete.

## Daily Operation

`openevent-stack/` contains the local full-stack template and scripts. Use `bootstrap.sh` to apply configuration,
`start.sh` / `stop.sh` to manage processes, `status.sh` to inspect status, and `logs.sh <process-name>` to follow logs.
The [Runtime Reconciler guide](docs/RUNTIME_RECONCILER.md) owns deployment configuration, startup ordering,
shared auxiliary Channel bindings, and restart requirements; this README does not maintain another set of those rules.

## Documentation

| Document | Purpose |
| --- | --- |
| [Environment setup](docs/UBUNTU_ENVIRONMENT.md) | Ubuntu setup, dependency installation, and troubleshooting |
| [Runtime Reconciler](docs/RUNTIME_RECONCILER.md) | Full deployment configuration, generated files, token/channel and process coordination |
| [Chat Agent](docs/CHAT_MODEL_AGENT.md) | Agent configuration, tools, cancellation, and behavior after failures |
| [Model input/output format](docs/MODEL_IO.md) | Events, summaries, compression requests, tools and responses, default prompts, and complete examples |
| [Agent WAL protocol](docs/AGENT_WAL_PROTOCOL.md) | Persistent fields, stable keys, and source-event references |

The corresponding modules own the public Chat, LLM, and command protocols. The Agent consumes those interfaces
without redefining their underlying protocols.

## Repository Layout

| Path | Purpose |
| --- | --- |
| `chat_model_agent/` | Chat Agent and object-reading command |
| `runtime_reconciler/` | Declarative configuration, channel/token and process coordination |
| `scripts/` | Environment preparation script |
| `openevent-stack/` | Local deployment template and process scripts |
| `docs/` | Project runtime and protocol documentation |
| `tests/` | Agent, Runtime, and script tests |

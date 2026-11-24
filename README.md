# OpenEvent Agent Demo

[中文版](README_cn.md)

OpenEvent Agent Demo is a runnable reference application for developers building agents on OpenEvent.
It shows how a chat agent uses events to connect a web conversation, a model provider, and a Linux command worker.
You can deploy the whole application or use its design as a starting point for your own agent.

## Architecture

Each conversation has its own Chat Channel in OpenEvent. Across conversations, the deployment shares three
other Channels. Model Proxy and Cmd Worker receive requests and return results on the Model and Cmd Channels,
respectively. The Agent writes its decisions and references to source events to the WAL (write-ahead log) Channel.

### OpenEvent Server

The server stores messages from all Channels in one global sequence and enforces who can read and publish them.
It also stores attachments as immutable objects. An Agent coming back online can read the retained events in order,
including user messages that arrived while it was offline. [OpenEvent](https://github.com/openevent-official/openevent)

### Chat

Chat provides the browser page, HTTP server, and Chat SDK. When a user creates a conversation, Chat Server creates
its Channel and stores the display name in its local session directory. It publishes user messages and attachment
references to that Channel and serves the Agent's replies to the browser as they arrive. [Chat](https://github.com/openevent-official/openevent-modules-chat)

### Chat Agent

One Agent process discovers the Chat Channels it has joined and maintains separate model histories and tool queues
for each conversation. It builds model inputs, streams answers to Chat, and feeds tool results into subsequent model calls. After a restart
it uses WAL decisions together with the referenced Chat, Model, and Cmd events to recover each conversation's progress.
[Chat Agent guide](docs/CHAT_MODEL_AGENT.md)

### Model Proxy

The Agent supplies the conversation context for a reply or a history summary. The proxy holds the provider credentials,
calls the configured OpenAI-compatible model service, and publishes response fragments and the final status back
to the Model Channel. [Model Proxy](https://github.com/openevent-official/openevent-modules-model-proxy)

### Cmd

Cmd Worker executes the Agent's shell requests on its Linux host, using the conversation's shared working directory
by default. It reports the outcome on the Cmd Channel and keeps stdout and stderr in local files, so the Agent
can request just the output range it needs. [Cmd](https://github.com/openevent-official/openevent-modules-cmd)

### OpenEvent View

View provides a separate, read-only web page and API for inspecting Channel metadata and message history. An operator
can use their OpenEvent credentials to trace a model request or command result across the Channels they can read.
[OpenEvent View](https://github.com/openevent-official/openevent-view)

## Agent Behavior

Start a conversation in the browser to ask a question or attach a file. Replies appear as they are generated.
You can send a follow-up while a command is running, or cancel an individual reply that is still in progress.
[Conversations and replies](docs/CHAT_MODEL_AGENT.md#conversations-and-replies)

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

## Build and Install

Run these commands from the project root. Make uses the current shell's `python3` by default. Set `PYTHON` to select another Python 3.10+ interpreter with pip.

| Command | Purpose |
| --- | --- |
| `make build` | pip prepares the build tools in an isolated environment. A successful build places its wheel in `dist/`. This step requires only the build dependencies in [pyproject.toml](pyproject.toml). |
| `make install` | Build a fresh wheel and install it into the selected Python environment. pip installs only missing or incompatible runtime dependencies and replaces the Demo itself even at the same version. Requires pip 22.3 or newer. |
| `make clean` | Remove this project's build files, caches, and release packages. Keep Python environments, offline dependencies, and artifacts from other projects. |

For example, this command installs the Demo into the deployment virtual environment.

```bash
make install PYTHON=/opt/openevent/venv/bin/python
```

Build intermediates and caches stay under `build/`. The build and install commands clear inherited
`PYTHONPATH` and `PYTHONHOME`. If the selected Python can import
user-site packages, it also supports `PIP_USER=1 make install`. Unset pip's `--python` and `--root` settings
before installation. To install into a separate directory with `--target` or `--prefix`, run `make build`, then
pass the exact path of the resulting wheel to `python3 -m pip install`.

In `pip.conf`, use `[global]` for a shared package source, or configure `[wheel]` for build dependencies and
`[install]` for runtime dependencies. A dependency resolution or download failure stops installation before
replacing the installed Demo. For an offline installation, place the build and runtime dependencies, including their
transitive dependencies, in `build/wheelhouse/`. Choose wheels that match the target Python and platform, then run the
following command.

```bash
PIP_NO_INDEX=1 PIP_FIND_LINKS=build/wheelhouse make install
```

Installation provides two commands:

```bash
chat-model-agent --config /etc/openevent-agent/agent.json
openevent-read-object 12345
```

The latter obtains attachment credentials from its Cmd task environment, supplied with the run request, and uses the object service address configured by Runtime. It does not require Agent to be online.
See the [Agent guide](docs/CHAT_MODEL_AGENT.md) for details.

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

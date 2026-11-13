# Chat Runtime Reconciler

> Implementation status: prompt-file loading, context byte-budget previews, the Chat text limit, and `agent.wal.v5` deployment configuration are implemented. See the [README](../README.md) for test coverage and external integration limits.

Runtime prepares credentials, three shared auxiliary Channels, component configuration and local processes for one Chat Agent deployment.
Chat Server creates browser sessions and Agent discovers them; Runtime does not maintain a session list or read Chat's session directory.
The following specifies deployment configuration and execution flow.
See the [Agent guide](CHAT_MODEL_AGENT.md#conversation-discovery) for conversation discovery and Agent configuration.
The deployment spec accepts only `chat-runtime.v1`.

## Entry points

Run from the project directory:

```bash
python -B -m runtime_reconciler.cli --spec openevent-stack/stack.yaml --runtime-root "$PWD/openevent-stack/runtime" --dry-run
python -B -m runtime_reconciler.cli --spec openevent-stack/stack.yaml --runtime-root "$PWD/openevent-stack/runtime" --apply
python -B -m runtime_reconciler.cli --spec openevent-stack/stack.yaml --runtime-root "$PWD/openevent-stack/runtime" --start
```

The commands above and the stack scripts run Runtime source from the project directory.
The local deployment also provides `openevent-stack/bootstrap.sh --dry-run|--apply`,
`start.sh`, `stop.sh`, `status.sh` and `logs.sh [program]`.
`--apply` reports progress through five main stages: checking configuration, preparing OpenEvent, resolving credentials and Channels, preparing service configuration, and updating services.
Both `--apply` and `--start` identify services being started or stopped, indicate when waiting for a service to exit, and can report that a reused service is already running.
Each progress line is flushed immediately, including when output is piped. Successful completion still prints a short success message;
`bootstrap.sh --apply` and `start.sh` then show the actual process status of each service and the Chat and View access URLs.
`--dry-run` and `--print-config` continue to output a YAML plan or configuration without progress messages.
The Chat URL uses configured `chat.origin` when present, otherwise an HTTP address built from its listening host and port, with the page path `/api/chat/`.
The View URL uses its listening host and port with the page path `/`. Wildcard listening addresses `0.0.0.0` and `::` are displayed as local addresses `127.0.0.1` and `[::1]`; other IPv6 addresses are also enclosed in brackets.

Local stack scripts always use `openevent-stack/runtime/` as their runtime directory, with generated configuration, state, data, logs and PID files below it.
Symlinks in the local stack directory path are consistently resolved to real paths, so startup, status checks and shutdown identify the same deployment.
They explicitly pass `--runtime-root`, so changing `runtime.root` in the spec does not change their directory; the template omits that field.
The direct commands above use an absolute path so they use the same directory as the local process scripts.
Other deployments can use `runtime.root` or `--runtime-root` through the general command entry point,
with their own process controller reading configuration from the selected directory.

- `--dry-run` checks the user spec and existing deployment bindings, then previews configuration paths without OpenEvent calls or writing runtime configuration, bindings or secrets.
  The plan's `channels` list only known IDs; the entire section is omitted when all Model/Cmd/WAL IDs are unknown. Unknown IDs are resolved automatically during `--apply`.
  The plan has no `unresolved` list; `notes` explains that OpenEvent access tokens will be reused or created automatically during `--apply`, without requiring users to supply them manually.
  Previews use no temporary IDs or tokens and do not pass unresolved configuration to component validators; a successful preview does not mean the complete component configuration has been validated.
- `--apply` first checks the user spec, existing deployment bindings and OpenEvent configuration, which needs no Channel IDs or tokens.
  It makes OpenEvent ready at the target addresses in the order below before preparing actual resources. After obtaining Channel IDs and valid tokens,
  it runs the remaining component validators, commits the complete configuration, then continues startup. After successful initialization, repeated calls reuse bindings and valid credentials.
  Component validation temporary files go under the current working directory's `build/validation/`.
- `--write-diagnostics` is accepted only with `--apply` and exports the optional diagnostic files described below;
  the local script accepts `openevent-stack/bootstrap.sh --apply --write-diagnostics`.
- `--start` is only for deployments whose first initialization completed successfully. Changes to mutable settings, including credentials
  or explicit OpenEvent tokens, require apply first. The process controller and names are fixed
  as described below and cannot be replaced by apply.
- `--runtime-root DIR` overrides the runtime directory.
- `--print-config openevent|chat_server|model_proxy|cmd_worker|agent|view` prints a preview.
  Explicit credentials remain in this output; unresolved fields are `null`. This preview cannot be used directly as runtime configuration.

Relative paths in a spec are resolved against its directory. Generated runtime paths are absolute.
A `supervisor.ctl` without a slash is searched on PATH; otherwise it follows the same path resolution.
Exit codes are 1 for unexpected failures, 2 for spec errors and 3 for reconciliation failures.

## Configuration

Minimal structure, with example tokens and keys replaced by deployment values:

```yaml
version: chat-runtime.v1
runtime:
  name: my-chat
  root: ./data
  supervisor:
    ctl: supervisorctl
openevent:
  grpc_addr: 127.0.0.1:9527
  admin_addr: 127.0.0.1:9528
principals:
  user: 10001
  agent: 10002
  model_proxy: 20001
  cmd_worker: 30001
cmd:
  max_concurrent_tasks: 4
  default_timeout_ms: 300000
chat:
  host: 127.0.0.1
  port: 8081
  origin: null
  web_token: replace-with-web-token
  max_user_message_bytes: 65536
model:
  provider_name: default
  base_url: https://api.example.com
  api_key: replace-with-model-key
  model: example-model
agent:
  system_prompt_file: ./prompts/system.txt
  context_compression_prompt_file: ./prompts/context_compression.txt
  context_max_input_bytes: 1048576
```

The four principals must be distinct positive uint64 values. Only Chat Server receives the user credential;
Agent, Model Proxy and Cmd Worker each use their own credential.
The web token is separate from OpenEvent tokens. The model key only enters Model Proxy configuration.

This example uses the same total request budget as the [complete request examples](examples/README.md), together with
Chat's default text limit and Agent's default tool-output allowance. Example checks cover text escaping and a tool returning
both stdout and stderr; attachment descriptions, reply context, and error details still undergo complete-event admission.
After changing prompts, the model name, or allowances, inspect the budget preview below. Text and output allowances count raw
bytes and cannot be used directly as encoded model-event allowances.

| Configuration | Meaning and default |
| --- | --- |
| `runtime.name` | Required stable deployment ID |
| `runtime.root` | Runtime directory for the general entry point; defaults to `runtime/<name>`; local stack scripts always override it with `openevent-stack/runtime/` |
| `runtime.supervisor.ctl` | Process control accepting `status/start/stop <program>`; supervisorctl; fixed after the first apply |
| `runtime.supervisor.programs` | Optional initial logical-to-process name overrides, below; fixed after the first apply |
| `openevent.storage.path` | `data/openevent` under the runtime directory |
| `openevent.max_payload_bytes` | 16777216; generated configuration is also checked by module parsers. The same cap is passed to Agent, Model Proxy and Cmd; see [Agent configuration](CHAT_MODEL_AGENT.md#agent-configuration) for encoded-size checks |
| `tokens` | Optional mapping keyed by user, agent, model_proxy, cmd_worker |
| `chat.channels_dir` | `chat-sessions` under the runtime directory; managed exclusively by Chat Server |
| `chat.host/port/origin` | Defaults to 127.0.0.1 / 8081; optional origin defaults to null; written to the same-named fields in `chat-server.json` |
| `chat.max_user_message_bytes` | Optional, 65536 by default; positive integer, excluding booleans. Passed unchanged to the same-named Chat Server field, limiting UTF-8 bytes of individual new user-message text, excluding attachment files and quoted target text; Agent checks its complete model-request budget separately |
| `model.provider_name` | default |
| `model.response_header_ms/idle_ms` | Provider response header/idle timeouts; 60000 each |
| `model.max_concurrency` | Model Proxy concurrency shared by all sessions; 4 |
| `cmd.output_dir` | `cmd-output` under the runtime directory |
| `cmd.max_concurrent_tasks/default_timeout_ms` | 4 / 300000 |
| `channels.model/cmd/wal` | Use `auto` or omit a field to discover or create the Channel during initialization as described below, or supply an existing positive uint64 ID. The entire `channels` mapping may be omitted; `null` and numeric strings are rejected. After initialization, the permanent binding always applies |
| `view.port` | 9999; View listens on 0.0.0.0 |
| `agent.system_prompt_file` | Required UTF-8 file path for the complete system prompt; see below |
| `agent.context_compression_prompt_file` | Required UTF-8 file path for the complete compression prompt; see below |
| `agent.context_max_input_bytes` | Required, with no default; see [Agent context byte budget](CHAT_MODEL_AGENT.md#context-byte-budget) |
| `agent.model_retry_interval_ms` | Optional; passed unchanged to Agent. See [Model Retry Interval](CHAT_MODEL_AGENT.md#model-retry-interval) for the default and waiting rules |
| `rpc.max_retries/timeout_ms` | 3 / 10000; passed to Agent, while other components receive only supported public configuration fields. The RPC retry interval is fixed internally at 100 milliseconds to match the current Chat SDK and is not configurable |

`runtime.startup_timeout_ms` has been removed from the configuration design and is not accepted; startup does not wait for Agent object references to become ready.

Runtime options under `agent` follow [Chat Agent configuration](CHAT_MODEL_AGENT.md). The prompt file inputs below
are read by Runtime into text fields in the generated Agent configuration; Agent does not receive the source paths.
Runtime injects identities, the three Channels, model name and Provider; `work_root` defaults to `workdir` under the runtime directory.
Runtime's `agent.object_target` defaults to the OpenEvent business address and only configures the object reader's
environment in Cmd; it is not written to Agent configuration.
Agent scheduling slots and Model Proxy execution concurrency are configured separately;
see [Agent configuration](CHAT_MODEL_AGENT.md#agent-configuration) for the meaning of Agent slots.

| Logical name | Default process | Configuration |
| --- | --- | --- |
| openevent | openevent | openevent-server.yaml |
| model_proxy | model-proxy | model-proxy.yaml |
| chat_server | chat-server | chat-server.json, including host, port, and origin |
| view | openevent-view | openevent-view.yaml |
| agent | chat-model-agent | chat-model-agent.json |
| cmd_worker | cmd-worker | cmd-worker.yaml |

Configuration is stored under runtime `config/` with mode 0600.
`state.yaml` uses `chat-runtime.state.v1`; `secrets.yaml` uses `chat-runtime.secrets.v1`.
The Chat startup command passes only `--config`; listening parameters and the access URL displayed after startup are read from `chat-server.json`.
Diagnostic files are not written by default. With `--write-diagnostics`, only after apply finishes starting every component
and saves its successful state, Runtime exports `desired.normalized.yaml` (redacted configuration) and `plan.yaml`
(the configuration write plan for this apply), both with mode 0600.
The detailed plan, including fields such as `changed` and `sha256`, is exported only through this option and is not printed in normal success output.
Diagnostic write failures only produce warnings on standard error and do not affect deployment success.
These files are never used for startup, recovery or identifying an existing deployment.
They are snapshots of that export: later runs without the option, or failed exports, may leave old contents. They can be deleted directly.
`initialized: true` is saved only after the first apply finishes starting every component. If deployment state already
exists but that initialization failed or was interrupted, another apply or start fails and preserves the existing state;
this release does not resume partial initialization automatically. Operations decides which resources to retain, whether to rebuild, and the scope of any rebuild as described below.
Before controlling any process, the first apply saves the resolved controller and full process name mapping
as the state's `supervisor` binding. This binding remains fixed for the deployment; it can be selected again only after ending the old deployment and creating new state.
Subsequent entry points reject a missing binding or changed settings before controlling processes;
this release does not support migrating the controller or process names.
State also stores `applied_spec_sha256`, a digest of the full effective configuration used by `--start`;
comparison never relies on the redacted display file. After its preliminary checks, before stopping the first process or overwriting configuration,
apply clears this digest, preserves the last fully committed component configuration records,
and persists the components that need stopping and starting again or updating by merging them into existing `pending_restarts`.
The configuration records and pending restarts together determine which running processes still need updates.
Writing OpenEvent configuration early does not commit the full configuration. Once the remaining components pass validation
and all configuration files have been written, the digest, new configuration records and remaining pending restarts are saved atomically;
OpenEvent startup already completed in the earlier step is recorded as complete at this point.
For a successfully initialized deployment, an update interrupted before this full commit requires apply again;
after it, start may finish the pending restarts.
State must contain `principals` and `work_root` matching the current configuration; a different deployment ID or missing permanent bindings also fails.

## Prompt files

`agent.system_prompt_file` and `agent.context_compression_prompt_file` require explicit nonempty paths.
Resolve relative paths against the spec directory; absolute paths identify deployment files. The repository provides the
[default system prompt](../openevent-stack/prompts/system.txt) and [default compression prompt](../openevent-stack/prompts/context_compression.txt) used by the example.
For a spec in `openevent-stack/`, use the example's `./prompts/` paths directly. For a spec elsewhere, copy the files into its `prompts/` directory or explicitly select deployment paths.
These are repository files, not implicit defaults when required paths are omitted. The `stack.yaml` template explicitly configures both file paths.
The default system file contains complete business instructions and tool, summary and event-input, and attachment guidance. Code no longer appends another protocol prompt implicitly.
Follow the [model input/output format](MODEL_IO.md) when editing prompts; [request examples](examples/README.md) include summaries and compression requests.
See [Agent conversations and replies](CHAT_MODEL_AGENT.md#conversations-and-replies) for how task information in a summary relates to later result events;
Runtime does not maintain a separate task input.
The compression file is a separate complete prompt, not concatenated with the normal system file. Preserve UTF-8 text as read;
the system file may be empty, while the compression file must contain non-whitespace text.
There is no template substitution or parallel inline `agent.system_prompt` or compression-prompt input in the spec.

`--dry-run`, `--print-config`, `--apply`, and `--start` each read both files once while parsing the spec.
Missing, unreadable, or non-UTF-8 files fail before state writes or process control. Preview, comparison, and generation within one operation use that same text snapshot.
Previews read files without writing runtime data or calling OpenEvent; `--print-config agent` displays the complete resolved texts.
`--dry-run` and `--print-config agent` also print a budget baseline calculated from configured `B` to standard error: the configured request cap,
fixed overhead, history and single-event targets, and the current Chat text and per-stream raw tool-output limits.
Use the prompt texts read for that operation, the model name, and tool definitions, following the
[Agent's sole budget rules](CHAT_MODEL_AGENT.md#context-byte-budget). No Channel IDs, tokens, or OpenEvent calls are needed.
Failure of the configuration itself to meet baseline requirements is a configuration error.
This is an offline preview that does not account for Model publication capacity. The effective runtime budget and compression target are
also constrained by OpenEvent's actual sending limits. A configured model-input cap larger than that capacity does not reject deployment,
and no new configuration setting is introduced.
Standard output for these two previews retains the YAML plan or configuration format. These derived values are only for preview, not new user settings or persistent state.

Generated `chat-model-agent.json` contains `agent.system_prompt`, `agent.context_compression_prompt`, and the context byte budget.
Agent reads only that generated configuration, without watching or rereading source prompt files. The effective configuration digest includes both texts
and the budget, rather than treating paths or modification times as content. Changed text at the same path makes `--start` require apply first;
a different path with identical text does not change the effective configuration. After successful initialization, changing only prompts or the budget
updates Agent configuration and restarts Agent under the existing rules, without restarting unchanged dependencies.
Texts frozen into prepared normal or compression requests remain fixed. Updates affect newly prepared requests, never retries or recovery of old inputs.
The new configuration must also pass [Agent compatibility checks for history and unfinished requests](CHAT_MODEL_AGENT.md#context-byte-budget).
Those checks distinguish configured-budget changes from current publication-capacity changes. Reduced sending capacity alone does not reject
recovery of the entire Agent; continuing execution follows the public Agent rules.

Compression and normal inference share the existing model, Provider, wait timeout, and Agent scheduling slots.
Only [Agent context byte budget](CHAT_MODEL_AGENT.md#context-byte-budget) defines the hard history target, single-event limit, and attempt allowance; Runtime adds no second set of options.

## Directory layout

Agent and Cmd Worker must run on the same host and access the same actual `work_root` through the same absolute path.
Identical path strings that resolve to different directories do not meet this prerequisite. Default session working directories are
`work_root/<channel_id>`, using numeric Chat Channel IDs; Agent gives Cmd the resolved absolute path.
Restarts and repeated apply operations do not change existing sessions' working paths.

The local stack uses the following layout with default paths. Configuration path overrides only relocate the corresponding data.

```text
openevent-stack/
├── stack.yaml             # User-supplied deployment entry point
├── prompts/               # Complete system/compression prompt files
└── runtime/               # Generated deployment files and runtime data
    ├── config/            # Component configuration, deployment state, credentials, and env.sh written during installation
    ├── data/openevent/    # Persistent OpenEvent data: message history, objects, and related data
    ├── logs/              # Service logs
    ├── run/               # Process PIDs
    ├── chat-sessions/     # Chat session configuration named <session_id>.json
    ├── cmd-output/        # Command stdout/stderr retained by Cmd
    └── workdir/           # Root of the Agent's default command working directories
        └── <channel_id>/  # Numeric Chat Channel ID, such as 4
            └── ...        # Files created or downloaded by commands
```

Within the runtime directory, `workdir/4/` holds local command files for Channel 4. Session configuration is in the sibling `chat-sessions/`, and OpenEvent stores chat history.
Renaming a session only changes its display name in the Chat session configuration; the numeric directory name stays the same.
A command with an explicit `workdir` uses that path. See the [Chat application guide](https://github.com/openevent-official/openevent-modules-chat/blob/main/docs/APP_USAGE.md)
for session-name storage and page operations.

The environment installation script's `--workdir runtime` selects `runtime/` under the project root: `runtime/venv/` contains the Python environment,
and `runtime/sources/` contains component checkouts and their build artifacts. This is separate from `openevent-stack/runtime/` and does not control the runtime data paths above.
See [environment setup](UBUNTU_ENVIRONMENT.md#preparation-and-installation) for the installation workspace.

## Tokens and resource bindings

For each identity, Runtime checks explicit tokens and then local secrets using `GetStatus`.
Only `UNAUTHENTICATED` proves the token invalid. Timeouts, network failures and permission errors fail apply without creating a replacement.
Runtime calls `AddToken` only when both sources are missing or explicitly invalid.
It reads `AddTokenResponse.token`, persists the new token, then validates it before passing it to components. It never calls `ListTokens`.
The local secrets file is updated only when an identity-to-token mapping changes; successful validation or reuse of the same credentials does not write it again.

All sessions share private Model, Cmd and WAL Channels. Members are respectively Agent with Model Proxy,
Agent with Cmd Worker, and Agent alone; their protocols are `llm.v1`, `cmd.v3` and `agent.wal.v5`.
The [WAL protocol](AGENT_WAL_PROTOCOL.md) defines the WAL rules.
Other Model/Cmd validation follows their public contracts. An explicit or already bound Cmd Channel can have an arbitrary description.
Model/Cmd/WAL resources automatically created by Runtime store a stable deployment_id in description metadata.
First deployment only requires the deployment configuration and `--apply`: Runtime starts OpenEvent, prepares tokens and auxiliary Channels, then starts the remaining components.
After validation, Runtime writes the same Cmd ID into Agent and cmd-worker configuration, then starts cmd-worker before Agent as described below.

Model/Cmd/WAL initialization uses explicit IDs first, otherwise stable metadata discovery. Multiple candidates fail; creation requires no candidates.
Automatic Cmd discovery only matches this deployment's metadata, not ordinary Cmd Channels with the same name or members.
All three IDs are saved as one group after validation; partial results cannot start Agent. Per-Channel pending markers
and partial IDs are not persisted for resuming initialization. After successful initialization, the three IDs are permanently
bound. Renames, additional sessions and temporary access failures cannot replace them.
Conflicting explicit IDs, missing resources and incompatible resources fail. A new deployment uses new state and WAL.

### Handling initialization failure

Initialization succeeds only when the first apply finishes starting every component and saves the successful state.
It involves local files, OpenEvent resources and multiple processes, rather than one transaction that can be rolled back as a unit.
When a step fails, previously created tokens, Channels, written data and started processes may still exist.
An entry-point error does not mean those actions did not happen or that already started business tasks have stopped.
Runtime stops further reconciliation and preserves existing records; it does not automatically delete data, roll back resources
or continue partial initialization. A preliminary validation failure before deployment state is generated can be retried directly after correcting the configuration.

Operations decides the next step from the actual processes, saved state and resources in OpenEvent. When data must be retained
or existing resources were adopted, operators can preserve the state for investigation and repair, or isolate the old deployment
and create an independent new one. This release has no automatic import or repair entry point for partial initialization;
retaining data alone does not enable another apply/start to resume. A new deployment uses new state and WAL, without arbitrarily
rebinding old session history to the new deployment. If the deployment and its resources are confirmed disposable, operators may
instead end the old deployment, stop the relevant processes, clean up and rebuild. Shutdown and publication handoff still follow
the prerequisite in the next section. Runtime does not decide resource ownership or the scope of deletion for operations.

When choosing cleanup, distinguish these consequences:

- Deleting only local state or configuration does not undo remote resources, committed events or command effects, and does not restore the deployment.
- Deleting OpenEvent storage removes its Channels, tokens, event history and objects. Explicit Channel IDs and tokens in the old configuration become invalid;
  retaining the original `stack.yaml` therefore does not guarantee an unchanged rerun. Prepare resources and credentials again before rebuilding,
  or change Channel settings that permit automatic creation to `auto`.
- The local stack generates `config/`, `data/`, `logs/`, `run/`, `workdir/`, `cmd-output/` and `chat-sessions/` under `openevent-stack/runtime/`;
  actual data can also reside at other configured paths. This is a list for locating data, not a requirement to delete every directory.
  `openevent-stack/runtime/config/env.sh` stores installation paths, and deployment scripts and the installed environment can be reused;
  check the entry configuration against the resources retained or rebuilt.

These boundaries concern failed first initialization only. Failed configuration updates after successful initialization
retain runtime data and continue according to the commit boundaries below.

## Startup, updates and shutdown

Deployment prerequisite: before a new Agent recovers, operations guarantees all OpenEvent publication RPCs issued by the old Agent have ended and cannot commit late.
Already submitted model or command business tasks need not have finished.

Apply runs in this order:

1. Validate the user spec and existing bindings, then generate and validate the OpenEvent target configuration independently; no Channel IDs or tokens are needed yet.
2. If dependencies need updating, stop Agent and wait for it to exit before changing any dependency's runtime configuration or process.
   When OpenEvent needs updating, also stop this deployment's Model Proxy, Chat Server, View and Cmd Worker, which use it.
   Then write the validated OpenEvent configuration, start or restart OpenEvent as needed, and wait for the business and admin ports in the target configuration.
   Restart decisions use the last fully committed configuration records and pending restarts in state; a file already containing target values does not prove the process uses them.
   OpenEvent keeps running when its configuration is unchanged, no restart is pending and its process is running; start it if it is not running.
3. Connect to the ready target addresses, validate or prepare tokens, and resolve and validate the three auxiliary Channels.
   With real IDs and valid tokens, generate and validate the remaining component configurations, then commit the complete configuration and remaining pending restarts.
4. Start or restart Model Proxy, Chat Server, View and Cmd Worker as needed, then start Agent last.

Changing OpenEvent business or admin ports therefore switches OpenEvent before connecting to the new addresses; apply never waits for new ports that have not yet been started.
Failure at any step stops reconciliation without starting later components or Agent, or reporting the complete apply as successful.
Operations chooses how to handle failed first initialization as described above. For updates to successfully initialized deployments,
if OpenEvent has switched but the full configuration has not been committed, preserve the state and rerun apply;
no automatic rollback is performed.
Agent recovers its history before dispatching new work. Cmd obtains attachment credentials from each run request and
can process submitted tasks independently, without an Agent credential lookup service or recovery/dispatch handshake files.
Process control status must report RUNNING; process status does not verify the complete model and tool workflows.
Local status scans reuse the expected configuration path within each scan while still checking each PID, process liveness, and command line.

Cmd Worker reads the object service address from `cmd_environment` in the generated deployment state `state.yaml`,
then sets `OPENEVENT_OBJECT_TARGET`. Failure to read this value fails startup.
Its PATH includes the deployment Python bin directory, making the installed `openevent-read-object` command available.
Default session working directories follow the deployment prerequisites and path rules in [Directory layout](#directory-layout).
Agent documentation defines the in-memory attachment mapping and command-result semantics.

After successful initialization, repeated apply restarts only changed components, components and Agent stopped for dependency updates, and continues existing pending restarts.
If a change becomes known only after resolving real resources and generating complete component configuration, Agent must still stop before committing dependency configuration and updating processes.
Agent resumes dispatch only after recovery, preventing new dispatch while dependencies are being updated.
Restarts still required after the full configuration commit remain in state `pending_restarts` until all subsequent processes have started successfully.
Apply or start resumes these items according to the commit boundary above, instead of treating updated files as proof that running processes use the new configuration.
`--start` neither resolves resources again nor changes configuration: first ensure OpenEvent is running with the committed
configuration and its target addresses are ready, then start the other dependencies and complete their pending restarts,
and finally start Agent. Removing OpenEvent from the pending restart list does not skip its running-status check.
Changes to the listening parameters or origin in Chat configuration, or to the worker object-reading environment, also restart their component.
New browser sessions do not trigger apply or change worker configuration. Unchanged running processes without pending restarts are not restarted.

Normal shutdown sends Agent SIGTERM, waits for it to finish draining and exit, then stops workers and other dependencies.
The local process script validates the PID before sending a signal and imposes no overall time limit while waiting for Agent to exit.
Other components have ten seconds to exit after SIGTERM; if still running, they receive SIGKILL, followed by up to
two more seconds to confirm exit. If a stop signal fails while the process is still alive, or another component's exit
remains unconfirmed after forced termination, the stop command fails and preserves the PID record.
Stack shutdown stops at that failure without closing dependencies; startup cleanup and restart likewise cannot launch
a replacement process after such a failure. Restart after forced termination still follows the operations prerequisite
above and Agent's history recovery.
Stopping only Agent does not make it send business cancellations; workers that remain running can continue processing submitted tasks.
After Cmd restarts, accepted commands without a final result receive
`RECOVERY_FAILED` and are not rerun, following [Cmd recovery rules](https://github.com/openevent-official/openevent-modules-cmd/blob/main/docs/CONFIGURATION.md#recovery-and-exit).
Agent alone defines failure freezing, cancellation, RPC retries and model history.

## Dependencies and validation

Deployment uses installed `openevent-sdk>=0.13.0` with `AddTokenResponse.token` and `batch_get_object_metadata`, plus Chat, `openevent-model-proxy>=0.1.1`,
`openevent-modules-cmd>=0.3.0,<0.4.0` (Cmd 0.3.x) and `openevent-view>=0.1.0` distributions.
View is a required dependency of this deployment and is declared by this package.
Chat SDK must support `get_turn_content`, `TurnWriter.reset`, restoring a writer from its start position, chain tail and effective content list,
as well as cancellation with `start_seq` and `serialize_payload`;
Model SDK must support streaming; Cmd SDK must support explicit publication UUIDs and attachment credentials through `object_keys`.
Equal version numbers do not prove equal capabilities: installation replaces each project package using its current successful build and checks actual public APIs.
SDK checkout paths are not added to imports. OpenEvent configuration is validated independently first; Chat, Model, Cmd, Agent and View parsers validate their generated configuration.

```bash
make test PYTHON=/absolute/path/to/python TEST_ARGS='-p test_reconcile_runtime.py'
make test PYTHON=/absolute/path/to/python TEST_ARGS='-p test_stack_scripts.py'
```

To measure local status-query time:

```bash
make benchmark-stack-status PYTHON=/absolute/path/to/python
```

This target runs three read-only queries each for `status chat-server` and `status all`, reports their median times, and saves results under `build/benchmarks/`. It does not start or stop services.

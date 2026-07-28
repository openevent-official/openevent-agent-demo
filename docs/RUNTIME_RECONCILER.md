# Runtime Reconciler

[中文版](RUNTIME_RECONCILER_cn.md)

Runtime Reconciler is the runtime-environment reconciliation script for the
Agent demo. It reads a declarative YAML file, generates runtime configuration
for OpenEvent, the IM P2P syncer, model-proxy, cmd-worker, and IM Model Agent,
and with `--apply` reconciles OpenEvent tokens, channels, and local processes.

It does not implement IM synchronization, model calls, or Agent business logic.
Those responsibilities remain in their respective components. Reconciler only
connects those components with one consistent set of principals, tokens,
channels, and configuration files.

The `view` section in `openevent-stack/stack.yaml` is consumed by the
`openevent-stack` scripts. Reconciler itself manages only the core
components. `openevent-view` is a required component of the Agent demo; it
depends on OpenEvent and is configured and started by `bootstrap.sh` /
`start.sh` after OpenEvent is running.

Local command support uses `openevent-modules-cmd` and `cmd-worker`. Reconciler
generates `cmd-worker` configuration, creates or reuses per-session `cmd.v1`
channels, and writes resolved cmd channel ids into Agent configuration.

## CLI

```bash
python3 scripts/reconcile_runtime.py --spec runtime.yaml --dry-run
python3 scripts/reconcile_runtime.py --spec runtime.yaml --apply
python3 scripts/reconcile_runtime.py --spec runtime.yaml --runtime-root openevent-stack --apply
python3 scripts/reconcile_runtime.py --spec runtime.yaml --print-config agent
```

Arguments:

| Argument | Description |
| --- | --- |
| `--spec` | Input YAML. Required. |
| `--dry-run` | Parse, fill defaults, generate a preview plan and configuration summary only. Does not write configuration, connect to OpenEvent, create tokens/channels, or restart processes. |
| `--apply` | Write configuration, start/restart OpenEvent, create or reuse tokens/channels, write final component configuration, and restart downstream components. |
| `--runtime-root` | Override `runtime.root` from the input YAML. Relative paths are resolved from the repository root. |
| `--print-config` | Print the dry-run parsed configuration for one component. One of `openevent`, `im_syncer`, `model_proxy`, `cmd_worker`, or `agent`. |

Exit codes:

| Exit Code | Meaning |
| --- | --- |
| `0` | Success. |
| `1` | Unclassified runtime error. |
| `2` | Input YAML validation failed. |
| `3` | Apply phase failed, for example OpenEvent is not ready or process startup failed. |

## Input Example

The example keeps common fields. Fields marked "no" in the field tables can be
deleted and will use defaults. `runtime.supervisor.ctl`, `view.port`, `tokens`,
`im.sync.*`, `model.provider_name`, `model.timeout_ms`, `agent.name`,
`agent.max_context_messages`, `agent.model_timeout_ms`,
`agent.max_model_attempts`,
`channels.visibility`, and
`sessions[].enabled` are all optional.

```yaml
version: v1

runtime:
  name: openevent-stack
  root: openevent-stack
  supervisor:
    ctl: ./openevent-stack/process.sh
    programs:
      openevent: openevent
      im_syncer: im-p2p-syncer
      model_proxy: model-proxy
      cmd_worker: cmd-worker
      agent: im-model-agent

openevent:
  grpc_addr: 127.0.0.1:9527
  admin_addr: 127.0.0.1:9528
  max_payload_bytes: 16777216
  storage:
    path: openevent-stack/data/openevent

view:
  port: 8080

principals:
  p_im_worker: 90001
  p_bot: 90002
  p_model_proxy: 20001
  p_cmd_worker: 30001
  p_user: 10001

tokens: {}

im:
  provider: lark
  session_type: p2p
  worker_principal: p_im_worker
  users:
    - principal: p_user
      user_email: user@example.com
      # Or use external_id/user_phone instead. --apply resolves email/phone to open_id.
      # external_id: ou_replace_with_lark_user_open_id
      # user_phone: "+8613800000000"
  bot:
    principal: p_bot
    app_id: cli_replace_with_lark_app_id
    app_secret: replace_with_lark_app_secret
  sync:
    history_retry_delay_ms: 1000
    history_overlap_ms: 300000
    history_lookback_ms: 300000
    page_size: 50
    event_queue_size: 1000

model:
  proxy_principal: p_model_proxy
  provider_name: openai_main
  base_url: https://api.openai.com
  api_key: replace_with_model_api_key
  model: gpt-4o-mini
  timeout_ms: 65000

cmd:
  worker_principal: p_cmd_worker
  max_concurrent_tasks: 8
  default_timeout_ms: 300000

agent:
  principal: p_bot
  name: im-model-agent
  system_prompt: "You are an assistant that talks with users through IM."
  max_context_messages: 20
  model_timeout_ms: 60000
  max_model_attempts: 3
  cmd_result_timeout_ms: 330000

channels:
  visibility: private

sessions:
  - session_id: s1
    enabled: true
    user:
      principal: p_user
    channels:
      im: openevent-stack.im.s1
      model: openevent-stack.llm.s1
      wal: openevent-stack.wal.s1
      cmd: openevent-stack.cmd.s1
```

## Input Fields

### Runtime

| Field | Required | Description |
| --- | --- | --- |
| `version` | yes | Must be `v1`. |
| `runtime.name` | yes | Runtime name. Written to normalized state and used in channel description metadata. |
| `runtime.root` | no | Runtime directory. Defaults to `runtime/<runtime.name>`; `--runtime-root` has highest priority. |
| `runtime.supervisor.ctl` | no | Process control command. Defaults to `supervisorctl`. `openevent-stack` uses the local `process.sh`. |
| `runtime.supervisor.programs.openevent` | yes | OpenEvent process name. |
| `runtime.supervisor.programs.im_syncer` | yes | IM P2P syncer process name. |
| `runtime.supervisor.programs.model_proxy` | yes | model-proxy process name. |
| `runtime.supervisor.programs.cmd_worker` | yes | cmd-worker process name. |
| `runtime.supervisor.programs.agent` | yes | IM Model Agent process name. |

### OpenEvent

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `openevent.grpc_addr` | yes | | Business gRPC address used by the SDK, IM syncer, model-proxy, and Agent. |
| `openevent.admin_addr` | yes | | Admin gRPC address used by Reconciler to call `ListTokens` and `AddToken`. |
| `openevent.storage.path` | yes | | Unified OpenEvent RocksDB directory. Metadata and messages use different Column Families in the same DB. Relative paths are resolved from the demo repository root. |
| `openevent.max_payload_bytes` | no | `16777216` | Written to OpenEvent server, model-proxy, and cmd-worker configuration. |

### Principals And Tokens

OpenEvent has no separate principal creation API. A principal is an integer
identity used in token bindings, channel members, and message publishers.
Reconciler does not allocate principals automatically; they must be declared
explicitly in `principals`.

`principals` is a global registry. The key is only a name inside this
configuration file and does not express a role. Each module references the
principals it needs by these names.

| Field | Required | Description |
| --- | --- | --- |
| `principals.<name>` | yes | OpenEvent principal integer. `<name>` can be freely chosen. |
| `tokens.<name>` | no | Token for `principals.<name>`. If omitted, `--apply` uses an existing token or creates a new one. |

`tokens.<name>` must refer to a name already declared in `principals`. If the
same user principal has both `tokens.<name>` and `im.users[].token`, the two
tokens must be identical.

Token reconciliation order:

1. If input `tokens.<name>` or `im.users[].token` is usable, use it directly.
2. If `config/secrets.yaml` already has a usable token, reuse it.
3. If OpenEvent `ListTokens` already has a usable token for the same principal,
   adopt it.
4. If no usable token exists, call
   `AdminService.AddToken(target_principal)` to create one.

Every created or adopted token is verified with `GetStatus(principal, token)`.
Business configuration needs plaintext tokens. `config/state.yaml` stores only
digests; `config/secrets.yaml` stores OpenEvent tokens that were automatically
created or adopted.

### IM

The current implementation supports Feishu/Lark P2P. Both use the same Lark
OpenAPI shape; the main difference is the open-platform domain and tenant
region.

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `im.provider` | no | `lark` | Supports `feishu` or `lark`. The example uses `lark`. |
| `im.session_type` | no | `p2p` | Currently must be `p2p`. |
| `im.worker_principal` | yes | | Name from `principals`; used as the IM P2P syncer worker principal. |
| `im.bot.principal` | yes | | Name from `principals`; used as the IM bot principal. It must resolve to the same principal as `agent.principal`. |
| `im.bot.app_id` | yes | | Feishu/Lark application ID. Also used to generate the bot mapping `external_user_id`. |
| `im.bot.app_secret` | yes | | Feishu/Lark application secret. Written to `im-p2p-syncer.yaml`. |
| `im.bot.api_base_url` | no | provider default | Not recommended in input YAML. Derived from `im.provider`: `https://open.feishu.cn` for `feishu`, `https://open.larksuite.com` for `lark`. |
| `im.sync.history_retry_delay_ms` | no | `1000` | Retry delay after a Provider history fetch failure. |
| `im.sync.history_overlap_ms` | no | `300000` | Overlap window for the history fetch watermark. |
| `im.sync.history_lookback_ms` | no | `300000` | History lookback window on first startup. |
| `im.sync.page_size` | no | `50` | Provider message page size. |
| `im.sync.event_queue_size` | no | `1000` | Provider live-event queue capacity. |

Feishu/Lark bot messages are mapped by application identity. Reconciler uses
`im.bot.app_id` as the bot mapping `external_user_id`.

`agent.principal` must resolve to the same principal as `im.bot.principal`. The
IM worker, model-proxy, user principal, and bot/agent principal must be distinct
from each other so one token does not gain unintended channel-operation
boundaries.

### Command Worker

Command support uses `openevent-modules-cmd`. Reconciler generates
`cmd-worker` configuration, creates or reuses per-session `cmd.v1` channels, and
writes the resolved cmd channel ids into Agent configuration.

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `cmd.worker_principal` | yes | | Name from `principals`; used as the cmd-worker principal. |
| `cmd.output_dir` | no | `<runtime.root>/data/cmd-worker-output` | Local output root for command stdout/stderr files. |
| `cmd.max_concurrent_tasks` | no | `8` | Maximum number of concurrent local command tasks handled by cmd-worker. |
| `cmd.default_timeout_ms` | no | `300000` | Default command timeout used when an Agent command request does not specify one. |

#### IM Users

`im.users[]` describes the relationship between Provider user identity and
OpenEvent principal.

| Field | Required | Description |
| --- | --- | --- |
| `im.users[].principal` | yes | Name from `principals`; this real user's OpenEvent principal. |
| `im.users[].token` | no | OpenEvent token for this user principal. It can also be written in the corresponding `tokens.<name>`. |
| `im.users[].external_id` | conditionally yes | Feishu/Lark user `open_id`. Exactly one of `external_id`, `user_phone`, or `user_email` is required. |
| `im.users[].user_phone` | conditionally yes | User phone number. Exactly one of `external_id`, `user_phone`, or `user_email` is required. Resolved to `open_id` through Contact API during `--apply`. |
| `im.users[].user_email` | conditionally yes | User email. Exactly one of `external_id`, `user_phone`, or `user_email` is required. Resolved to `open_id` through Contact API during `--apply`. |

If `user_phone` or `user_email` is used, Reconciler calls the Provider Contact
API, so `app_id/app_secret` must be able to access the corresponding user
information. `--dry-run` does not access the Provider and only generates a
placeholder external id.

#### IM Channel Resolution

IM channel does not need an extra configuration layer. Each session's
`sessions[].channels.im` is the OpenEvent IM channel name. To take over an
existing channel manually, configure `sessions[].channel_ids.im`.

Reconciler does not expose Provider `chat_id`. It finds the `im.users[]` entry
from each enabled session's `user.principal`, resolves the P2P conversation from
the user's open_id/phone/email, and writes it to the generated
`config/im-p2p-syncer.yaml`.

Do not confuse the generated provider `session_id` with the Agent
`sessions[].session_id`. The former is the Provider conversation ID; the latter
is the Agent's internal stable business key.

### Model

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `model.proxy_principal` | yes | | Name from `principals`; used as the model-proxy worker principal. |
| `model.provider_name` | no | `openai_main` | Written to model-proxy `default_provider`. |
| `model.base_url` | yes | | OpenAI-compatible provider base URL. Do not append `/v1/chat/completions`; the Agent request path is already `/v1/chat/completions`. |
| `model.api_key` | yes | | Model provider API key. Written to `model-proxy.yaml`. |
| `model.model` | yes | | Model name used by the Agent when constructing request bodies. It is not written to model-proxy provider configuration. |
| `model.timeout_ms` | no | `65000` | Total timeout for model-proxy calls to the provider. |

Generated model-proxy configuration explicitly allows only
`POST /v1/chat/completions` and `POST /v1/responses`. Requests to other provider
methods or paths are rejected before an HTTP request is sent.

### Agent

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `agent.principal` | yes | | Name from `principals`; used as the Agent principal. It must resolve to the same principal as `im.bot.principal`. |
| `agent.name` | no | `im-model-agent` | Agent name. |
| `agent.system_prompt` | yes | | Agent system prompt. |
| `agent.max_context_messages` | no | `20` | Context limit for prompts maintained by the Agent. |
| `agent.model_timeout_ms` | no | `60000` | How long the Agent waits for a model result. |
| `agent.max_model_attempts` | no | `3` | Maximum automatic attempts for one WAL; see the [Agent WAL protocol](AGENT_WAL_PROTOCOL.md) for retry and blocked semantics. |
| `agent.cmd_result_timeout_ms` | no | `330000` | How long the Agent waits for a Cmd result before reconciling history and supplying a failed tool result to the model. |

### Channels

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `channels.visibility` | no | `private` | Supports `protected` or `private`. `public` is currently disallowed because `llm.v1` channels cannot be public. |

Each enabled session has four channels.

| Channel | Protocol | Members |
| --- | --- | --- |
| IM | `im.v1` | user, agent bot, IM sync worker |
| Model | `llm.v1` | agent bot, model-proxy |
| WAL | `agent.wal.v1` | agent bot |
| Cmd | `cmd.v1` | agent bot, cmd-worker |

### Sessions

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `sessions[].session_id` | yes | | Stable business key for the Agent session. |
| `sessions[].enabled` | no | `true` | Whether this session should generate configuration and channels. At least one enabled session is required. |
| `sessions[].user.principal` | yes | | Must reference a name from `im.users[].principal`. |
| `sessions[].channels.im` | yes | | OpenEvent IM channel name. |
| `sessions[].channels.model` | no | `<runtime.name>.llm.<session_id>` | Model channel name. |
| `sessions[].channels.wal` | no | `<runtime.name>.wal.<session_id>` | WAL channel name. |
| `sessions[].channels.cmd` | no | `<runtime.name>.cmd.<session_id>` | Cmd channel name used by Agent local command calls. |
| `sessions[].channel_ids.im` | no | | Selected IM channel id for an uninitialized session; after initialization it must be omitted or equal the bound id. |
| `sessions[].channel_ids.model` | no | | Selected Model channel id for an uninitialized session; after initialization it must be omitted or equal the bound id. |
| `sessions[].channel_ids.wal` | no | | Selected WAL channel id for an uninitialized session; after initialization it must be omitted or equal the bound id. |
| `sessions[].channel_ids.cmd` | no | | Selected Cmd channel id for an uninitialized session; after initialization it must be omitted or equal the bound id. |

Within one Agent configuration, enabled sessions must have unique `model`,
`wal`, and `cmd` channel names. IM channel names must also be unique. Each
enabled session writes one IM channel id, one Model channel id, one WAL channel
id, and one Cmd channel id.

## Generated Files

By default, `--apply` writes generated configuration, state, plan, and secret
files under `config/` in the runtime root:

```text
config/openevent-server.yaml
config/im-p2p-syncer.yaml
config/model-proxy.yaml
config/cmd-worker.yaml
config/im-model-agent.yaml
config/desired.normalized.yaml
config/state.yaml
config/secrets.yaml
config/plan.yaml
data/openevent/
data/cmd-worker-output/
```

File descriptions:

| File | Description |
| --- | --- |
| `config/openevent-server.yaml` | OpenEvent server configuration, including gRPC/admin addresses and the unified RocksDB path from `openevent.storage.path`. |
| `config/im-p2p-syncer.yaml` | IM syncer configuration, including Feishu/Lark credentials, user/bot mappings, and principal tokens. |
| `config/model-proxy.yaml` | model-proxy configuration, including provider base URL/API key, OpenEvent token, channel ids, and request limits. |
| `config/cmd-worker.yaml` | cmd-worker configuration, including OpenEvent token, cmd channel ids, output directory, concurrency, and timeout settings. |
| `config/im-model-agent.yaml` | Agent configuration, including Agent token, model name, and IM/Model/WAL/Cmd channel ids. |
| `config/desired.normalized.yaml` | Desired state after defaults are expanded. Sensitive values are redacted. |
| `config/state.yaml` | Configuration digests, token digests, permanent channel bindings for every initialized session, and the latest apply status and phase. |
| `config/secrets.yaml` | Plaintext OpenEvent tokens that were automatically created or adopted. |
| `config/plan.yaml` | Actions and configuration summary for this apply. |

When a new runtime root is created, permissions are `0700`. Generated
configuration, state, secrets, and plan files are written with `0600`.
Configuration files contain OpenEvent tokens, Feishu/Lark app secrets, and model
API keys, and must not be committed.

## Apply Flow

`--apply` records five checkpoints in `config/state.yaml`:

| Phase | Completed work |
| --- | --- |
| `parsed` | The input YAML was parsed, defaults were filled, and fields were validated. |
| `openevent_ready` | `config/openevent-server.yaml` was written, OpenEvent was started or restarted when needed, and its business and admin ports became available. |
| `resources_resolved` | Provider identities and sessions, OpenEvent tokens, and IM/Model/Cmd/WAL channels were resolved or created. |
| `config_committed` | All downstream configurations were rendered and validated, then normalized desired state, component configurations, secrets, plan, and state were written. |
| `processes_running` | Changed downstream processes were restarted in dependency order, missing processes were started, and supervisor reported every configured program as `RUNNING`. |

`last_apply.status` is `in_progress`, `complete`, or `failed`. `last_apply.phase`
is the last completed checkpoint. On failure, `last_apply.failed_phase` identifies
the checkpoint that was being attempted. Timestamps record when the apply began,
was last updated, and completed or failed.

An apply is `complete` only at `processes_running`. This phase confirms a
supervisor observation, not end-to-end health: it does not prove that Provider
credentials, model requests, IM delivery, or the complete event path work.

Restart order:

```text
openevent -> model_proxy -> cmd_worker -> im_syncer -> agent
```

Agent starts last so IM syncer, model-proxy, and cmd-worker receive their
start/restart commands first. This ordering does not itself prove readiness.

## Token Reconciliation

Reconciler does not delete old tokens. It only uses, adopts, or creates tokens.

Input tokens are optional. In fully automatic mode, keep `tokens: {}` empty;
`--apply` creates tokens for the relevant principals through the OpenEvent Admin
API and writes them to `config/secrets.yaml`. Later runs prefer to reuse usable
tokens from `config/secrets.yaml`.

Token usage:

| Token | Used By |
| --- | --- |
| Principal referenced by `im.worker_principal` | `im-p2p-syncer.yaml.worker.token` |
| Principal referenced by `agent.principal` / `im.bot.principal` | `im-model-agent.yaml.agent.token`, IM syncer bot principal token, and the operator token Reconciler uses to create/maintain Agent channels |
| Principal referenced by `model.proxy_principal` | `model-proxy.yaml.token` |
| Principal referenced by `cmd.worker_principal` | `cmd-worker.yaml.token` |
| Principal referenced by `im.users[].principal` | IM syncer uses it to publish inbound `sync.record` as the user principal |

## Channel Reconciliation

`session_id` is the permanent binding key for the IM, Model, WAL, and Cmd
channels. A session becomes initialized after all four channels have been
resolved and validated and their complete mapping is written to
`config/state.yaml` during `config_committed`. The four ids are committed as one
set; a partial mapping does not initialize a session.

Channel reconciliation has two lifecycle paths:

1. Uninitialized: for each channel type, try the explicit id from the input
   YAML, a channel with the same name, and then a channel whose protocol and
   stable description business fields match. Create a channel only when no
   candidate is reusable. An explicit id is an instruction, not a hint: if it
   is missing, inaccessible, or incompatible, apply fails without falling back
   to discovery or creation.
2. Initialized: the four ids stored in `config/state.yaml` are the only
   authoritative mapping. Explicit ids may be omitted; when provided, they
   must equal the stored values. If any id differs or any bound channel is
   missing, inaccessible, or incompatible, apply fails. Reconciler does not
   discover or create a replacement.

Disabling a session, temporarily removing it from the desired configuration,
or later re-enabling it does not release the binding. `config/state.yaml` must
retain the complete mapping for every initialized session, not only currently
enabled sessions. Replacing any channel requires a new `session_id`; the
original `session_id` has no unbind or rebind operation.

Reusable conditions:

- `protocol` matches.
- `visibility` matches and is not forbidden by the protocol.
- `description` is valid JSON and stable business fields match.
- Channel creator must be the principal resolved from `agent.principal`.
- Required members already exist, or the `agent.principal` token is usable and
  can add missing members through `AddMember`.

When only required members are missing, Reconciler may add them under the
conditions above; this does not change channel identity. Reconciler does not
modify the `protocol`, `description`, `visibility`, or `creator` of old channels,
because the current OpenEvent API has no channel update operation. It also does
not delete old channels or historical events.

Stable description fields:

| Channel | Stable Fields |
| --- | --- |
| IM | `version=v1`, `provider`, `session_id`, `session_type`, `metadata.runtime_name`, `metadata.agent_session_id` |
| Model | `version=v1`, `metadata.runtime_name`, `metadata.agent_session_id`, `metadata.model_proxy_principal` |
| WAL | `version=v1`, `session_id`, `im_channel_id`, `model_channel_id`, `metadata.runtime_name` |
| Cmd | `version=v1`, `metadata.runtime_name`, `metadata.agent_session_id`, `metadata.cmd_worker_principal` |

WAL resolution depends on the final IM and Model channel ids because both are
stored in its description. Cmd has no ordering dependency on WAL; no stronger
creation order is part of the contract.

## Derived Configuration Notes

This table records only mappings that are not obvious from the input field
names. Each component's own documentation remains authoritative for its config
schema and runtime behavior.

| Component | Generated Contract |
| --- | --- |
| IM syncer | `worker.principal/token` comes from `im.worker_principal`. `principal_tokens` contains enabled users and the bot, not the worker. Every enabled session creates one user mapping and one bot mapping; user `external_user_id` is explicit or resolved from phone/email, while the bot uses `im.bot.app_id`. |
| model-proxy | Protocol is `llm.v1`; `channels` contains enabled Model channel ids. Provider config receives `model.base_url/api_key/timeout_ms`; `model.model` goes to Agent config instead. |
| cmd-worker | Protocol is `cmd.v1`; principal/token and enabled Cmd channel ids are resolved from the desired state. `output_dir` defaults to `<runtime_root>/data/cmd-worker-output`; Agent never executes shell commands directly. |
| Agent | Principal/token and all four resolved channel ids are generated per enabled session. There is no `from_seq`; Agent restores its position from OpenEvent history. |

## Troubleshooting

Common checks:

```bash
python3 scripts/reconcile_runtime.py --spec openevent-stack/stack.yaml --dry-run
python3 scripts/reconcile_runtime.py --spec openevent-stack/stack.yaml --print-config im_syncer
python3 scripts/reconcile_runtime.py --spec openevent-stack/stack.yaml --print-config model_proxy
python3 scripts/reconcile_runtime.py --spec openevent-stack/stack.yaml --print-config cmd_worker
python3 scripts/reconcile_runtime.py --spec openevent-stack/stack.yaml --print-config agent
```

The local `openevent-stack` runtime directory also provides:

```bash
./openevent-stack/bootstrap.sh --dry-run
./openevent-stack/bootstrap.sh --apply
./openevent-stack/status.sh
./openevent-stack/logs.sh model-proxy
./openevent-stack/logs.sh cmd-worker
```

Troubleshooting files:

| File | What To Check |
| --- | --- |
| `config/plan.yaml` | Token creation/adoption, channel creation/reuse, configuration writes, and restart actions for this run. |
| `config/state.yaml` | Current token and configuration digests, permanent channel bindings for initialized sessions, and `last_apply` status, completed phase, or failed phase. |
| `config/secrets.yaml` | OpenEvent tokens that were automatically created or adopted. Keep it secret. |
| `config/*.yaml` component configuration | Final real configuration passed to each component. |
| `logs/*.log` | Logs for components started by local `openevent-stack/process.sh`. |
| `workdir/` | Current working directory for processes started by `openevent-stack/process.sh`; also the default command working directory for Agent `exec` calls when `workdir` is omitted. |

Common errors:

| Error | Handling |
| --- | --- |
| `version must be v1` | Top-level `version` in the input YAML must be `v1`. |
| `principals.<name> must be a positive integer` | Principals must be explicitly configured as positive integers. |
| `im.users[] must provide external_id, user_phone, or user_email` | Each IM user must provide exactly one of Provider user `open_id`, phone, or email. |
| `sessions[].channels.im must be a string` | The session IM channel name must be a string. |
| `OpenEvent admin endpoint is not ready` | OpenEvent is not started, the port is wrong, the server binary path is wrong, or configuration is invalid. |
| `created token is not usable for principal ...` | Usually caused by multiple leftover OpenEvent processes on the same port, so Admin and business gRPC requests reach different instances. `openevent-stack/process.sh start openevent` cleans stale processes with the same configuration. |
| `supervisor start/restart failed` | Check that `runtime.supervisor.ctl` and `runtime.supervisor.programs.*` can start the corresponding processes. |

## Dependency Contracts

Generated configuration is validated with the config parsers installed in the
current Python environment. OpenEvent and each worker's public documentation
remain authoritative for their own APIs, protocols, and config schema;
Reconciler owns only the input-to-config mapping described here. Agent behavior
and WAL fields are defined locally by [IM_MODEL_AGENT.md](IM_MODEL_AGENT.md) and
[AGENT_WAL_PROTOCOL.md](AGENT_WAL_PROTOCOL.md).

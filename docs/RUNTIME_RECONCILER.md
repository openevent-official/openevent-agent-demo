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
`agent.max_model_attempts`, `agent.freeze_message`, `channels.visibility`, and
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
    metadata_path: openevent-stack/data/openevent/meta
  store:
    rocksdb:
      path: openevent-stack/data/openevent/messages

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
    interval_ms: 5000
    page_size: 50
    startup_lookback_ms: 300000

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
  freeze_message: "The model service is temporarily unavailable. The session is paused. Send another message to continue."

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
| `openevent.storage.metadata_path` | yes | | OpenEvent metadata storage path. Relative paths are resolved from the demo repository root. |
| `openevent.store.rocksdb.path` | yes | | OpenEvent message RocksDB storage path. Relative paths are resolved from the demo repository root. |
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
| `im.sync.interval_ms` | no | `5000` | IM polling interval. |
| `im.sync.page_size` | no | `50` | Provider message page size. |
| `im.sync.startup_lookback_ms` | no | `300000` | Lookback window for the first poll. |

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

### Agent

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `agent.principal` | yes | | Name from `principals`; used as the Agent principal. It must resolve to the same principal as `im.bot.principal`. |
| `agent.name` | no | `im-model-agent` | Agent name. |
| `agent.system_prompt` | yes | | Agent system prompt. |
| `agent.max_context_messages` | no | `20` | Context limit for prompts maintained by the Agent. |
| `agent.model_timeout_ms` | no | `60000` | How long the Agent waits for a model result. |
| `agent.max_model_attempts` | no | `3` | Maximum model attempts for the same turn. |
| `agent.freeze_message` | no | built-in English text | Freeze message written back to IM after repeated model attempt failures. |

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
| `sessions[].channel_ids.im` | no | | Manually specified existing IM channel id. |
| `sessions[].channel_ids.model` | no | | Manually specified existing Model channel id. |
| `sessions[].channel_ids.wal` | no | | Manually specified existing WAL channel id. |
| `sessions[].channel_ids.cmd` | no | | Manually specified existing Cmd channel id. |

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
data/openevent/meta/
data/openevent/messages/
data/cmd-worker-output/
```

File descriptions:

| File | Description |
| --- | --- |
| `config/openevent-server.yaml` | OpenEvent server configuration, including gRPC/admin addresses and data paths from `openevent.storage` / `openevent.store`. |
| `config/im-p2p-syncer.yaml` | IM syncer configuration, including Feishu/Lark credentials, user/bot mappings, and principal tokens. |
| `config/model-proxy.yaml` | model-proxy configuration, including provider base URL/API key, OpenEvent token, and response-header filtering switch. |
| `config/cmd-worker.yaml` | cmd-worker configuration, including OpenEvent token, cmd channel ids, output directory, concurrency, and timeout settings. |
| `config/im-model-agent.yaml` | Agent configuration, including Agent token, model name, and IM/Model/WAL/Cmd channel ids. |
| `config/desired.normalized.yaml` | Desired state after defaults are expanded. Sensitive values are redacted. |
| `config/state.yaml` | Configuration digests, token digests, channel ids, and apply status from the last apply. |
| `config/secrets.yaml` | Plaintext OpenEvent tokens that were automatically created or adopted. |
| `config/plan.yaml` | Actions and configuration summary for this apply. |

When a new runtime root is created, permissions are `0700`. Generated
configuration, state, secrets, and plan files are written with `0600`.
Configuration files contain OpenEvent tokens, Feishu/Lark app secrets, and model
API keys, and must not be committed.

## Apply Flow

The `--apply` order is:

1. Parse input YAML, fill defaults, and validate fields.
2. Generate and write `config/openevent-server.yaml`.
3. Restart OpenEvent if OpenEvent configuration changed.
4. Ensure the OpenEvent process is running and wait for business and admin ports
   to become available.
5. Resolve or create OpenEvent tokens.
6. Resolve or create IM, Model, Cmd, and WAL channels.
7. Generate IM syncer, model-proxy, cmd-worker, and Agent configuration.
8. Write `config/desired.normalized.yaml`, component configuration under
   `config/`, `config/state.yaml`, `config/secrets.yaml`, and
   `config/plan.yaml`.
9. Restart downstream processes whose configuration changed. If OpenEvent
   configuration changed, downstream processes are force-restarted.
10. Ensure model-proxy, cmd-worker, IM syncer, and Agent processes are in the running state.

Restart order:

```text
openevent -> model_proxy -> cmd_worker -> im_syncer -> agent
```

Agent starts last because it depends on IM syncer, model-proxy, and cmd-worker
channels being ready to consume.

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

Reconciler tries to reuse existing channels instead of creating new channels on
every run.

Lookup priority:

1. Channel id recorded in `config/state.yaml`.
2. Explicit channel id from the input YAML.
3. Channel with the same name from OpenEvent `ListChannels`.
4. Channel from OpenEvent `ListChannels` whose protocol and stable description
   business fields match.

Reusable conditions:

- `protocol` matches.
- `visibility` matches and is not forbidden by the protocol.
- `description` is valid JSON and stable business fields match.
- Channel creator must be the principal resolved from `agent.principal`.
- Required members already exist, or the `agent.principal` token is usable and
  can add missing members through `AddMember`.

If a channel is not reusable, Reconciler creates a new one. Reconciler does not
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

Creation order:

1. IM channel.
2. Model channel.
3. Cmd channel.
4. WAL channel.

WAL description needs the final IM/Model channel ids, so WAL must be created
last.

## Derived Configuration Notes

IM syncer:

- `worker.principal/token` uses the principal referenced by
  `im.worker_principal`.
- `principal_tokens` includes each active user and bot principal token; it does
  not include the worker itself.
- Each enabled session generates two active mappings:
  - `identity_type=user`, where `external_user_id` comes from the user's
    `external_id` or resolved phone/email.
  - `identity_type=bot`, where `external_user_id` uses `im.bot.app_id`.
- `openevent.publish.use_auto_seq` is fixed to `true`.
- Feishu/Lark sync mode is fixed to `poll`.

Model proxy:

- `protocol` is fixed to `llm.v1`.
- Request idempotency is handled in model-proxy process memory. During startup
  recovery, model-proxy rebuilds that state by scanning the OpenEvent log.
- `model.base_url/api_key/timeout_ms` are written to provider configuration.
- `model.model` is not written to the model-proxy provider configuration; it is
  written to Agent configuration and used by the Agent to construct request
  bodies.

Cmd worker:

- `protocol` is fixed to `cmd.v1`.
- `principal/token` uses the principal referenced by `cmd.worker_principal`.
- `channel_ids` contains the resolved cmd channel ids for enabled sessions.
- `output_dir` defaults to `<runtime_root>/data/cmd-worker-output`.
- The cmd worker process is a stack component; the Agent does not execute shell
  commands directly.

Agent:

- `agent.principal/token` uses the principal referenced by `agent.principal`.
- Each enabled session writes the final resolved `im_channel_id`,
  `model_channel_id`, `wal_channel_id`, and `cmd_channel_id`.
- Agent configuration does not contain `from_seq`; the Agent restores state from
  OpenEvent history itself, then continues consuming live messages.

## Limits And Boundaries

- Currently only `im.provider=feishu|lark` and `im.session_type=p2p` are
  supported.
- `channels.visibility=public` is currently disallowed.
- Reconciler does not allocate principals; it only creates tokens for declared
  principals.
- Reconciler does not delete tokens, channels, or historical events.
- Reconciler does not update immutable metadata of existing channels; when
  metadata drifts, it creates a new channel.
- Reconciler can prevent this configuration from generating multiple
  model-proxy consumers for the same Model channel, but it cannot enforce global
  mutual exclusion at the OpenEvent layer.
- Reconciler prevents this configuration from generating multiple cmd-worker
  consumers for the same Cmd channel, but it cannot enforce global mutual
  exclusion at the OpenEvent layer.
- `--dry-run` uses preview token/channel ids and does not represent real
  OpenEvent state.
- Generated configuration contains secrets; the runtime root should not be
  committed to git.

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
| `config/state.yaml` | Current runtime token digests, channel ids, and configuration digests. |
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

## Dependency Project Contracts

- OpenEvent API and token/channel behavior are defined by
  `openevent-sdk/docs/API.md`.
- OpenEvent server configuration is defined by `openevent/docs/CONFIG.md`.
- IM P2P syncer configuration is defined by
  `openevent-modules-im/docs/IM-P2P-SYNCER.md`.
- model-proxy configuration is defined by
  `openevent-modules-model-proxy/docs/CONFIGURATION.md`.
- Cmd protocol, worker configuration, and SDK behavior are defined by
  `openevent-modules-cmd/docs/CMD_PROTOCOL.md`,
  `openevent-modules-cmd/docs/CONFIGURATION.md`, and
  `openevent-modules-cmd/docs/SDK_USAGE.md`.
- Agent behavior is defined by [IM_MODEL_AGENT.md](IM_MODEL_AGENT.md).
- Agent WAL payload is defined by
  [AGENT_WAL_PROTOCOL.md](AGENT_WAL_PROTOCOL.md).

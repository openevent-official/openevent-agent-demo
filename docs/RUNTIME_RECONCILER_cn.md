# Runtime Reconciler

[English version](RUNTIME_RECONCILER.md)

Runtime Reconciler 是 Agent demo 的运行环境协调脚本。它读取一个声明式 YAML，生成
OpenEvent、IM P2P syncer、model-proxy、cmd-worker 和 IM Model Agent 的运行配置，并在
`--apply` 时协调 OpenEvent token、channel 和本地进程。

它不实现 IM 同步、模型调用或 Agent 业务逻辑；这些逻辑仍由各组件负责。Reconciler
只负责把这些组件按同一套 principal、token、channel 和配置文件连起来。

`openevent-stack/stack.yaml` 中的 `view` 段由 `openevent-stack` 脚本消费。
Reconciler 本身只管理核心组件；`openevent-view` 是 agent demo 的必须组件，
依赖 OpenEvent，由 `bootstrap.sh`/`start.sh` 在 OpenEvent 已运行后生成配置并启动。

本地命令能力使用 `openevent-modules-cmd` 和 `cmd-worker`。Reconciler 会生成
`cmd-worker` 配置，创建或复用每个 session 的 `cmd.v1` channel，并把解析出的 cmd channel id
写入 Agent 配置。

## CLI

```bash
python3 scripts/reconcile_runtime.py --spec runtime.yaml --dry-run
python3 scripts/reconcile_runtime.py --spec runtime.yaml --apply
python3 scripts/reconcile_runtime.py --spec runtime.yaml --runtime-root openevent-stack --apply
python3 scripts/reconcile_runtime.py --spec runtime.yaml --print-config agent
```

参数：

| 参数 | 说明 |
| --- | --- |
| `--spec` | 输入 YAML。必填。 |
| `--dry-run` | 只解析、填默认值、生成预览 plan 和配置摘要；不写配置、不连 OpenEvent、不创建 token/channel、不重启进程。 |
| `--apply` | 写配置、启动/重启 OpenEvent、创建或复用 token/channel、写最终组件配置、重启下游组件。 |
| `--runtime-root` | 覆盖输入 YAML 中的 `runtime.root`。相对路径按仓库根目录解析。 |
| `--print-config` | 打印 dry-run 解析后的单个组件配置，可选 `openevent`、`im_syncer`、`model_proxy`、`cmd_worker`、`agent`。 |

退出码：

| 退出码 | 含义 |
| --- | --- |
| `0` | 成功。 |
| `1` | 未分类运行错误。 |
| `2` | 输入 YAML 校验失败。 |
| `3` | apply 阶段失败，例如 OpenEvent 未就绪或进程启动失败。 |

## 输入示例

示例里保留的是常用配置；字段表中标为“否”的字段可以删掉并使用默认值。
`runtime.supervisor.ctl`、`view.port`、`tokens`、`im.sync.*`、`model.provider_name`、
`model.timeout_ms`、`agent.name`、`agent.max_context_messages`、`agent.model_timeout_ms`、
`agent.max_model_attempts`、`agent.freeze_message`、`channels.visibility` 和
`sessions[].enabled` 都不是必填项。

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
  system_prompt: "你是一个通过 IM 与用户对话的助手。"
  max_context_messages: 20
  model_timeout_ms: 60000
  max_model_attempts: 3
  freeze_message: "模型服务暂时没有响应，会话已暂停。请再发送一条消息以继续。"

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

## 输入字段

### Runtime

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `version` | 是 | 必须为 `v1`。 |
| `runtime.name` | 是 | 运行环境名。写入 normalized state，也用于 channel description metadata。 |
| `runtime.root` | 否 | 运行目录。默认 `runtime/<runtime.name>`；`--runtime-root` 优先级最高。 |
| `runtime.supervisor.ctl` | 否 | 进程控制命令。默认 `supervisorctl`。`openevent-stack` 使用本地 `process.sh`。 |
| `runtime.supervisor.programs.openevent` | 是 | OpenEvent 进程名。 |
| `runtime.supervisor.programs.im_syncer` | 是 | IM P2P syncer 进程名。 |
| `runtime.supervisor.programs.model_proxy` | 是 | model-proxy 进程名。 |
| `runtime.supervisor.programs.cmd_worker` | 是 | cmd-worker 进程名。 |
| `runtime.supervisor.programs.agent` | 是 | IM Model Agent 进程名。 |

### OpenEvent

| 字段 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- |
| `openevent.grpc_addr` | 是 | | 业务 gRPC 地址，给 SDK、IM syncer、model-proxy 和 Agent 使用。 |
| `openevent.admin_addr` | 是 | | 管理 gRPC 地址，Reconciler 用它调用 `ListTokens` 和 `AddToken`。 |
| `openevent.storage.metadata_path` | 是 | | OpenEvent metadata 存储路径。相对路径按 demo 仓库根目录解析。 |
| `openevent.store.rocksdb.path` | 是 | | OpenEvent 消息 RocksDB 存储路径。相对路径按 demo 仓库根目录解析。 |
| `openevent.max_payload_bytes` | 否 | `16777216` | 写入 OpenEvent server、model-proxy 和 cmd-worker 配置。 |

### Principals And Tokens

OpenEvent 没有单独的 principal 创建接口。principal 是 token 绑定、channel 成员和消息发布者里的整数身份。
Reconciler 不会自动分配 principal；必须在 `principals` 中显式声明。

`principals` 是全局注册表，key 只是本配置文件内的名字，不表达功能。各模块需要的
principal 在自己的配置区域里用这些名字引用。

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `principals.<name>` | 是 | OpenEvent principal 整数。`<name>` 可自由命名。 |
| `tokens.<name>` | 否 | 对应 `principals.<name>` 的 token。省略时 `--apply` 会采用已有 token 或创建新 token。 |

`tokens.<name>` 的 `<name>` 必须已经在 `principals` 中声明。
如果同一个用户 principal 同时配置了 `tokens.<name>` 和 `im.users[].token`，两个 token 必须一致。

token 协调顺序：

1. 如果输入 `tokens.<name>` 或 `im.users[].token` 可用，直接使用。
2. 如果 `config/secrets.yaml` 中已有 token 且仍可用，继续使用。
3. 如果 OpenEvent `ListTokens` 中已有同 principal 的可用 token，采用它。
4. 如果没有可用 token，调用 `AdminService.AddToken(target_principal)` 创建。

所有 token 创建或采用后都会用 `GetStatus(principal, token)` 校验。业务配置中需要明文 token；
`config/state.yaml` 只保存摘要，`config/secrets.yaml` 保存自动创建或采用的 OpenEvent token。

### IM

当前实现支持 Feishu/Lark P2P。两者使用同一套 Lark OpenAPI 形态，主要差异是开放平台域名和应用所属租户区域。

| 字段 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- |
| `im.provider` | 否 | `lark` | 支持 `feishu` 或 `lark`。示例使用 `lark`。 |
| `im.session_type` | 否 | `p2p` | 目前必须是 `p2p`。 |
| `im.worker_principal` | 是 | | 引用 `principals` 中的名字，作为 IM P2P syncer worker principal。 |
| `im.bot.principal` | 是 | | 引用 `principals` 中的名字，作为 IM bot principal；必须和 `agent.principal` 解析到同一个 principal。 |
| `im.bot.app_id` | 是 | | Feishu/Lark 应用 ID。也用于生成 bot mapping 的 `external_user_id`。 |
| `im.bot.app_secret` | 是 | | Feishu/Lark 应用密钥。写入 `im-p2p-syncer.yaml`。 |
| `im.bot.api_base_url` | 否 | provider 默认值 | 不建议在输入 YAML 暴露。由 `im.provider` 推导：`feishu` 为 `https://open.feishu.cn`，`lark` 为 `https://open.larksuite.com`。 |
| `im.sync.interval_ms` | 否 | `5000` | IM 轮询间隔。 |
| `im.sync.page_size` | 否 | `50` | Provider 消息分页大小。 |
| `im.sync.startup_lookback_ms` | 否 | `300000` | 首次轮询回看窗口。 |

Feishu/Lark bot 消息按应用身份映射。Reconciler 使用 `im.bot.app_id`
作为 bot mapping 的 `external_user_id`。

`agent.principal` 必须和 `im.bot.principal` 解析到同一个 principal。IM worker、
model-proxy、用户 principal 和 bot/agent principal 必须彼此区分，避免一个 token 获得不该有的 channel 操作边界。

### Command Worker

命令能力使用 `openevent-modules-cmd`。Reconciler 会生成 `cmd-worker` 配置，
创建或复用每个 session 的 `cmd.v1` channel，并把解析出的 cmd channel id 写入 Agent 配置。

| 字段 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- |
| `cmd.worker_principal` | 是 | | 引用 `principals` 中的名字，作为 cmd-worker principal。 |
| `cmd.output_dir` | 否 | `<runtime.root>/data/cmd-worker-output` | 本地 stdout/stderr 输出根目录。 |
| `cmd.max_concurrent_tasks` | 否 | `8` | cmd-worker 同时处理的本地命令任务上限。 |
| `cmd.default_timeout_ms` | 否 | `300000` | Agent 命令请求未指定 timeout 时使用的默认命令超时。 |

#### IM Users

`im.users[]` 描述 Provider 用户身份和 OpenEvent principal 的关系。

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `im.users[].principal` | 是 | 引用 `principals` 中的名字，表示这个真实用户在 OpenEvent 里的 principal。 |
| `im.users[].token` | 否 | 这个用户 principal 的 OpenEvent token；也可以统一写在对应的 `tokens.<name>` 中。 |
| `im.users[].external_id` | 条件必填 | Feishu/Lark 用户 `open_id`。与 `user_phone`、`user_email` 三选一。 |
| `im.users[].user_phone` | 条件必填 | 用户手机号。与 `external_id`、`user_email` 三选一；`--apply` 时通过 Contact API 解析为 `open_id`。 |
| `im.users[].user_email` | 条件必填 | 用户邮箱。与 `external_id`、`user_phone` 三选一；`--apply` 时通过 Contact API 解析为 `open_id`。 |

如果使用 `user_phone` 或 `user_email`，Reconciler 会调用 Provider Contact API，所以 `app_id/app_secret`
必须能访问对应用户信息。`--dry-run` 不会访问 Provider，只生成占位 external id。

#### IM Channel Resolution

IM channel 不需要单独配置一层。每个 session 的 `sessions[].channels.im`
就是 OpenEvent IM channel 名称；如果要人工接管已有 channel，可以配置
`sessions[].channel_ids.im`。

Reconciler 不暴露 Provider `chat_id`。它会根据 enabled session 的 `user.principal`
找到 `im.users[]`，再由用户 open_id/手机号/邮箱解析出 P2P 会话，最终写入生成后的
`config/im-p2p-syncer.yaml`。

不要把生成配置里的 provider `session_id` 和 Agent 的 `sessions[].session_id` 混用。前者是 Provider 会话 ID，后者只是 Agent 内部稳定业务键。

### Model

| 字段 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- |
| `model.proxy_principal` | 是 | | 引用 `principals` 中的名字，作为 model-proxy worker principal。 |
| `model.provider_name` | 否 | `openai_main` | 写入 model-proxy `default_provider`。 |
| `model.base_url` | 是 | | OpenAI-compatible provider 根地址。不要把 `/v1/chat/completions` 拼进去；Agent 请求 path 已经是 `/v1/chat/completions`。 |
| `model.api_key` | 是 | | 模型 provider API key。写入 `model-proxy.yaml`。 |
| `model.model` | 是 | | Agent 构造模型请求时使用的模型名。不会写入 model-proxy provider 配置。 |
| `model.timeout_ms` | 否 | `65000` | model-proxy 调 provider 的总超时。 |

### Agent

| 字段 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- |
| `agent.principal` | 是 | | 引用 `principals` 中的名字，作为 Agent principal；必须和 `im.bot.principal` 解析到同一个 principal。 |
| `agent.name` | 否 | `im-model-agent` | Agent 名称。 |
| `agent.system_prompt` | 是 | | Agent system prompt。 |
| `agent.max_context_messages` | 否 | `20` | Agent 自维护 prompt 的上下文上限。 |
| `agent.model_timeout_ms` | 否 | `60000` | Agent 等待模型结果的超时时间。 |
| `agent.max_model_attempts` | 否 | `3` | 同一 turn 的最大模型尝试次数。 |
| `agent.freeze_message` | 否 | 内置英文文案 | 模型 attempt 多次失败后写回 IM 的冻结提示。 |

### Channels

| 字段 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- |
| `channels.visibility` | 否 | `private` | 支持 `protected` 或 `private`。当前不允许 `public`，因为 `llm.v1` channel 不能公开。 |

每个 enabled session 有四个 channel。

| Channel | Protocol | 成员 |
| --- | --- | --- |
| IM | `im.v1` | user、agent bot、IM sync worker |
| Model | `llm.v1` | agent bot、model-proxy |
| WAL | `agent.wal.v1` | agent bot |
| Cmd | `cmd.v1` | agent bot、cmd-worker |

### Sessions

| 字段 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- |
| `sessions[].session_id` | 是 | | Agent session 的稳定业务键。 |
| `sessions[].enabled` | 否 | `true` | 是否生成该 session 的配置和 channel。至少需要一个 enabled session。 |
| `sessions[].user.principal` | 是 | | 必须引用 `im.users[].principal` 的名字。 |
| `sessions[].channels.im` | 是 | | OpenEvent IM channel 名称。 |
| `sessions[].channels.model` | 否 | `<runtime.name>.llm.<session_id>` | Model channel 名称。 |
| `sessions[].channels.wal` | 否 | `<runtime.name>.wal.<session_id>` | WAL channel 名称。 |
| `sessions[].channels.cmd` | 否 | `<runtime.name>.cmd.<session_id>` | Agent 本地命令调用使用的 Cmd channel 名称。 |
| `sessions[].channel_ids.im` | 否 | | 人工指定已有 IM channel id。 |
| `sessions[].channel_ids.model` | 否 | | 人工指定已有 Model channel id。 |
| `sessions[].channel_ids.wal` | 否 | | 人工指定已有 WAL channel id。 |
| `sessions[].channel_ids.cmd` | 否 | | 人工指定已有 Cmd channel id。 |

同一份 Agent 配置中，enabled session 的 `model`、`wal` 和 `cmd` channel name 必须唯一。
IM channel name 也必须唯一。每个 enabled session 最终会写入一个 IM channel id、
一个 Model channel id、一个 WAL channel id 和一个 Cmd channel id。

## 生成文件

`--apply` 默认把生成配置、状态、计划和密钥文件都写入 runtime root 下的 `config/`：

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

文件说明：

| 文件 | 说明 |
| --- | --- |
| `config/openevent-server.yaml` | OpenEvent server 配置，包含 gRPC/admin 地址和 `openevent.storage`/`openevent.store` 指定的数据路径。 |
| `config/im-p2p-syncer.yaml` | IM syncer 配置，包含 Feishu/Lark 凭据、用户/bot mappings、principal tokens。 |
| `config/model-proxy.yaml` | model-proxy 配置，包含 provider base URL/API key、OpenEvent token、响应 header 过滤开关。 |
| `config/cmd-worker.yaml` | cmd-worker 配置，包含 OpenEvent token、cmd channel ids、输出目录、并发和超时设置。 |
| `config/im-model-agent.yaml` | Agent 配置，包含 Agent token、模型名、IM/Model/WAL/Cmd channel ids。 |
| `config/desired.normalized.yaml` | 展开默认值后的目标状态。敏感值会被 redacted。 |
| `config/state.yaml` | 上次 apply 的配置摘要、token 摘要、channel id 和 apply 状态。 |
| `config/secrets.yaml` | 自动创建或采用的 OpenEvent 明文 token。 |
| `config/plan.yaml` | 本次 apply 的 actions 和配置摘要。 |

新建 runtime root 时权限按 `0700` 创建；生成配置和 state/secrets/plan 按 `0600` 写入。
配置文件包含 OpenEvent token、Feishu/Lark app secret 和模型 API key，不能提交。

## Apply 流程

`--apply` 顺序是：

1. 解析输入 YAML，填默认值，校验字段。
2. 生成并写入 `config/openevent-server.yaml`。
3. 如果 OpenEvent 配置变化，重启 OpenEvent。
4. 确保 OpenEvent 进程已运行，并等待业务端口和管理端口可用。
5. 解析或创建 OpenEvent token。
6. 解析或创建 IM、Model、Cmd、WAL channel。
7. 生成 IM syncer、model-proxy、cmd-worker 和 Agent 配置。
8. 写入 `config/desired.normalized.yaml`、`config/` 下的组件配置、`config/state.yaml`、`config/secrets.yaml`、`config/plan.yaml`。
9. 对配置变化的下游进程执行 restart；如果 OpenEvent 配置变化，会强制重启下游进程。
10. 确保 model-proxy、cmd-worker、IM syncer 和 Agent 进程处于 running 状态。

重启顺序：

```text
openevent -> model_proxy -> cmd_worker -> im_syncer -> agent
```

Agent 最后启动，因为它依赖 IM syncer、model-proxy 和 cmd-worker 的 channel 已经可消费。

## Token 协调

Reconciler 不删除旧 token。它只使用、采用或创建 token。

输入 token 可选。全自动模式下，可以把 `tokens: {}` 留空；`--apply` 会通过 OpenEvent
Admin API 为相关 principal 创建 token，并写入 `config/secrets.yaml`。后续再次运行时会优先复用
`config/secrets.yaml` 里的可用 token。

token 使用位置：

| Token | 使用位置 |
| --- | --- |
| `im.worker_principal` 引用的 principal | `im-p2p-syncer.yaml.worker.token` |
| `agent.principal` / `im.bot.principal` 引用的 principal | `im-model-agent.yaml.agent.token`、IM syncer bot principal token，以及 Reconciler 创建/维护该 Agent channel 的 operator token |
| `model.proxy_principal` 引用的 principal | `model-proxy.yaml.token` |
| `cmd.worker_principal` 引用的 principal | `cmd-worker.yaml.token` |
| `im.users[].principal` 引用的 principal | IM syncer 用它以用户 principal 发布入站 `sync.record` |

## Channel 协调

Reconciler 会尽量复用已有 channel，而不是每次创建新 channel。

查找优先级：

1. `config/state.yaml` 中记录的 channel id。
2. 输入 YAML 中显式写的 channel id。
3. OpenEvent `ListChannels` 中 name 相同的 channel。
4. OpenEvent `ListChannels` 中 protocol 和 description 稳定业务字段匹配的 channel。

可复用条件：

- `protocol` 匹配。
- `visibility` 匹配且不是协议禁止值。
- `description` 是合法 JSON，且稳定业务字段匹配。
- channel creator 必须是 `agent.principal` 解析出的 principal。
- 必需成员已经存在，或 `agent.principal` 的 token 可用且能通过 `AddMember` 补齐。

不可复用时会创建新 channel。Reconciler 不会修改旧 channel 的 `protocol`、`description`、
`visibility` 或 `creator`，因为当前 OpenEvent API 没有 channel update。它也不会删除旧
channel 或历史事件。

description 稳定字段：

| Channel | 稳定字段 |
| --- | --- |
| IM | `version=v1`、`provider`、`session_id`、`session_type`、`metadata.runtime_name`、`metadata.agent_session_id` |
| Model | `version=v1`、`metadata.runtime_name`、`metadata.agent_session_id`、`metadata.model_proxy_principal` |
| WAL | `version=v1`、`session_id`、`im_channel_id`、`model_channel_id`、`metadata.runtime_name` |
| Cmd | `version=v1`、`metadata.runtime_name`、`metadata.agent_session_id`、`metadata.cmd_worker_principal` |

创建顺序：

1. IM channel。
2. Model channel。
3. Cmd channel。
4. WAL channel。

WAL description 需要最终 IM/Model channel id，所以 WAL 必须最后创建。

## 派生配置要点

IM syncer：

- `worker.principal/token` 使用 `im.worker_principal` 引用的 principal。
- `principal_tokens` 包含每个 active user 和 bot principal 的 token；不包含 worker 自己。
- 每个 enabled session 生成两条 active mapping：
  - `identity_type=user`，`external_user_id` 来自用户 `external_id` 或手机号/邮箱解析结果。
  - `identity_type=bot`，`external_user_id` 使用 `im.bot.app_id`。
- `openevent.publish.use_auto_seq` 固定为 `true`。
- Feishu/Lark sync mode 固定为 `poll`。

Model proxy：

- `protocol` 固定为 `llm.v1`。
- 请求幂等由 model-proxy 进程内内存处理；启动恢复时，model-proxy 通过扫描 OpenEvent 日志重建该状态。
- `model.base_url/api_key/timeout_ms` 写入 provider 配置。
- `model.model` 不写入 model-proxy provider；它写入 Agent 配置，由 Agent 构造请求 body。

Cmd worker：

- `protocol` 固定为 `cmd.v1`。
- `principal/token` 使用 `cmd.worker_principal` 引用的 principal。
- `channel_ids` 包含 enabled sessions 最终解析出的 cmd channel ids。
- `output_dir` 默认写到 `<runtime_root>/data/cmd-worker-output`。
- cmd worker 是 stack 组件；Agent 不直接执行 shell 命令。

Agent：

- `agent.principal/token` 使用 `agent.principal` 引用的 principal。
- 每个 enabled session 写入最终解析出的 `im_channel_id`、`model_channel_id` 和
  `wal_channel_id`、`cmd_channel_id`。
- Agent 配置不包含 `from_seq`；Agent 自己从 OpenEvent 历史恢复状态，再继续实时消费。

## 限制和边界

- 当前只支持 `im.provider=feishu|lark` 和 `im.session_type=p2p`。
- 当前不允许 `channels.visibility=public`。
- Reconciler 不分配 principal，只为已声明 principal 创建 token。
- Reconciler 不删除 token、channel 或历史事件。
- Reconciler 不更新已有 channel 的 immutable metadata；metadata 漂移时创建新 channel。
- Reconciler 只能防止本配置生成多个 model-proxy 消费同一个 Model channel；不能从 OpenEvent 层强制全局互斥。
- Reconciler 会防止本配置生成多个 cmd-worker 消费同一个 Cmd channel；但不能从 OpenEvent 层强制全局互斥。
- `--dry-run` 使用预览 token/channel id，不代表真实 OpenEvent 状态。
- 生成配置包含密钥；runtime root 不应该提交到 git。

## 排错

常用检查：

```bash
python3 scripts/reconcile_runtime.py --spec openevent-stack/stack.yaml --dry-run
python3 scripts/reconcile_runtime.py --spec openevent-stack/stack.yaml --print-config im_syncer
python3 scripts/reconcile_runtime.py --spec openevent-stack/stack.yaml --print-config model_proxy
python3 scripts/reconcile_runtime.py --spec openevent-stack/stack.yaml --print-config cmd_worker
python3 scripts/reconcile_runtime.py --spec openevent-stack/stack.yaml --print-config agent
```

`openevent-stack` 本地运行目录还提供：

```bash
./openevent-stack/bootstrap.sh --dry-run
./openevent-stack/bootstrap.sh --apply
./openevent-stack/status.sh
./openevent-stack/logs.sh model-proxy
./openevent-stack/logs.sh cmd-worker
```

排错文件：

| 文件 | 看什么 |
| --- | --- |
| `config/plan.yaml` | 本次创建/采用 token、创建/复用 channel、写配置和重启动作。 |
| `config/state.yaml` | 当前 runtime 记录的 token 摘要、channel id 和配置摘要。 |
| `config/secrets.yaml` | 自动创建或采用的 OpenEvent token。注意保密。 |
| `config/*.yaml` 组件配置 | 最终传给各组件的真实配置。 |
| `logs/*.log` | 本地 `openevent-stack/process.sh` 启动的组件日志。 |
| `workdir/` | 本地 `openevent-stack/process.sh` 启动组件时使用的当前工作目录；Agent `exec` tool call 未传 `workdir` 时也默认在这里执行命令。 |

常见错误：

| 错误 | 处理 |
| --- | --- |
| `version must be v1` | 输入 YAML 顶层 `version` 必须是 `v1`。 |
| `principals.<name> must be a positive integer` | principal 必须显式配置为正整数。 |
| `im.users[] must provide external_id, user_phone, or user_email` | 每个 IM 用户必须提供 Provider 用户 `open_id`、手机号或邮箱三者之一。 |
| `sessions[].channels.im must be a string` | session 的 IM channel 名称必须是字符串。 |
| `OpenEvent admin endpoint is not ready` | OpenEvent 没启动、端口不对、server binary 路径不对，或配置写错。 |
| `created token is not usable for principal ...` | 通常是同一端口上残留了多个 OpenEvent 进程，Admin 和业务 gRPC 请求落到了不同实例；`openevent-stack/process.sh start openevent` 会清理同配置的陈旧进程。 |
| `supervisor start/restart failed` | 检查 `runtime.supervisor.ctl` 和 `runtime.supervisor.programs.*` 是否能启动对应进程。 |

## 依赖项目契约

- OpenEvent API 和 token/channel 行为以 `openevent-sdk/docs/API.md` 为准。
- OpenEvent server 配置以 `openevent/docs/CONFIG.md` 为准。
- IM P2P syncer 配置以 `openevent-modules-im/docs/IM-P2P-SYNCER.md` 为准。
- model-proxy 配置以 `openevent-modules-model-proxy/docs/CONFIGURATION.md` 为准。
- Cmd 协议、worker 配置和 SDK 行为以 `openevent-modules-cmd/docs/CMD_PROTOCOL.md`、
  `openevent-modules-cmd/docs/CONFIGURATION.md` 和
  `openevent-modules-cmd/docs/SDK_USAGE.md` 为准。
- Agent 行为以 [IM_MODEL_AGENT_cn.md](IM_MODEL_AGENT_cn.md) 为准。
- Agent WAL payload 以 [AGENT_WAL_PROTOCOL_cn.md](AGENT_WAL_PROTOCOL_cn.md) 为准。

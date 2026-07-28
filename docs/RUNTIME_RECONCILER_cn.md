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
`agent.max_model_attempts`、
`channels.visibility` 和
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
  system_prompt: "你是一个通过 IM 与用户对话的助手。"
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
| `openevent.storage.path` | 是 | | OpenEvent 统一 RocksDB 数据目录；metadata 和消息使用同一 DB 的不同 Column Family。相对路径按 demo 仓库根目录解析。 |
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
| `im.sync.history_retry_delay_ms` | 否 | `1000` | Provider 历史拉取失败后的重试间隔。 |
| `im.sync.history_overlap_ms` | 否 | `300000` | 历史拉取水位的重叠窗口。 |
| `im.sync.history_lookback_ms` | 否 | `300000` | 首次启动的历史回看窗口。 |
| `im.sync.page_size` | 否 | `50` | Provider 消息分页大小。 |
| `im.sync.event_queue_size` | 否 | `1000` | Provider 实时事件队列容量。 |

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

生成的 model-proxy 配置只显式允许 `POST /v1/chat/completions` 和
`POST /v1/responses`。其他 Provider method 或 path 会在发送 HTTP 请求前被拒绝。

### Agent

| 字段 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- |
| `agent.principal` | 是 | | 引用 `principals` 中的名字，作为 Agent principal；必须和 `im.bot.principal` 解析到同一个 principal。 |
| `agent.name` | 否 | `im-model-agent` | Agent 名称。 |
| `agent.system_prompt` | 是 | | Agent system prompt。 |
| `agent.max_context_messages` | 否 | `20` | Agent 自维护 prompt 的上下文上限。 |
| `agent.model_timeout_ms` | 否 | `60000` | Agent 等待模型结果的超时时间。 |
| `agent.max_model_attempts` | 否 | `3` | 同一 WAL 的最大自动尝试次数；具体 retry 和 blocked 语义见 [Agent WAL 协议](AGENT_WAL_PROTOCOL_cn.md)。 |
| `agent.cmd_result_timeout_ms` | 否 | `330000` | Agent 等待 Cmd result 的时间；超时并完成历史对账后向模型提供失败工具结果。 |

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
| `sessions[].channel_ids.im` | 否 | | 未初始化 session 的指定 IM channel id；初始化后只能省略或与已绑定 id 相同。 |
| `sessions[].channel_ids.model` | 否 | | 未初始化 session 的指定 Model channel id；初始化后只能省略或与已绑定 id 相同。 |
| `sessions[].channel_ids.wal` | 否 | | 未初始化 session 的指定 WAL channel id；初始化后只能省略或与已绑定 id 相同。 |
| `sessions[].channel_ids.cmd` | 否 | | 未初始化 session 的指定 Cmd channel id；初始化后只能省略或与已绑定 id 相同。 |

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
data/openevent/
data/cmd-worker-output/
```

文件说明：

| 文件 | 说明 |
| --- | --- |
| `config/openevent-server.yaml` | OpenEvent server 配置，包含 gRPC/admin 地址和 `openevent.storage.path` 指定的统一 RocksDB 路径。 |
| `config/im-p2p-syncer.yaml` | IM syncer 配置，包含 Feishu/Lark 凭据、用户/bot mappings、principal tokens。 |
| `config/model-proxy.yaml` | model-proxy 配置，包含 provider base URL/API key、OpenEvent token、channel ids 和请求限制。 |
| `config/cmd-worker.yaml` | cmd-worker 配置，包含 OpenEvent token、cmd channel ids、输出目录、并发和超时设置。 |
| `config/im-model-agent.yaml` | Agent 配置，包含 Agent token、模型名、IM/Model/WAL/Cmd channel ids。 |
| `config/desired.normalized.yaml` | 展开默认值后的目标状态。敏感值会被 redacted。 |
| `config/state.yaml` | 配置摘要、token 摘要、所有已初始化 session 的永久 channel 绑定，以及最近一次 apply 的状态和阶段。 |
| `config/secrets.yaml` | 自动创建或采用的 OpenEvent 明文 token。 |
| `config/plan.yaml` | 本次 apply 的 actions 和配置摘要。 |

新建 runtime root 时权限按 `0700` 创建；生成配置和 state/secrets/plan 按 `0600` 写入。
配置文件包含 OpenEvent token、Feishu/Lark app secret 和模型 API key，不能提交。

## Apply 流程

`--apply` 在 `config/state.yaml` 中记录五个检查点：

| 阶段 | 已完成的工作 |
| --- | --- |
| `parsed` | 输入 YAML 已解析，默认值已展开，字段已校验。 |
| `openevent_ready` | 已写入 `config/openevent-server.yaml`，按需启动或重启 OpenEvent，并确认业务端口和管理端口可用。 |
| `resources_resolved` | 已解析或创建 Provider 身份和会话、OpenEvent token，以及 IM/Model/Cmd/WAL channel。 |
| `config_committed` | 已渲染并校验所有下游配置，并写入 normalized desired state、组件配置、secrets、plan 和 state。 |
| `processes_running` | 已按依赖顺序重启配置变化的下游进程，启动缺失进程，并由 supervisor 确认所有配置的程序都处于 `RUNNING`。 |

`last_apply.status` 取值为 `in_progress`、`complete` 或 `failed`。
`last_apply.phase` 表示最后完成的检查点。失败时，`last_apply.failed_phase` 表示当时正在执行的
检查点。时间戳分别记录 apply 开始、最近更新以及完成或失败的时间。

只有到达 `processes_running`，apply 才会记为 `complete`。这个阶段只证明 supervisor 观察到
进程在运行，不等于端到端健康；它不能证明 Provider 凭据、模型请求、IM 投递或完整事件链路可用。

重启顺序：

```text
openevent -> model_proxy -> cmd_worker -> im_syncer -> agent
```

Agent 最后启动，让 IM syncer、model-proxy 和 cmd-worker 先收到 start/restart 命令；这个顺序本身
不证明它们已经 ready。

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

`session_id` 是 IM、Model、WAL、Cmd 四个 channel 的永久绑定键。一个 session 的四个 channel 全部
解析、校验并在 `config_committed` 阶段以完整映射写入 `config/state.yaml` 后，该 session 完成初始化。
四个 id 必须作为一组提交；部分映射不代表初始化完成。

Channel 协调分为两个生命周期：

1. 未初始化：每类 channel 按输入 YAML 中的显式 id、同名 channel、protocol 和 description 稳定
   业务字段匹配的 channel 依次查找；没有可复用候选时创建。显式 id 是指令，不是提示：它不存在、
   不可访问或不兼容时，apply 直接失败，不能回退到自动发现或创建。
2. 已初始化：`config/state.yaml` 中保存的四个 id 是唯一权威映射。输入中的显式 id 可以省略；如果
   提供，必须与保存值相同。任一 id 不同，或者已绑定 channel 缺失、不可访问或不兼容时，apply
   直接失败；Reconciler 不再查找或创建替代 channel。

禁用 session、从目标配置中暂时移除 session，或随后重新启用，都不解除绑定。`config/state.yaml`
必须保留所有已初始化 session 的完整映射，而不只保留当前 enabled session。需要更换任一 channel
时，必须使用新的 `session_id` 创建新 session；原 `session_id` 不提供解绑或重绑操作。

可复用条件：

- `protocol` 匹配。
- `visibility` 匹配且不是协议禁止值。
- `description` 是合法 JSON，且稳定业务字段匹配。
- channel creator 必须是 `agent.principal` 解析出的 principal。
- 必需成员已经存在，或 `agent.principal` 的 token 可用且能通过 `AddMember` 补齐。

只缺必需成员时，Reconciler 可以按上述条件补齐成员；这不改变 channel 身份。Reconciler 不会修改
旧 channel 的 `protocol`、`description`、`visibility` 或 `creator`，因为当前 OpenEvent API 没有
channel update。它也不会删除旧 channel 或历史事件。

description 稳定字段：

| Channel | 稳定字段 |
| --- | --- |
| IM | `version=v1`、`provider`、`session_id`、`session_type`、`metadata.runtime_name`、`metadata.agent_session_id` |
| Model | `version=v1`、`metadata.runtime_name`、`metadata.agent_session_id`、`metadata.model_proxy_principal` |
| WAL | `version=v1`、`session_id`、`im_channel_id`、`model_channel_id`、`metadata.runtime_name` |
| Cmd | `version=v1`、`metadata.runtime_name`、`metadata.agent_session_id`、`metadata.cmd_worker_principal` |

WAL 的解析依赖最终 IM 和 Model channel id，因为它的 description 会记录这两个 id。Cmd 和 WAL
之间没有顺序依赖，设计不再规定更强的创建顺序。

## 派生配置要点

这里只记录无法从输入字段名直接看出的映射。各组件自己的文档仍是其配置 schema 和运行行为的
权威来源。

| 组件 | 生成契约 |
| --- | --- |
| IM syncer | `worker.principal/token` 来自 `im.worker_principal`；`principal_tokens` 包含 enabled user 和 bot，不包含 worker。每个 enabled session 生成一条 user mapping 和一条 bot mapping；user `external_user_id` 使用显式值或手机号/邮箱解析结果，bot 使用 `im.bot.app_id`。 |
| model-proxy | protocol 固定为 `llm.v1`；`channels` 包含 enabled Model channel ids。provider 配置接收 `model.base_url/api_key/timeout_ms`；`model.model` 只进入 Agent 配置。 |
| cmd-worker | protocol 固定为 `cmd.v1`；principal/token 和 enabled Cmd channel ids 从目标状态解析。`output_dir` 默认是 `<runtime_root>/data/cmd-worker-output`；Agent 不直接执行 shell。 |
| Agent | 每个 enabled session 生成 principal/token 和四个最终 channel id。配置没有 `from_seq`；Agent 从 OpenEvent 历史恢复消费位置。 |

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
| `config/state.yaml` | 当前 token/config 摘要、已初始化 session 的永久 channel 绑定，以及 `last_apply` 状态、已完成阶段或失败阶段。 |
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

## 依赖契约

生成配置使用当前 Python 环境中已安装组件的 config parser 校验。OpenEvent 和各 Worker 的 API、
协议与配置 schema 仍以组件自己的公开文档为准；Reconciler 只负责本文定义的输入到配置映射。Agent
行为和 WAL 字段由本项目的 [IM_MODEL_AGENT_cn.md](IM_MODEL_AGENT_cn.md) 与
[AGENT_WAL_PROTOCOL_cn.md](AGENT_WAL_PROTOCOL_cn.md) 定义。

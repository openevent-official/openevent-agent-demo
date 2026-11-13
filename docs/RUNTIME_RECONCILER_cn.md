# Chat Runtime Reconciler

> 实现状态：提示词文件加载、上下文字节预算预览、Chat 正文上限及 `agent.wal.v5` 部署配置已实现；测试范围与外部联调限制见 [README](../README_cn.md)。

Runtime 为一套 Chat Agent 部署准备凭据、三个共享辅助 Channel、组件配置和本地进程。
网页会话由 Chat Server 创建，Agent 自行发现；Runtime 不维护会话列表，不读取 Chat 的会话目录。
下文规定部署配置与执行流程。
会话发现与 Agent 配置见 [Agent 文档](CHAT_MODEL_AGENT_cn.md#会话发现)。
部署规格只接受 `chat-runtime.v1`。

## 入口

在项目目录运行：

```bash
python -B -m runtime_reconciler.cli --spec openevent-stack/stack.yaml --runtime-root "$PWD/openevent-stack/runtime" --dry-run
python -B -m runtime_reconciler.cli --spec openevent-stack/stack.yaml --runtime-root "$PWD/openevent-stack/runtime" --apply
python -B -m runtime_reconciler.cli --spec openevent-stack/stack.yaml --runtime-root "$PWD/openevent-stack/runtime" --start
```

上述命令及 stack 脚本从项目目录运行 Runtime 源码。也可通过 `openevent-stack/bootstrap.sh --dry-run|--apply`、
`start.sh`、`stop.sh`、`status.sh` 和 `logs.sh [program]` 操作本地部署。
`--apply` 按五个主要阶段报告进展：检查配置、准备 OpenEvent、解析凭据和 Channel、准备服务配置、更新服务。
`--apply` 和 `--start` 都会指出正在启动或停止的具体服务，停止时说明正在等待服务退出，也可提示复用的服务已经运行。
每行进度立即刷新输出，即使通过管道读取也能及时看到。成功结束后仍打印简短成功提示；
`bootstrap.sh --apply` 和 `start.sh` 随后显示各服务的实际进程状态，以及 Chat 和 View 的访问 URL。
`--dry-run` 和 `--print-config` 仍输出 YAML 计划或配置，不加进度消息。
Chat URL 优先使用配置的 `chat.origin`，未配置时按监听 host 和 port 生成 HTTP 地址，页面路径为 `/api/chat/`。
View URL 使用其监听 host 和 port，页面路径为 `/`。通配监听地址 `0.0.0.0` 和 `::` 分别显示为本机地址 `127.0.0.1` 和 `[::1]`，其他 IPv6 地址也加方括号。

本地 stack 脚本固定使用 `openevent-stack/runtime/` 作为运行目录，生成的配置、状态、数据、日志和 PID 文件均放在其下。
本地 stack 目录路径中的软链接统一解析为真实路径，使启动、状态查询和停止操作识别同一部署。
脚本显式传入 `--runtime-root`，因此修改 spec 的 `runtime.root` 不会改变本地脚本的目录；模板不提供该字段。
上面的直接调用使用绝对路径，确保与本地进程脚本使用同一目录。其他部署可以通过通用命令入口使用
`runtime.root` 或 `--runtime-root` 选择目录，并让自己的进程管理器读取该目录中的配置。

- `--dry-run` 检查用户配置及已有部署绑定并预览配置路径，不调用 OpenEvent，不写运行配置、资源绑定或 secrets。
  计划中的 `channels` 只列出已知真实 ID；Model/Cmd/WAL ID 全部未知时，不输出整个 `channels` 段。未知 ID 在 `--apply` 时自动解析。
  计划不列出 `unresolved` 清单；`notes` 统一说明 OpenEvent 访问凭据将在执行 `--apply` 时自动复用或创建，无需手工补齐。
  预览不使用临时 ID 或 token，也不把未解析的配置交给组件校验器；预览成功不代表完整组件配置已经通过校验。
- `--apply` 先检查用户配置、已有部署绑定和不依赖 Channel ID/token 的 OpenEvent 配置，
  按下文顺序使 OpenEvent 在目标地址就绪，再准备真实资源。取得 Channel ID 和有效 token 后，
  调用其余组件的配置校验器，通过后提交完整配置并继续启动。初始化成功后重复执行复用原绑定和有效凭据。
  组件配置校验的临时文件位于当前工作目录的 `build/validation/`。
- `--write-diagnostics` 仅与 `--apply` 一起使用，导出下文说明的可选诊断文件；
  本地脚本用法为 `openevent-stack/bootstrap.sh --apply --write-diagnostics`。
- `--start` 只用于已成功完成首次初始化的部署；允许修改的配置发生变化，包括凭据或显式 OpenEvent token，
  均要求先 apply。进程管理入口和名称按下文固定，不能通过 apply 更换。
- `--runtime-root DIR` 覆盖运行目录。
- `--print-config openevent|chat_server|model_proxy|cmd_worker|agent|view` 输出预览配置；
  显式提供的凭据仍出现在该输出中，未解析字段为 `null`；这份预览不能直接作为运行配置。

spec 中的相对路径以 spec 文件所在目录为基准；所有生成配置中的运行路径均为绝对路径。
`supervisor.ctl` 不含斜杠时按 PATH 查找，否则也按此基准解析。
异常退出码为 1，spec 错误为 2，协调失败为 3。

## 配置

最小结构如下。示例 token 和模型 key 必须换成部署值：

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

四个 principal 必须是互不相同的正 uint64。用户凭据只给 Chat Server；Agent、Model Proxy、Cmd Worker 各使用自己的凭据。
web token 与 OpenEvent token 是不同凭据；模型 key 只进入 Model Proxy 配置。

本例的总请求预算与[完整请求示例](examples/README_cn.md)一致，配合 Chat 的默认正文上限及 Agent 的默认工具输出额度。
示例校验包含正文转义和同一工具同时返回 stdout、stderr 的情况；附件描述、回复引用和错误详情仍按实际完整事件准入。
更换提示词、模型名或额度后，应查看下文的预算预览；正文和输出额度按原始字节计量，不能直接当作模型事件的编码额度。

| 配置 | 含义与默认值 |
| --- | --- |
| `runtime.name` | 必填、稳定的部署标识 |
| `runtime.root` | 通用入口的运行目录，缺省 `runtime/<name>`；本地 stack 脚本固定覆盖为 `openevent-stack/runtime/` |
| `runtime.supervisor.ctl` | 接受 `status/start/stop <program>` 的进程控制入口，默认 supervisorctl；首次 apply 后固定 |
| `runtime.supervisor.programs` | 初次部署可覆盖逻辑名到进程名的映射，见下表；首次 apply 后固定 |
| `openevent.storage.path` | 默认运行目录下 `data/openevent` |
| `openevent.max_payload_bytes` | 默认 16777216；生成配置再由模块 parser 校验。相同额度传给 Agent、Model Proxy 和 Cmd；Agent 的编码大小检查见 [Agent 配置](CHAT_MODEL_AGENT_cn.md#agent-配置) |
| `tokens` | 可选映射，键为 user、agent、model_proxy、cmd_worker |
| `chat.channels_dir` | 默认运行目录下 `chat-sessions`，只由 Chat Server 管理 |
| `chat.host/port/origin` | host 默认 127.0.0.1，port 默认 8081；origin 可选，默认 null；写入 `chat-server.json` 的同名字段 |
| `chat.max_user_message_bytes` | 可选，默认 65536；正整数，不接受 boolean。原值传给 Chat Server 的同名字段，按单条用户正文的 UTF-8 字节数限制新发送，不计算附件文件或回复目标正文；Agent 的完整模型请求预算另行判断 |
| `model.provider_name` | 默认 default |
| `model.response_header_ms/idle_ms` | Provider 响应头/空闲超时，各默认 60000 |
| `model.max_concurrency` | 所有会话共享的 Model Proxy 并发额度，默认 4 |
| `cmd.output_dir` | 默认运行目录下 `cmd-output` |
| `cmd.max_concurrent_tasks/default_timeout_ms` | 默认 4 / 300000 |
| `channels.model/cmd/wal` | 填 `auto` 或省略表示首次初始化时按下文发现或创建；也可填写已有 Channel 的正 uint64 ID。整个 `channels` 映射可省略，不能填写 `null` 或数字字符串。初始化后始终沿用永久绑定 |
| `view.port` | 默认 9999；View 监听 0.0.0.0 |
| `agent.system_prompt_file` | 必填，完整系统提示词 UTF-8 文件路径，见下节 |
| `agent.context_compression_prompt_file` | 必填，完整压缩提示词 UTF-8 文件路径，见下节 |
| `agent.context_max_input_bytes` | 必填，无默认值；上下文字节预算，定义见 [Agent 配置](CHAT_MODEL_AGENT_cn.md#上下文字节预算) |
| `agent.model_retry_interval_ms` | 可选，原值传给 Agent；默认值与等待规则见 [模型重试间隔](CHAT_MODEL_AGENT_cn.md#模型重试间隔) |
| `rpc.max_retries/timeout_ms` | 默认 3 / 10000；传给 Agent，其他组件仅使用其公开支持的字段。RPC 重试间隔内部固定为 100 毫秒，与当前 Chat SDK 一致，不提供 RPC 间隔配置项 |

`runtime.startup_timeout_ms` 已从配置设计删除，不再接受；启动流程不等待 Agent 对象引用就绪。

`agent` 的运行选项遵循 [Chat Agent 配置](CHAT_MODEL_AGENT_cn.md)；两个提示词文件入口由下节定义，
Runtime 读取正文后生成 Agent 配置中的文本字段，不把源文件路径交给 Agent。
Runtime 注入身份、三条 Channel、模型名和 Provider，`work_root` 默认是运行目录下的 `workdir`。
Runtime 的 `agent.object_target` 默认等于 OpenEvent 业务地址，仅用于 Cmd 中对象读取命令的环境，
不写入 Agent 配置。Agent 的模型调度名额与 Model Proxy 的执行并发分别配置，
Agent 名额的含义见 [Agent 配置](CHAT_MODEL_AGENT_cn.md#agent-配置)。

| 逻辑名 | 默认进程名 | 配置 |
| --- | --- | --- |
| openevent | openevent | openevent-server.yaml |
| model_proxy | model-proxy | model-proxy.yaml |
| chat_server | chat-server | chat-server.json，包含 host、port、origin |
| view | openevent-view | openevent-view.yaml |
| agent | chat-model-agent | chat-model-agent.json |
| cmd_worker | cmd-worker | cmd-worker.yaml |

配置保存在运行目录的 `config/`，权限为 0600。状态文件为 `state.yaml`，规格为
`chat-runtime.state.v1`；secrets 为 `secrets.yaml`，规格为 `chat-runtime.secrets.v1`。
Chat 启动命令只传 `--config`，监听参数和启动后显示的访问地址均从 `chat-server.json` 读取。
默认不写诊断文件。指定 `--write-diagnostics` 后，仅在 apply 完成全部组件启动并保存成功状态后，
以 0600 权限导出 `desired.normalized.yaml`（脱敏配置）和 `plan.yaml`（本次配置写入计划）。
包含 `changed`、`sha256` 等字段的详细计划只通过该选项导出，不打印在普通成功输出中。
诊断文件写入失败只在标准错误输出告警，不影响部署成功；它们不参与启动、恢复或已有部署的判断。
这些文件只是导出时的快照；后续未指定该选项或导出失败时，已有文件可能仍是旧内容，可以直接删除。
`initialized: true` 只在首次 apply 完成所有组件启动后保存。此前已生成状态但失败或中断的部署，
再次 apply 或 start 会报错并保留现场；本版不自动续跑半初始化状态。运维按下文判断哪些资源需要保留、是否重建以及重建范围。
首次 apply 在任何进程控制前，将解析后的进程管理入口和完整名称映射保存为 state 的 `supervisor` 绑定。
这组绑定在本次部署期间保持固定；结束旧部署并建立新状态后才可重新选择。后续各入口发现绑定缺失或配置不同即报错，不执行进程控制；
本版不提供更换进程管理入口或名称的迁移功能。
state 还保存完整有效配置的摘要 `applied_spec_sha256`，供 `--start` 比较；不使用脱敏展示文件判断配置是否变化。
apply 通过前置检查后，在首次停止进程或覆盖配置前，先清除旧摘要，保留上次完整提交的组件配置记录，
并将本次需要停止后重新启动或更新的组件合入已有 `pending_restarts` 后持久保存。
运行进程是否仍须更新由这些配置记录和待重启事项共同判断。
提前写入 OpenEvent 配置不代表全套配置已提交。其余组件校验通过且所有配置文件写完后，
摘要、新配置记录和剩余待重启事项一起原子保存；提前完成的 OpenEvent 启动在此时记为已完成。
对已成功初始化的部署，更新在这次完整提交之前中断必须重新 apply；提交之后中断，start 可以继续完成待重启事项。
state 中的 `principals` 和 `work_root` 必须存在并与本次配置一致；部署标识不一致或永久绑定缺失同样报错。

## 提示词文件

`agent.system_prompt_file` 和 `agent.context_compression_prompt_file` 必须显式指定非空路径；
相对路径以 spec 文件目录为基准，绝对路径用于部署环境。仓库已提供上例对应的
[默认系统提示词](../openevent-stack/prompts/system.txt)和[默认压缩提示词](../openevent-stack/prompts/context_compression.txt)。
spec 放在 `openevent-stack/` 时可直接使用上例的 `./prompts/` 路径；spec 位于其他目录时，复制提示词到其 `prompts/` 下或显式指定部署路径。
这些文件随仓库提供，不会因省略必填路径而自动采用；`stack.yaml` 模板已显式配置两个文件入口。
默认系统文件包含完整业务提示与工具、摘要和事件输入、附件使用说明；代码不再隐式追加另一段协议提示词。
编辑提示词时遵循[模型输入输出格式](MODEL_IO_cn.md)，包含摘要和压缩请求的完整例子见[请求示例](examples/README_cn.md)。
摘要中的任务信息与后续结果事件的关系见 [Agent 会话和回答](CHAT_MODEL_AGENT_cn.md#会话和回答)，Runtime 不维护另一份任务输入。
压缩文件是独立的完整压缩提示，不与普通系统文件拼接。UTF-8 正文原样保留，系统文件允许为空，压缩文件必须有非空白内容。
不做模板替换，不同时保留 spec 的内联 `agent.system_prompt` 或内联压缩提示词入口。

`--dry-run`、`--print-config`、`--apply` 和 `--start` 在解析 spec 时各读取两个文件一次。
不存在、不可读或非 UTF-8 的文件在写状态、控制进程前报错；同一次操作的预览、比较与生成使用同一份正文快照。
预览只读取文件，不写运行数据或调用 OpenEvent；`--print-config agent` 展示解析后的完整正文。
`--dry-run` 与 `--print-config agent` 还在标准错误输出按配置 `B` 计算的预算基线：配置请求上限、固定开销、
历史与单事件目标，以及当前 Chat 正文上限、工具每流原始输出上限。
计算使用同一次操作读取的提示词正文、模型名和工具定义，遵循[Agent 的唯一预算规则](CHAT_MODEL_AGENT_cn.md#上下文字节预算)，
不需要 Channel ID、token 或 OpenEvent 调用；配置自身无法满足基线要求时报告配置错误。
这是尚未计入 Model 发布容量的离线预览。运行时的有效预算与压缩目标还会受 OpenEvent 实际发送额度限制，
不因配置的模型输入上限大于该额度而拒绝部署，也不增加配置项。
这两种预览的标准输出保持 YAML 计划或配置格式；这些推导值只用于预览，不增加用户配置项或持久状态。

生成的 `chat-model-agent.json` 保存 `agent.system_prompt`、`agent.context_compression_prompt` 正文与上下文字节预算；
Agent 运行时只读取生成配置，不监听或回读源提示词文件。有效配置摘要包含这两段正文和预算，不以路径或修改时间代替内容。
同一路径正文改变后，`--start` 要求先 apply；仅换路径而正文相同不构成有效配置变化。
成功部署后仅改提示词或预算，apply 更新 Agent 配置并按原规则重启 Agent，不需要重启未变化的依赖。
正文保存到已经准备的普通或压缩请求后即固定；更新只影响后来新准备的请求，不能改写旧请求的重试与恢复输入。
新配置还须通过 [Agent 的历史与未结束请求兼容检查](CHAT_MODEL_AGENT_cn.md#上下文字节预算)。
该检查区分配置预算变化与当前发布容量变化；仅发送容量下降不因此拒绝整个 Agent 恢复，继续执行时按公开 Agent 规则处理。

压缩与普通推理共用现有模型、Provider、等待 timeout 和 Agent 调度名额；
历史硬目标、单事件上限及重试额度只在 [Agent 上下文字节预算](CHAT_MODEL_AGENT_cn.md#上下文字节预算)定义，Runtime 不增加另一套选项。

## 目录结构

Agent 与 Cmd Worker 必须部署在同一主机，并通过相同绝对路径访问同一份实际的 `work_root`。
仅路径文字相同、实际目录不同，不满足此前提。默认会话工作目录为 `work_root/<channel_id>`，目录名为纯数字 Chat Channel ID；
Agent 向 Cmd 提供解析后的绝对路径。重启或重复 apply 不改变已有会话的工作路径。

本地 stack 使用默认路径时，目录用途如下；配置中的路径覆盖只改变对应数据的位置。

```text
openevent-stack/
├── stack.yaml             # 用户填写的部署入口
├── prompts/               # 完整系统和压缩提示词文件
└── runtime/               # 生成的部署文件和运行数据
    ├── config/            # 组件配置、部署状态、凭据和安装生成的 env.sh
    ├── data/openevent/    # OpenEvent 持久数据：消息历史、对象等
    ├── logs/              # 各服务日志
    ├── run/               # 进程 PID
    ├── chat-sessions/     # Chat 会话配置，文件名为 <session_id>.json
    ├── cmd-output/        # Cmd 保存的命令 stdout/stderr
    └── workdir/           # Agent 执行命令的默认工作目录根
        └── <channel_id>/  # 纯数字 Chat Channel ID，例如 4
            └── ...        # 命令创建或下载的文件
```

运行目录中的 `workdir/4/` 只保存 Channel 4 中命令使用的本地文件；会话配置位于同级的 `chat-sessions/`，聊天记录保存在 OpenEvent 中。
会话重命名只改 Chat 会话配置中的显示名称，不改变数字目录名。命令显式传入 `workdir` 时使用该路径。
会话名称的保存和页面操作见 [Chat 应用指南](https://github.com/openevent-official/openevent-modules-chat/blob/main/docs/APP_USAGE_cn.md)。

环境安装脚本的 `--workdir runtime` 使用项目根目录下的 `runtime/`：`runtime/venv/` 放 Python 环境，`runtime/sources/` 放各组件源码及其构建产物。
它与 `openevent-stack/runtime/` 分开，不控制上面这些运行数据路径。安装工作区说明见 [环境准备](UBUNTU_ENVIRONMENT_cn.md#准备与安装)。

## Token 与资源绑定

每个身份按显式 token、本地 secrets 的顺序调用 `GetStatus` 验证。
只有明确的 `UNAUTHENTICATED` 表示该 token 无效；超时、网络错误或权限错误使本次 apply 失败，
不以此为理由创建新 token。两种来源都不存在或明确无效时才调用 `AddToken`。
从 `AddTokenResponse.token` 取得新 token 后先持久保存，再验证并交给组件；不调用 `ListTokens`。
本地凭据文件仅在身份与 token 的映射变化时更新；验证通过或再次采用相同凭据不重复保存。

所有会话共用 private 的 Model、Cmd、WAL Channel。成员分别为 Agent 与 Model Proxy、
Agent 与 Cmd Worker、Agent 自己；协议分别为 `llm.v1`、`cmd.v3`、`agent.wal.v5`。
WAL 规则以 [WAL 协议](AGENT_WAL_PROTOCOL_cn.md)为准。
Model/Cmd 的其他校验采用各模块公开契约。显式指定或已经绑定的 Cmd Channel，其 description 可为任意文本。
Runtime 自动创建的 Model/Cmd/WAL description 在 metadata 中保存稳定 deployment_id。
首次部署只需填写部署配置并运行 `--apply`，由 Runtime 启动 OpenEvent、准备 token 和辅助 Channel，再启动其余组件。
校验通过后，Runtime 将同一个 Cmd ID 写入 Agent 和 cmd-worker 配置，并按下节顺序先启动 cmd-worker，再启动 Agent。

首次初始化时，Model/Cmd/WAL 先采用显式 ID，否则按稳定元数据查找；多候选即失败，没有候选才创建。
自动发现 Cmd 只匹配本部署元数据，不认领只有相同名称或成员的普通 Cmd Channel。
三条全部校验完成后作为一组保存，不能用部分结果启动 Agent；不保存逐条创建的未决标记或部分 ID 供续跑。
初始化成功后，三条 ID 为永久绑定。改名、会话增加、暂时不可访问都不能更换它们。
显式 ID 与原绑定冲突、资源丢失或不兼容时直接失败；新部署使用新状态和新 WAL。

### 初始化失败后的处理

首次 apply 完成所有组件启动并保存成功状态才算初始化成功。初始化涉及本地文件、OpenEvent 资源和多个进程，
不是一次可以整体回滚的事务。某一步失败时，之前创建的 token、Channel、写入的数据和已启动的进程可能仍然存在；
入口报错不表示这些动作没有发生，也不表示已启动的业务任务已经停止。
Runtime 停止后续协调、保留已有记录，不自动删除数据、回滚资源或继续半初始化流程。
前置校验失败且尚未生成部署状态时，修正配置后可直接重试。

运维根据实际进程、保存的状态和 OpenEvent 中的资源决定下一步。需要保留已有数据或采用了既有资源时，
可以保留现场排查、安排修复，或隔离旧部署并建立独立的新部署；本版没有自动导入或修复半初始化状态的入口，
保留数据本身不会使再次 apply/start 获得续跑能力。新部署使用新状态和新 WAL，不把旧会话的历史随意绑定到新部署。
如果确认本次部署及其资源均可丢弃，运维也可以结束旧部署、停止相关进程并清理后重建。
停止与发布请求交接仍遵守下节的运行前提。Runtime 不替运维决定资源归属或删除范围。

选择清理时，需要区分这些后果：

- 只删除本地 state 或配置，不会撤销已创建的远端资源、已提交的事件或命令效果，也不等于恢复部署。
- 删除 OpenEvent 存储会删除其中的 Channel、token、事件历史和对象。原配置中的显式 Channel ID 和 token 随之失效，
  不能承诺保留原 `stack.yaml` 就能原样重跑；重建前应重新准备资源和凭据，或把允许自动创建的 Channel 配置改为 `auto`。
- 本地 stack 在 `openevent-stack/runtime/` 下生成 `config/`、`data/`、`logs/`、`run/`、`workdir/`、`cmd-output/` 和 `chat-sessions/`；
  实际数据也可能位于配置指定的其他路径。这是定位数据的清单，不是必须全部删除的步骤。
  `openevent-stack/runtime/config/env.sh` 保存安装路径，部署脚本和已安装环境可以继续使用；入口配置按保留或重建后的资源重新核对。

以上只说明首次初始化失败的边界。成功部署后的配置更新失败仍保留运行数据，按下节的提交边界继续更新。

## 启动、更新与停止

运行前提：恢复新 Agent 前，运维已保证旧 Agent 发出的 OpenEvent 发布 RPC 全部结束，不会迟到提交。
这不要求已提交的模型或命令业务任务执行完毕。

apply 按以下顺序执行：

1. 校验用户配置和已有绑定，单独生成并校验 OpenEvent 目标配置；这一步不需要 Channel ID 或 token。
2. 如果依赖需要更新，先停止 Agent 并等待退出，再修改任何依赖的运行配置或进程。
   OpenEvent 需要更新时，还须停止本部署使用它的 Model Proxy、Chat Server、View 和 Cmd Worker。
   随后写入已校验的 OpenEvent 配置，按需启动或重启 OpenEvent，再等待目标配置中的业务和管理端口就绪。
   判断是否需要重启依据 state 中上次完整提交的配置记录和待重启事项；文件已经写成目标值不代表进程已使用它。
   配置未变、没有待重启事项且正在运行的 OpenEvent 保持运行；未运行则启动。
3. 连接已经就绪的目标地址，验证或准备 token、解析并校验三条辅助 Channel。
   取得真实 ID 和有效 token 后，生成并校验其余组件配置，再提交完整配置和剩余待重启事项。
4. 启动或按需重启 Model Proxy、Chat Server、View、Cmd Worker，最后启动 Agent。

因此修改 OpenEvent 业务或管理端口时，先切换 OpenEvent，再连接新地址；不会先等待尚未启动的新端口。
任一步失败即停止协调，不继续启动后续组件或 Agent，不报告完整 apply 成功。
首次初始化失败按上节由运维决定处理方式；已成功初始化的部署更新时，OpenEvent 已切换而完整配置尚未提交，
保留现场并重新 apply，不自动回滚。
Agent 自行恢复历史后才派发新任务。Cmd 从执行请求取得附件凭据，可以独立处理已经提交的任务；
不需要等待 Agent 提供凭据查询服务，也没有恢复就绪和放行文件。
进程控制入口的 status 必须返回 RUNNING；进程运行状态不代表模型和工具完整链路已通过验证。
本地状态扫描在同次扫描中复用预期配置路径，仍逐个核对 PID、进程存活状态和命令行。

Cmd Worker 从已生成部署状态 `state.yaml` 的 `cmd_environment` 读取对象服务地址，
据此设置 `OPENEVENT_OBJECT_TARGET`；读取失败即启动失败。
PATH 包含部署 Python 的 bin 目录，因此可以运行本项目安装的 `openevent-read-object`。
默认会话工作目录遵循[目录结构](#目录结构)中的部署前提与路径规则。附件内存映射与命令结果语义见 Agent 文档。

成功初始化后，重复 apply 只重启配置发生变化的组件、因依赖更新已停止的组件和 Agent，并继续已有待重启事项。
如果变化到取得真实资源、生成完整组件配置时才确定，也必须先停止 Agent，再提交依赖配置和更新进程。
Agent 完成恢复后再派发，保证依赖更新期间没有新的派发。
完整配置提交后仍需完成的重启保存在 state 的 `pending_restarts` 中；只有后续进程启动全部完成才清除。
apply 或 start 按上述提交边界继续这些事项，不能因配置文件已更新就复用仍运行旧配置的进程。
`--start` 不重新解析资源或修改配置：先确保已提交配置中的 OpenEvent 运行且目标地址就绪，
再启动其余依赖并完成相应待重启事项，最后启动 Agent；OpenEvent 已从待重启清单移除也不能跳过运行检查。
Chat 配置中的监听参数或 origin，以及 worker 对象读取环境的变化，也会触发对应组件重启。
新网页会话不触发 apply，不改变 worker 配置。无配置变化且没有待重启事项时，不重新启动已运行的进程。

正常停止先向 Agent 发 SIGTERM，等待其收尾和退出，再关闭 worker 与其他依赖。
本地进程脚本在发送信号前核对 PID，等待 Agent 退出时不设置总时限。
其他组件收到 SIGTERM 后等待十秒，仍未退出则发送 SIGKILL，再最多等待两秒确认退出。
发送停止信号失败且进程仍存活，或其他组件强制结束后仍未确认退出时，停止命令报错并保留 PID 记录。
整套停止在失败处中止，不继续关闭依赖；启动清理或重启遇到同样失败时，也不能启动替代进程。
强制结束后的重启仍遵守上述运维前提，并由 Agent 恢复历史。
只停止 Agent 时，Agent 不会因停机主动发送业务取消；仍在运行的 worker 可以继续处理已提交任务。
Cmd 重启后，已接受且没有最终结果的命令按
[Cmd 的恢复规则](https://github.com/openevent-official/openevent-modules-cmd/blob/main/docs/CONFIGURATION_cn.md#恢复与退出)
补写 `RECOVERY_FAILED`，不重新执行。调用失败冻结、取消、RPC 重试和模型历史只由 Agent 规则定义。

## 依赖与验证

使用部署 Python 环境已安装的 `openevent-sdk>=0.13.0`、Chat、`openevent-model-proxy>=0.1.1`、
`openevent-modules-cmd>=0.3.0,<0.4.0`（Cmd 0.3.x）和 `openevent-view>=0.1.0` 发行包；
SDK 必须提供 `AddTokenResponse.token` 和 `batch_get_object_metadata`。
View 是本部署必需组件，本项目的包依赖中已声明。
Chat SDK 必须支持 `get_turn_content`、`TurnWriter.reset`、按创建位置、链尾和有效内容清单恢复 `TurnWriter`，
以及带 `start_seq` 的取消调用和 `serialize_payload`；
Model SDK 必须支持流式协议，Cmd SDK 必须支持指定发布 UUID 和通过 `object_keys` 传递附件凭据。
版本号相同不表示能力相同，安装脚本使用本次构建产物替换各本项目包并检查实际公开 API。
不从 SDK 子模块拼接源码路径。OpenEvent 配置先独立校验，其余配置分别由 Chat、Model、Cmd、Agent、View 的 parser 校验。

```bash
make test PYTHON=/absolute/path/to/python TEST_ARGS='-p test_reconcile_runtime.py'
make test PYTHON=/absolute/path/to/python TEST_ARGS='-p test_stack_scripts.py'
```

测量本地状态查询耗时：

```bash
make benchmark-stack-status PYTHON=/absolute/path/to/python
```

该入口分别执行三次 `status chat-server` 和 `status all` 只读查询，报告各自耗时中位数，并将结果保存到 `build/benchmarks/`；不启动或停止服务。

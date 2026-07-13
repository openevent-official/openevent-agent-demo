# IM Model Agent

[English version](IM_MODEL_AGENT.md)

IM Model Agent 是 Agent demo 中连接 `im.v1` 会话、`llm.v1` model-proxy
和 `cmd.v1` 本地命令的进程。它不直接访问 IM Provider，也不直接调用模型 Provider；
所有输入、模型请求、模型结果、命令请求/结果、Agent WAL 记录和 IM 回复都通过
OpenEvent channel 追踪。

## Channel

每个 enabled session 绑定四类 channel：

| Channel | Protocol | 内容 |
| --- | --- | --- |
| IM channel | `im.v1` | 用户 `sync.record`、Agent `send.request`、IM worker `send.result` |
| Model channel | `llm.v1` | Agent `infer.request`、model-proxy `infer.result` |
| WAL channel | `agent.wal.v1` | Agent `llm.request.prepare` |
| Cmd channel | `cmd.v1` | Agent `cmd.run.request`、cmd-worker `cmd.run.accepted` / `cmd.run.result` / 输出读取 |

首版 Agent demo 只支持 P2P 单聊。Agent principal 必须等于该 P2P channel active
bot mapping 的 principal；`user_principal` 必须是同一 channel 的 active user
mapping principal。

## 配置

```yaml
version: v1

agent:
  name: im-model-agent
  principal: 90002
  token: tok-bot-90002
  system_prompt: "你是一个通过 IM 与用户对话的助手。"
  max_context_messages: 20
  model: gpt-4o-mini
  model_timeout_ms: 60000
  max_model_attempts: 3
  freeze_message: "模型服务暂时没有响应，会话已暂停。请再发送一条消息以继续。"

openevent:
  target: 127.0.0.1:9527
  subscribe:
    only_my_recipient: false
    idle_sleep_ms: 200

model_proxy:
  principal: 20001

im_sync_worker:
  principal: 90001

cmd_worker:
  principal: 30001

sessions:
  - session_id: agent-session-001
    im_channel_id: 10001
    model_channel_id: 20001
    wal_channel_id: 30001
    cmd_channel_id: 40001
    user_principal: 10001
    agent_bot_principal: 90002
    enabled: true
```

Agent 启动时会先从 OpenEvent 现有历史恢复状态，再从恢复边界之后继续消费新消息。
恢复起点由 Agent 内部管理，不需要在配置中指定。

## 处理流程

1. 读取 IM channel 中来自 `user_principal` 的 `sync.record`。
2. 过滤 Agent 自己的回复回流。
3. 写入 WAL `llm.request.prepare`。
4. 向 model channel 发布带命令工具 schema 的 `infer.request`。
5. 接收匹配的 `infer.result`。
6. 如果模型结果包含 tool call，发布对应的 `cmd.run.request` 或
   `cmd.output.read.request`，等待 cmd 结果，把结果映射为 `exec_result`、
   `read_stdout_result` 或 `read_stderr_result`，写新的 WAL，并在同一 turn 内发起后续模型请求。
7. 把最终模型回复，或模型 attempt 多次失败后的冻结提示，作为 `send.request` 写入 IM channel。
8. 由 IM P2P syncer 发送到 Provider，并回写 `send.result`。

用户文本来自 `sync.record.data.text`。非文本、空文本或超大消息降级记录按配置使用占位文本。

## 系统提示词与工具说明

模型请求使用普通 OpenAI 兼容 chat 结构。system message 包含配置中的业务提示词，
也可以追加固定协议提示词。工具参数说明属于模型 API `tools` schema，不写进最后一条
user JSON。

Agent 通过 API `tools` 传入这三个 function tool：

- `exec`
- `read_stdout`
- `read_stderr`

## 模型输入

Agent 发送 OpenAI 兼容 chat 请求。最后一条 `role=user` message 的
`content` 是 JSON 字符串，解析后顶层只包含 `events`。

- 用户 `sync.record` 映射为 `{"type":"user","time":"...","text":"..."}`
  event。
- OpenEvent seq、principal、channel id、WAL seq、`session_id`、`turn_id`、
  model request id 和重试状态不进入模型可见输入。
- 命令结果映射为 `exec_result` event。输出读取结果映射为 `read_stdout_result`
  或 `read_stderr_result` event。唯一暴露给模型的 OpenEvent seq 是原始执行的
  `exec_id`，用于 `read_stdout` 和 `read_stderr` 读取大输出。

## 模型输出

模型输出使用原生 assistant `content` 和 `tool_calls`；Agent 不期待自定义 actions JSON 格式。
如果模型结果包含 tool call，assistant `content` 只视为进度文本，不是最终回复。最终 IM 回复必须等命令结果事件进入后续模型请求后再生成。

## Attempt 与冻结

一批用户消息生成一个 turn。Agent 派生
`turn_id="{session_id}:{first_user_sync_record_seq}"`；它用于日志、去重、冻结提示和恢复，
但不写入 WAL payload，也不进入模型可见输入。

以下情况都视为模型 attempt 失败：

- `agent.model_timeout_ms` 内没有匹配的 `infer.result`。
- 匹配的 `infer.result.status_code` 非 2xx。
- 模型输出无法解析。
- tool call 使用未知工具、非法参数或违反 schema。

失败 attempt 不推进 prompt 状态，也不发送普通 IM 失败回复。如果还没有超过
`max_model_attempts`，Agent 写新的 WAL，并用同一个 turn 和同一批用户消息发布下一次
`infer.request`，但使用新的 `model_request_id`。超过 attempt 上限后，Agent 为该 turn
写一条冻结 `send.request`，并只冻结该 session。冻结期间 session 继续消费用户
`sync.record`，但不发布模型请求；用户再发任意新消息后解冻，并把该消息作为新 turn 处理。

冻结只用于用户可恢复的模型 attempt 失败。WAL 损坏、历史扫描失败、配置冲突和 Agent
不变量破坏属于 error 状态；Agent 退出并等待人工修复。

## 恢复

Agent 不使用外部数据库保存会话状态。启动时它扫描已配置 IM、Model、WAL 和 Cmd channel 的
OpenEvent 历史。它会重建：

- 待处理用户消息队列。
- WAL、模型请求/结果、命令请求/结果和 IM send 索引。
- prompt 状态、冻结 session 和待确认 `send.result`。

如果存在 WAL 但缺少对应 `infer.request`，Agent 基于完整历史重建请求 body，并使用同一个
WAL seq 派生的 `request_id` 补发。如果模型结果是失败 attempt，恢复时按运行时相同的
重试/冻结规则处理。如果同一 `turn_id` 已经存在最终 `send.request`，迟到模型结果和重复恢复路径
不得写第二条最终 IM 回复。

## 运行

```bash
python3 -m im_model_agent.cli --config /path/to/im-model-agent.yaml
```

实际部署建议由 runtime reconciler 生成配置和 channel。

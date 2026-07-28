# IM Model Agent

[English version](IM_MODEL_AGENT.md)

IM Model Agent 持续连接 `im.v1` 会话、`llm.v1` model-proxy 和 `cmd.v1` 本地命令。它不直接访问
IM Provider、模型 Provider 或 shell。

## Channel

| Channel | Protocol | 内容 |
| --- | --- | --- |
| IM | `im.v1` | 用户输入和模型输出文字 |
| Model | `llm.v1` | 模型请求和结果 |
| WAL | `agent.wal.v1` | 下一次模型请求的新输入引用 |
| Cmd | `cmd.v1` | 命令、结果和输出读取 |

一个 session 初始化后，四个 Channel id 在其整个生命周期内保持不变；更换任一 Channel 必须创建新的
`session_id`。初始化和绑定规则由 [Runtime Reconciler](RUNTIME_RECONCILER_cn.md#channel-协调) 定义。

首版只支持 P2P。Agent principal 必须是 active bot mapping，`user_principal` 必须是对应 active user。

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
  cmd_result_timeout_ms: 330000

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
    enabled: true
```

配置严格校验。Channel 和 token 由 Runtime Reconciler 或部署工具创建，Agent 只校验，不执行
session Channel 重绑。

## 持续处理

```text
用户消息或工具结果进入队列
  -> WAL 引用本次新事件
  -> infer.request / infer.result
       -> content 非空：发送到 IM
       -> tool_calls 非空：执行工具
       -> 两者可以同时发生
  -> 工具结果重新进入队列
  -> 继续下一次模型请求
  -> 没有新事件时等待
```

Agent 接受的模型结果完整解析并通过全部工具调用校验后，其中非空 `content` 就发送。`null`、空字符串
和只含 JSON whitespace 的字符串不发送；其他类型非法。是否同时返回工具调用不改变这条规则；什么
结果可以接受由 WAL 协议定义。

固定系统提示词要求每次 assistant 响应都用非空 `content` 告诉用户当前进展或有用结果。Agent 为
兼容 Provider 仍接受空 `content`，但会记录 warning，不向 IM 发送文字，也不影响同一响应中的合法
工具调用。

模型文字使用协议定义的稳定 ID，因此重复消费或进程恢复不会重复发送。

## 工具调用

模型返回 `tool_calls` 时，Agent 按顺序发布 Cmd 请求。模型同时返回文字和工具调用时，Agent 先确认
文字发送请求，再执行工具。命令结果通过下一条 WAL 输入模型，不直接伪装成用户消息或 IM 回复。
`cmd.v1` request payload 没有稳定 ID：业务关联键只保留在 Agent 状态和 WAL 超时记录中；不确定发布
按 Agent principal 和完整 payload 对账；request 的 OpenEvent seq 是 Cmd task ID。Cmd request 超过
`cmd_result_timeout_ms` 且对账仍无结果时，Agent 把超时作为带 `error_code="timeout"` 的失败工具结果
交给模型。cmd-worker 返回命令执行 `TIMEOUT` 时使用同一错误码。完整恢复规则见 WAL 协议。

当前 P2P Sync Worker 只在 Provider 发送成功后写 `send.result`。发送失败时，稳定的 `send.request` 保持
未完成，Sync Worker 退出；修复并重启后继续处理同一 request。Agent 不补写第二条 request，也不会因
旧历史中的 `FAILED` 结果退出。

## WAL 与恢复

Agent 启动时从 OpenEvent 历史恢复，模型失败时按原 WAL 重试。WAL 字段、稳定 ID、Provider 侧并发
请求、stale 结果、blocked、解除和恢复规则统一见
[AGENT_WAL_PROTOCOL_cn.md](AGENT_WAL_PROTOCOL_cn.md)，本文不维护第二份协议说明。

# IM Model Agent

[English version](IM_MODEL_AGENT.md)

IM Model Agent 是 Agent demo 中连接 `im.v1` 会话与 `llm.v1` model-proxy
的进程。它不直接访问 IM Provider，也不直接调用模型 Provider；所有输入、模型请求、
模型结果和 IM 回复都通过 OpenEvent channel 追踪。

## Channel

每个 enabled session 绑定三类 channel：

| Channel | Protocol | 内容 |
| --- | --- | --- |
| IM channel | `im.v1` | 用户 `sync.record`、Agent `send.request`、IM worker `send.result` |
| Model channel | `llm.v1` | Agent `infer.request`、model-proxy `infer.result` |
| WAL channel | `agent.wal.v1` | Agent `llm.request.prepare` |

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

sessions:
  - session_id: agent-session-001
    im_channel_id: 10001
    model_channel_id: 20001
    wal_channel_id: 30001
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
4. 向 model channel 发布 `infer.request`。
5. 接收匹配的 `infer.result`。
6. 向 IM channel 发布 `send.request`。
7. 由 IM P2P syncer 发送到 Provider，并回写 `send.result`。

用户文本来自 `sync.record.data.text`。非文本、空文本或超大消息降级记录按配置使用占位文本。

## 运行

```bash
python3 -m im_model_agent.cli --config /path/to/im-model-agent.yaml
```

实际部署建议由 runtime reconciler 生成配置和 channel。

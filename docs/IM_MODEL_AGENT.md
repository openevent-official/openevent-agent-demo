# IM Model Agent

[中文版](IM_MODEL_AGENT_cn.md)

IM Model Agent is the Agent demo process that connects an `im.v1` conversation
to `llm.v1` model-proxy. It does not access the IM Provider directly and does
not call the model Provider directly. All inputs, model requests, model results,
and IM replies are tracked through OpenEvent channels.

## Channels

Each enabled session binds three kinds of channels:

| Channel | Protocol | Contents |
| --- | --- | --- |
| IM channel | `im.v1` | User `sync.record`, Agent `send.request`, IM worker `send.result` |
| Model channel | `llm.v1` | Agent `infer.request`, model-proxy `infer.result` |
| WAL channel | `agent.wal.v1` | Agent `llm.request.prepare` |

The first Agent demo version supports only P2P direct chats. The Agent principal
must equal the active bot mapping principal for the P2P channel, and
`user_principal` must be the active user mapping principal in the same channel.

## Configuration

```yaml
version: v1

agent:
  name: im-model-agent
  principal: 90002
  token: tok-bot-90002
  system_prompt: "You are an assistant that talks with users through IM."
  max_context_messages: 20
  model: gpt-4o-mini
  model_timeout_ms: 60000
  max_model_attempts: 3
  freeze_message: "The model service is temporarily unavailable. The session is paused. Send another message to continue."

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

At startup, the Agent first restores state from existing OpenEvent history, then
continues consuming new messages after the restored boundary. The restore start
point is managed internally by the Agent and does not need to be specified in
configuration.

## Processing Flow

1. Read `sync.record` messages from `user_principal` in the IM channel.
2. Filter out the Agent's own reply loopback.
3. Write WAL `llm.request.prepare`.
4. Publish `infer.request` to the model channel.
5. Receive the matching `infer.result`.
6. Publish `send.request` to the IM channel.
7. The IM P2P syncer sends it to the Provider and writes back `send.result`.

User text comes from `sync.record.data.text`. Non-text, empty-text, or oversized
message downgrade records use placeholder text according to configuration.

## Run

```bash
python3 -m im_model_agent.cli --config /path/to/im-model-agent.yaml
```

For real deployments, use Runtime Reconciler to generate configuration and
channels.

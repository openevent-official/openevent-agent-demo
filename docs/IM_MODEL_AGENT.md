# IM Model Agent

[中文版](IM_MODEL_AGENT_cn.md)

IM Model Agent is the Agent demo process that connects an `im.v1` conversation
to `llm.v1` model-proxy and local commands through `cmd.v1`. It does not access
the IM Provider directly and does not call the model Provider directly. Inputs,
model requests, model results, command requests/results, Agent WAL records, and
IM replies are tracked through OpenEvent channels.

## Channels

Each enabled session binds four kinds of channels:

| Channel | Protocol | Contents |
| --- | --- | --- |
| IM channel | `im.v1` | User `sync.record`, Agent `send.request`, IM worker `send.result` |
| Model channel | `llm.v1` | Agent `infer.request`, model-proxy `infer.result` |
| WAL channel | `agent.wal.v1` | Agent `llm.request.prepare` |
| Cmd channel | `cmd.v1` | Agent `cmd.run.request`, cmd-worker `cmd.run.accepted` / `cmd.run.result` / output reads |

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

At startup, the Agent first restores state from existing OpenEvent history, then
continues consuming new messages after the restored boundary. The restore start
point is managed internally by the Agent and does not need to be specified in
configuration.

## Processing Flow

1. Read `sync.record` messages from `user_principal` in the IM channel.
2. Filter out the Agent's own reply loopback.
3. Write WAL `llm.request.prepare`.
4. Publish `infer.request` to the model channel with the command tools schema.
5. Receive the matching `infer.result`.
6. If the model result contains tool calls, publish the matching `cmd.run.request`
   or `cmd.output.read.request`, wait for the cmd result, map it to
   `exec_result`, `read_stdout_result`, or `read_stderr_result`, write a new WAL,
   and continue the same turn with a follow-up model request.
7. Publish the final model response, or a freeze message after repeated model
   attempt failures, as `send.request` to the IM channel.
8. The IM P2P syncer sends it to the Provider and writes back `send.result`.

User text comes from `sync.record.data.text`. Non-text, empty-text, or oversized
message downgrade records use placeholder text according to configuration.

## System Prompt And Tools

The model request uses normal OpenAI-compatible chat structure. The system
message contains the configured business prompt and may append fixed protocol
instructions. Tool parameter descriptions belong in the model API `tools`
schema, not in the final user JSON.

The Agent passes exactly these function tools through API `tools`:

- `exec`
- `read_stdout`
- `read_stderr`

## Model Input

The Agent sends OpenAI-compatible chat requests. The final `role=user` message
content is a JSON string whose top-level object contains only `events`.

- User `sync.record` messages become `{"type":"user","time":"...","text":"..."}`
  events.
- OpenEvent seqs, principals, channel ids, WAL seqs, `session_id`, `turn_id`,
  model request ids, and retry state are not included in model-visible input.
- Command results become `exec_result` events. Output-read results become
  `read_stdout_result` or `read_stderr_result` events. The only OpenEvent
  sequence exposed to the model is the original execution `exec_id`, used by
  `read_stdout` and `read_stderr` for large outputs.

## Model Output

Model output uses native assistant `content` and `tool_calls`; the Agent does
not expect a custom actions JSON format. If a model result contains tool calls,
assistant `content` is treated as progress text, not the final reply. The final
IM reply is generated only after command result events are fed back into a
follow-up model request.

## Attempts And Freeze

One user-message batch is a turn. The Agent derives
`turn_id="{session_id}:{first_user_sync_record_seq}"`; it is used for logging,
deduplication, freeze messages, and recovery, but is not written into the WAL
payload or model-visible input.

A model attempt fails when any of these happens:

- No matching `infer.result` appears before `agent.model_timeout_ms`.
- The matching `infer.result.status_code` is non-2xx.
- The model output cannot be parsed.
- A tool call uses an unknown tool, invalid arguments, or a schema violation.

Failed attempts do not advance prompt state and do not send a normal IM failure
reply. If the turn is still below `max_model_attempts`, the Agent writes a new
WAL record and publishes another `infer.request` with the same turn and user
message batch but a new `model_request_id`. After the attempt limit, the Agent
writes one freeze `send.request` for that turn and freezes only that session.
While frozen, the session keeps consuming user `sync.record` messages but does
not publish model requests. Any new user message unfreezes the session and is
processed as a new turn.

The freeze path is only for user-recoverable model-attempt failures. WAL
corruption, failed history scans, configuration conflicts, and Agent invariant
breaks are error states; the Agent exits and waits for manual repair.

## Recovery

The Agent does not use an external database for session state. On startup it
scans OpenEvent history for configured IM, Model, WAL, and Cmd channels. It rebuilds:

- Pending user-message queues.
- WAL, model request/result, command request/result, and IM send indexes.
- Prompt state, frozen sessions, and pending `send.result` confirmations.

If a WAL record exists without the corresponding `infer.request`, the Agent
rebuilds the request body from full history and republishes using the same WAL
seq-derived `request_id`. If a model result is a failed attempt, recovery follows
the same retry/freeze rules as live processing. If a final `send.request` already
exists for the same `turn_id`, late model results and repeated recovery paths
must not write a second final IM reply.

## Run

```bash
python3 -m im_model_agent.cli --config /path/to/im-model-agent.yaml
```

For real deployments, use Runtime Reconciler to generate configuration and
channels.

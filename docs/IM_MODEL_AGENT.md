# IM Model Agent

[中文版](IM_MODEL_AGENT_cn.md)

The IM Model Agent continuously connects an `im.v1` conversation, the `llm.v1`
model-proxy, and `cmd.v1` local commands. It never accesses an IM provider,
model provider, or shell directly.

## Channels

| Channel | Protocol | Content |
| --- | --- | --- |
| IM | `im.v1` | User input and model output text |
| Model | `llm.v1` | Model requests and results |
| WAL | `agent.wal.v1` | New input references for the next model request |
| Cmd | `cmd.v1` | Commands, results, and output reads |

After a session is initialized, all four Channel ids remain unchanged for its
entire lifetime. Replacing any Channel requires a new `session_id`. The
[Runtime Reconciler](RUNTIME_RECONCILER.md#channel-reconciliation) defines
initialization and binding.

The first version supports P2P only. The Agent principal is the active bot
mapping and `user_principal` is the corresponding active user.

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

Configuration is strict. Channels and tokens are provisioned by deployment
tooling; the Agent validates them without mutation and does not rebind session
Channels.

## Continuous Processing

```text
user messages or tool results enter the queue
  -> WAL references the new events
  -> infer.request / infer.result
       -> non-empty content: send to IM
       -> non-empty tool_calls: execute tools
       -> both may happen
  -> tool results return to the queue
  -> continue with the next model request
  -> wait when there are no new events
```

After an accepted model result is fully parsed and all tool calls validate,
every non-empty string `content` is sent. Null, empty, and JSON-whitespace-only
strings are not sent; other types are invalid. Tool calls do not change that
rule; the WAL protocol defines which result is accepted.

The fixed system prompt requires every assistant response to use non-empty
`content` to give the user current progress or a useful result. For provider
compatibility, the Agent still accepts empty `content`, but logs a warning,
sends no IM text, and continues processing valid tool calls in the same response.

Model text uses the stable ID defined by the WAL protocol, preventing duplicate
delivery when a result is observed again during recovery.

## Tool Calls

When text and tools are returned together, the Agent first confirms the text
send request and then executes the tools. Command results enter the model
through the next WAL record. If a Cmd request exceeds `cmd_result_timeout_ms`
and reconciliation still finds no result, the Agent supplies the timeout to the
model as a failed tool result with `error_code="timeout"`. The same code maps a
command execution `TIMEOUT` returned by cmd-worker. A `cmd.v1` request payload
has no stable ID: the Agent keeps its business correlation key in state and WAL
timeout records, reconciles uncertain publishes by Agent principal and exact
payload, and uses the request's OpenEvent seq as the Cmd task ID. The WAL
protocol defines the complete recovery rules.

The current P2P sync worker writes `send.result` only after a successful provider
send. A failed send leaves the stable `send.request` unfinished and terminates
the sync worker, so it can retry the same request after repair and restart. The
Agent does not publish a second request and does not stop on legacy `FAILED`
results.

## WAL And Recovery

The Agent rebuilds state from OpenEvent history at startup and retries model
failures against the original WAL. The WAL fields, stable IDs, provider-side
concurrency, stale results, blocked state, explicit unblock, and recovery rules
are defined only in [AGENT_WAL_PROTOCOL.md](AGENT_WAL_PROTOCOL.md).

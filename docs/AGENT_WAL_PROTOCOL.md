# Agent WAL Protocol agent.wal.v1

[中文版](AGENT_WAL_PROTOCOL_cn.md)

> Status: draft
> Scope: payload and Channel description for `protocol="agent.wal.v1"`

## 1. Document Responsibility

This document is the sole protocol authority for `agent.wal.v1` fields, stable
IDs, publish reconciliation, retry/blocked, and recovery semantics. The
continuous Agent overview is documented in [IM_MODEL_AGENT.md](IM_MODEL_AGENT.md).

## 2. Channel

Each Agent session owns one WAL Channel with `protocol="agent.wal.v1"`. The WAL
records the new facts consumed by the next model request. It does not store the
model request body or command output bodies.

```json
{
  "version": "v1",
  "session_id": "agent-session-001",
  "im_channel_id": 10001,
  "model_channel_id": 20001,
  "updated_at_ms": 1710000000000,
  "metadata": {}
}
```

`private` visibility is recommended. The Agent principal needs read and write
access. Other workers do not consume this Channel.

This protocol assumes that a `session_id`, once initialized, is permanently
bound to the same IM, Model, WAL, and Cmd Channels. All session-scoped message
references are interpreted within that fixed binding. Replacing any Channel
requires a new `session_id`. The [Runtime Reconciler](RUNTIME_RECONCILER.md#channel-reconciliation)
defines the binding lifecycle.

## 3. Payload

The protocol defines `llm.request.prepare` and `cmd.request.timeout`. The prepare payload is:

```json
{
  "kind": "llm.request.prepare",
  "prepare_id": "prepare:agent-session-001:9d70c7",
  "ts_ms": 1710000000000,
  "pre_llm_seq": 12340,
  "user_message_seqs": [],
  "input_event_refs": [
    {
      "type": "exec_result",
      "seq": 12350
    }
  ]
}
```

- `kind`: required and fixed to `llm.request.prepare`.
- `prepare_id`: required, non-empty, and unique within the WAL Channel for this exact set of new model inputs.
- `ts_ms`: required Unix timestamp in milliseconds.
- `pre_llm_seq`: required and equal to the latest `infer.request.seq` in this session before the WAL, or `0` initially.
- `user_message_seqs`: required array of new user `sync.record.seq` values; empty when absent.
- `input_event_refs`: required array of new tool results; empty when absent.

At least one input array is non-empty. Both arrays are strictly increasing by
seq and contain no duplicates.

Each `input_event_refs` item strictly contains:

| Field | Rule |
| --- | --- |
| `type` | `exec_result`, `read_stdout_result`, `read_stderr_result`, or `cmd_timeout` |
| `seq` | OpenEvent seq of the matching Cmd result or WAL `cmd.request.timeout` |

References must belong to the configured IM/Cmd/WAL Channel for this session and
pass relationship validation such as `prev_seq`, `target_seq`, and
`cmd_request_id`. Every referenced seq precedes the WAL seq, and a reference
already consumed by another canonical prepare cannot be reused. Unknown or
malformed fields are invalid.

## 4. Prepare ID

The Agent generates `prepare_id` before the first publish and keeps it unchanged
across transport retries. It identifies only this exact input set. It does not
represent a task, phase, start, or completion. Different input sets use
different IDs; retries of the same logical publish reuse the ID and payload.

The model request uses:

```text
model_request_id = "agent:{session_id}:wal:{wal_seq}:retry:{retry_index}"
```

One WAL record fixes the new input references consumed by the request and may
map to multiple model retries. `retry_index` starts at `1`. Each retry is built
strictly from those references and the accepted context preceding the WAL;
later queued events are not included. Request bodies need not be byte-for-byte
identical, and retries do not create another WAL record.

## 5. Publish Reconciliation

1. Record `max_seq` with `GetStatus` before publishing.
2. Call `PublishAutoSeq` once.
3. On success, use the returned seq.
4. On an uncertain result, obtain a fixed reconciliation watermark and fully scan the interval with `Fetch(channels=[wal_channel_id])`.
5. Reuse the smallest matching seq when ID and content match; conflicting content is a consistency error.
6. Republish the same ID and content only after the complete scan confirms absence.

Model and IM requests use the same stable-ID method. A `cmd.v1` request payload
has no stable ID. Before publishing it, the Agent fixes the complete payload. On
an uncertain result, it scans the full watermark interval and accepts only a
request from the Agent principal with exactly that payload. It republishes the
same payload only after the complete scan confirms absence. Looking only at the
latest Channel message is insufficient.

## 6. Model Text and Tools

Each non-empty model `content` uses an independent IM deduplication key:

```text
send.request.request_id = "model-content:{model_request_id}"
```

This applies whether or not the same model result contains tool calls. Empty
`content` does not create an IM message.

Tool calls use:

```text
cmd_request_id = "cmd:{model_request_id}:{tool_call_index}"
```

`cmd_request_id` is an Agent-state and WAL-timeout business correlation key; it
is not a `cmd.v1` payload field. The OpenEvent seq of the actual `cmd.v1` request
is the Cmd task ID. Tool results enter the next WAL through `input_event_refs`.
These relationships identify concrete messages and calls, not a higher-level
task state.

A Cmd request timeout event is written to the current session's WAL Channel:

```json
{
  "kind": "cmd.request.timeout",
  "timeout_id": "cmd-timeout:cmd:agent:agent-session-001:wal:12345:retry:1:0",
  "ts_ms": 1710000300000,
  "cmd_request_id": "cmd:agent:agent-session-001:wal:12345:retry:1:0",
  "cmd_request_seq": 12350,
  "tool_call_id": "call_exec_1",
  "tool_name": "exec"
}
```

`timeout_id = "cmd-timeout:{cmd_request_id}"` is unique in the WAL Channel.
`cmd_request_seq` must reference the matching request in this session's Cmd
Channel; `tool_call_id` and `tool_name` must match the accepted assistant tool
call. After `agent.cmd_result_timeout_ms`, the Agent records a deterministic Cmd
Channel watermark and Fetches through it. It publishes the timeout event using
the reconciliation algorithm in section 5 only when no result exists through
that watermark. Conflicting content under the same ID is a consistency error.

The timeout maps to the matching `role=tool` result with `status="error"`,
`error_code="timeout"`, and an `error_message` that says waiting for the Cmd
result timed out. Once published, it is the only accepted result for that tool
call. A later real Cmd result is marked late and does not enter the prompt. If
reconciliation finds the real result first, no timeout event is published.

## 7. Retry

- The next retry is published only after the current retry explicitly fails or exceeds the Agent wait timeout.
- Each retry has a new request ID and is a new model call to model-proxy.
- A timeout only means that the Agent no longer waits for the older request; it
  does not require model-proxy or the provider to have stopped processing it.
  Multiple model calls may therefore execute concurrently at the provider.
- Concurrent model calls may add token, rate-limit, and capacity cost, but must
  not change Agent state semantics. Model inference itself has no business side
  effects. Only the valid result for the highest current `retry_index` may
  produce IM, Cmd, or prompt side effects.
- Once a newer retry is published, all later results from lower indexes are stale and produce no text, tools, or prompt changes.
- An uncertain retry publish is reconciled by its request ID before another index may be allocated.
- At `max_model_attempts`, the WAL remains blocked. Its referenced input and later queued events cannot be skipped.
- Explicit unblock continues from the next index after the historical maximum.

## 8. Recovery

- Duplicate records with identical `prepare_id` and payload use the smallest seq.
- The same `prepare_id` with different content is a consistency error.
- A mismatched `pre_llm_seq`, session ownership, reference type, causal
  relationship, or reference uniqueness is a consistency error.
- A WAL record without a model request is rebuilt from exact input references.
- A current model request without a result waits until timeout, then either publishes the next retry or remains blocked at the configured limit.
- Late results from older retries are stale and have no side effects.
- If the current retry result was fully validated and accepted, missing
  `model-content:{model_request_id}` for its non-empty content is reconciled and published.
- If the current retry result was fully validated and accepted, missing Cmd
  requests are restored in tool order from deterministic payloads. Recovery
  associates existing requests with `cmd_request_id` by the same order and
  payload validation.
- Tool results not covered by a later WAL return to the session input queue.
- A Cmd request without a result waits before its deadline; after the deadline the
  Agent reconciles history and publishes `cmd.request.timeout` if no result exists.
- A `cmd.request.timeout` not covered by a later prepare returns to the session
  input queue as `cmd_timeout`.

Recovery never decides that a unit of work started, completed, or closed.

## 9. Versioning

- `agent.wal.v1` uses a strict schema.
- This is still a draft and is not compatible with earlier draft data.
- Breaking changes after stable release require a new protocol version.

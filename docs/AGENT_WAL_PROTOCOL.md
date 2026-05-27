# Agent WAL Protocol agent.wal.v1

[中文版](AGENT_WAL_PROTOCOL_cn.md)

> Status: draft
> Scope: Agent write-ahead log payloads and channel descriptions for OpenEvent
> channels with `protocol="agent.wal.v1"`

## 1. Channel Conventions

Each Agent session MUST own exactly one WAL channel. The WAL channel stores only
the Agent's local advancement intent for that session. It does not carry IM
content, model request bodies, or model results.

All Agent WAL channels MUST set:

```text
protocol = "agent.wal.v1"
```

`description` MUST be a JSON string:

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

Field constraints:

- `version`: currently fixed to `v1`.
- `session_id`: session ID from Agent configuration.
- `im_channel_id`: the `im.v1` channel bound to this Agent session.
- `model_channel_id`: the `llm.v1` channel bound to this Agent session.
- `updated_at_ms`: millisecond timestamp.
- `metadata`: optional object for static deployment or business-domain
  extension information.

Constraints:

- One Agent session can configure only one WAL channel.
- One WAL channel can belong to only one Agent session.
- `wal_channel_id` MUST be unique within the same Agent process.
- WAL channels should use `private` visibility.
- The Agent principal MUST have read/write permission on the WAL channel.
- IM Sync Worker and model-proxy worker do not consume the WAL channel and do
  not need WAL channel permission.

## 2. Payload Envelope

`agent.wal.v1` payloads are UTF-8 JSON objects. The first version defines one
log kind:

```json
{
  "kind": "llm.request.prepare",
  "ts_ms": 1710000000000,
  "pre_llm_seq": 12340,
  "user_message_seqs": [12345, 12346]
}
```

Common fields:

- `kind`: required; currently fixed to `llm.request.prepare`.
- `ts_ms`: required; Unix millisecond timestamp (UTC).
- `pre_llm_seq`: required; before preparing this LLM request, the OpenEvent
  `seq` of the previous `llm.v1 infer.request` in this session. If this session
  has no previous LLM request, the value is `0`.
- `user_message_seqs`: required; the list of user `im.v1 sync.record.seq`
  values prepared for this LLM request.

Protocol fields are validated strictly. Unknown fields, missing required fields,
or mismatched field types are invalid payloads.

## 3. OpenEvent principal And recipients

`principal` is a top-level OpenEvent Message field and is not stored inside the
`agent.wal.v1` payload.

Rules:

- The OpenEvent `principal` of `llm.request.prepare` MUST be the Agent
  principal.
- The OpenEvent `recipients` of `llm.request.prepare` MUST be an empty array.
- WAL channel visibility and members provide access control; `recipients` is not
  used to express receivers.
- Payloads MUST NOT contain `source_principal`, tokens, API keys, or model
  request bodies.

## 4. llm.request.prepare

`llm.request.prepare` means the Agent is preparing to write a new
`infer.request` to the `llm.v1` channel. It must be written to OpenEvent before
the corresponding `infer.request`.

```json
{
  "kind": "llm.request.prepare",
  "ts_ms": 1710000000000,
  "pre_llm_seq": 12340,
  "user_message_seqs": [12345, 12346]
}
```

Rules:

- `pre_llm_seq` MUST equal the previous `llm.v1 infer.request.seq` confirmed by
  this session before writing the WAL record.
- If the session has no previous `infer.request`, `pre_llm_seq` MUST be `0`.
- `user_message_seqs` MUST be a non-empty array.
- Every element in `user_message_seqs` MUST point to a user `sync.record.seq` in
  this session's IM channel that needs to trigger a model call.
- `user_message_seqs` MUST be strictly increasing by OpenEvent `seq` and MUST
  NOT contain duplicates.
- If one model request merges multiple user messages, `user_message_seqs` MUST
  list all user message seq values in the batch exactly, not only the final
  high-water mark.
- `turn_id` is not a WAL payload field. During recovery, the Agent derives
  `turn_id = "{session_id}:{user_message_seqs[0]}"` from the `session_id` in the
  WAL channel description and the first user message seq in the payload.
- After writing the WAL record, the corresponding `llm.v1
  infer.request.request_id` MUST be generated from the OpenEvent `seq` of that
  WAL message, using the format `agent:{session_id}:wal:{wal_seq}`.
- A new WAL record MUST be written before every new `infer.request` attempt. A
  timeout retry is also a new `infer.request`, so it also requires a new WAL
  record.

## 5. Correlation Chain

The first Agent version uses WAL as the pre-commit point between IM input and
LLM request, but does not use `infer.request.prev_seq` or
`send.request.prev_seq`. Cross-channel correlation is done through `request_id`
conventions:

```text
im sync.record.seq in agent wal user_message_seqs
agent wal.seq => model_request_id = "agent:{session_id}:wal:{wal_seq}"
llm infer.request.request_id == model_request_id
llm infer.result.request_id == model_request_id
llm infer.result.prev_seq -> llm infer.request.seq
im send.request.request_id == "im:{model_request_id}"
im send.result.request_id == im send.request.request_id
im send.result.prev_seq -> im send.request.seq
```

Meaning:

- WAL records the user message seq list prepared for this LLM request and the
  previous LLM request seq observed when the WAL record was written.
- `infer.request` does not set `prev_seq`; the corresponding WAL is recovered
  from the `wal_seq` embedded in `request_id`.
- `send.request` does not set `prev_seq`; it is associated with the selected
  model request through `request_id="im:{model_request_id}"`.
- `infer.result.prev_seq` and `send.result.prev_seq` remain required by their
  respective protocols and only express that a result points to a request in the
  same protocol.

## 6. Recovery Semantics

When the Agent scans the WAL channel after restart:

- If a `llm.request.prepare` exists, but no `infer.request` with
  `request_id == "agent:{session_id}:wal:{wal_seq}"` exists, and the turn has
  not been closed by a later result or IM reply, the Agent MUST rebuild the
  prompt and user message batch from full OpenEvent history and republish the
  corresponding `infer.request` using that WAL record. If it cannot rebuild the
  request, it MUST enter an error state and exit for manual inspection.
- If a `llm.request.prepare` exists and an `infer.request` with
  `request_id == "agent:{session_id}:wal:{wal_seq}"` already exists, the Agent
  MUST continue recovery based on that model request state and MUST NOT create a
  new WAL record for the same attempt.
- If a WAL record's `user_message_seqs` have already been covered by a later
  completed turn and there is no isolated side effect that needs compensation,
  the WAL record can be treated as history and does not trigger republishing.
- If `pre_llm_seq` does not match the previous LLM request seq reconstructed
  from scanned history, the Agent MUST record a consistency error, enter an
  error state, and exit to avoid duplicate advancement caused by concurrent
  Agent processes or configuration mistakes.

WAL expresses only the fact that the Agent is "prepared to submit". Whether the
model request has actually been published is determined only by the existence of
`llm.v1 infer.request.request_id == "agent:{session_id}:wal:{wal_seq}"`.

The Agent design decides how to republish isolated WAL records, but it must stay
within these boundaries:

- If the previous model request referenced by `pre_llm_seq` has the same
  `user_message_seqs`, the current WAL can be treated as the next attempt of the
  same turn.
- If the previous model request referenced by `pre_llm_seq` has different
  `user_message_seqs`, the current WAL represents a new turn.
- If `pre_llm_seq` is `0`, the current WAL represents the first request in the
  session.
- If recovery cannot rebuild the prompt or user message batch from full
  OpenEvent history, the Agent MUST NOT guess the request body. It must enter an
  error state and exit for manual handling.

## 7. Versioning

- `agent.wal.v1` only receives backward-compatible additions.
- Breaking changes use a new channel protocol, such as `agent.wal.v2`.
- The first version defines only `llm.request.prepare`. Future versions may add
  other `kind` values, but must not change the semantics of already defined
  fields.

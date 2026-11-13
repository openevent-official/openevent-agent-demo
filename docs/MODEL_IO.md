# Chat Agent Model Input and Output Format v3

[中文版](MODEL_IO_cn.md)

> Implementation status: model I/O v3 event input, output parsing, asynchronous tools, and context compression are implemented. See the [README](../README.md) for test coverage and external integration limits.
> Model I/O version: `3`.
> Date: 2026-10-27
> Scope: Model-visible events, history summaries and task information, the three tools, and response interpretation.

## 1. Document boundary

This document is the authoritative model input/output contract for prompt authors, model integrators, and Agent users.
Session behavior, answer lifecycles, cancellation,
failure handling, and limits are described in [Chat Agent](CHAT_MODEL_AGENT.md). Deployment, prompt-file loading,
object access, and working directories are described in [Runtime](RUNTIME_RECONCILER.md).
The [WAL protocol](AGENT_WAL_PROTOCOL.md) defines durable fields, stable keys, event sources, ordering cursors,
and compaction checkpoints. This document does not define another scheduler or recovery algorithm.
The underlying `llm.v1`, `chat.v1`, and `cmd.v3` protocols remain owned by their modules;
this contract defines how Agent interprets their facts and presents them to the model.

The model receives only text, attachment metadata, identifiers that associate tasks and answers, and tool results.
Channel IDs, principals, tokens, complete ObjectKeys, WAL keys, retry/recovery state, OpenEvent message UUIDs,
and model request IDs are excluded from input.
`answer_id` is the UUID string of the original answer's `turn_id` and identifies its answer ownership.
`task_id` is a UUID string fixed when preparing a tool, used to associate its original call, task information in the summary,
and final result. Neither identifier is a WAL key. Numeric `exec_id` identifies command output for reading;
numeric `object_id` identifies an attachment for reading.
Numeric `input_id` reuses the positive source seq of the original complete user `turn.single`, solely to associate
a user request with cancellation of an answer to it. These identifiers are not interchangeable and contain no
read credentials. Their use does not expose other internal event seq values or recovery cursors to the model.

## 2. Requests and complete prompts

Ordinary calls use Model Proxy's `POST /v1/chat/completions`, with `stream=true` and `n=1`.
`body.tools` contains the three definitions in section 5. `body.messages` always contains two native messages:

1. `role=system`, whose `content` is the complete system prompt frozen for this input.
2. `role=user`, whose `content` is the JSON serialization of the object in section 3, carrying session facts in source order.

Model **output** continues to use native Chat Completions `content`, `refusal`, and `tool_calls`, which Agent
fully parses, validates, and dispatches. The next **input** converts accepted output and tool facts into section 3
events instead of replaying native `assistant.tool_calls` or `role=tool` messages.
There are no received, queued, or `accepted/pending/queued` tool acknowledgements. A tool-call event records the
actual call, and a result enters the event sequence separately when its outcome is determined.
Input `role=user` is an event container: only `type=user` represents a user message. Other event types retain
their own provenance and are not presented as user instructions.

Loading, configuring, and freezing both complete prompts follows [Runtime prompt files](RUNTIME_RECONCILER.md#prompt-files).
The system file contains the business instructions and instructions for using this contract;
Agent does not implicitly append another fixed protocol prompt.
The supplied defaults are [system.txt](../openevent-stack/prompts/system.txt) and
[context_compression.txt](../openevent-stack/prompts/context_compression.txt).
They can be used directly as prompt-file configuration inputs. When editing business instructions,
retain the required interpretation of events, summaries, tools, and cancellation. These prompts are usage text for
this contract, not another definition of its fields.
Tool definitions describe each tool's purpose and parameters; the system prompt explains context and results
without repeating parameter lists or internal validation procedures.
The deployment supplies the model name and Provider. Provider credentials never enter model messages.
There is no additional tokenizer, output-token option, or Provider-specific option.
Request byte counting and ordinary/compaction request limits are defined by the
[context byte budget](CHAT_MODEL_AGENT.md#context-byte-budget).

Once a model input is prepared, it remains fixed. Failure retries use the same content;
events arriving meanwhile are reserved for the next input.
While tools run in the background, a new user message can produce another model request according to
the session scheduling and trigger rules in [Chat Agent](CHAT_MODEL_AGENT.md#conversations-and-replies).
Agent-generated processing placeholders, tool-completion notices, failure notices, and oversized-message notices are Chat display text, not model-generated
`assistant` events. When a user explicitly replies to one, the reply-reference rules provide its final visible content.
User inputs durably rejected under [Agent's oversized-message rules](CHAT_MODEL_AGENT.md#oversized-user-messages) produce no independent `user` event
and do not enter later compaction material. A later user explicitly quoting one still includes and measures its complete
`reply_context` under section 3.2 as part of the new input, without restoring the original independent event; quoted attachments obtain their source only through that new input.

## 3. Unified input structure

Decoded `role=user.content` contains exactly the following two fields; no other top-level fields are allowed:

| Field | Format and meaning |
| --- | --- |
| `summary` | null when there is no history summary; otherwise the summary-text string from section 4.2 |
| `events` | Nonempty array of retained old events not covered by the history summary and selected new events, in source-event order |

The history summary comes first, followed by consecutive model events after its coverage boundary.
Selection cannot skip an intervening event in favor of a later message.
Durably rejected source messages are not model events; range coverage follows the [WAL protocol](AGENT_WAL_PROTOCOL.md#4-event-references-order-and-compaction-checkpoints).
Input selection, compaction coverage, and fixed watermarks follow the Agent and WAL contracts.
The model follows `events` array order and must not reorder by `time`.
Every event has `type` and an ISO8601 UTC `time` string. Its remaining fields are restricted to the following table.

### 3.1 Event fields and sources

| `type` | Other required fields | Optional fields |
| --- | --- | --- |
| `user` | `input_id`: positive source seq of the original user single; `text`: string; `attachments`: attachment array | Nonempty `reply_context` array |
| `assistant` | `answer_id`: original answer UUID; `text`: model content string | `refusal`: nonempty original refusal text |
| `tool_call` | `answer_id`; `task_id`; `name`: one of the three tools; `arguments`: validated parameter object | None |
| `tool_result` | `task_id`; `result`: one result object from section 6 | None |
| `user_cancelled` | `target_answer_id`: UUID of the original answer cancelled by the user; `target_input_ids`: array of user request IDs that answer addressed | None |

- `user` comes from a complete `turn.single` sent by the configured user. Its `input_id`, ordering position,
  and `time` all come from that source event. Later contexts, compaction, and recovery reuse the same `input_id`;
  no separate input identifier is allocated or persisted.
- `assistant` comes only from fully accepted ordinary model output. Its order and `time` come from that output's
  terminal model source event. `text` preserves the aggregated original `content`; a refusal-only output uses an
  empty string. When both content and refusal exist, they are stored separately.
  Tool-only output with neither content nor refusal does not produce an empty `assistant` text event.
- `tool_call` comes from the same accepted output and uses the same terminal source and timestamp.
  An `assistant` text event from that source comes first, followed by calls in model-array index order.
  `task_id` comes from the corresponding tool preparation record. `arguments` is the validated object,
  preserving original command and path strings rather than nesting the native JSON arguments string again.
  This event records a validated model call; a later `tool_result` describes whether it succeeded.
- `tool_result` associates with the original call through `task_id` and need not be adjacent to it.
  A real result's order and `time` come from the last source event needed to form the complete result.
  Local timeout and cancellation-before-send decisions use the WAL source rules. Saving a decision or rejecting
  its presentation does not move a real result to a later position.
- `user_cancelled.time` comes from the actual user cancellation source event; its ordering and confirmation follow WAL.
  `target_answer_id` identifies the cancelled answer. `target_input_ids` copies the `reply_to_seqs` frozen when that
  answer was prepared, preserving the corresponding user events' `input_id` order. Later messages do not change
  these targets, and adjacency is not used to infer them. The array is nonempty and contains distinct positive integers.
  It identifies the requests addressed by that answer without extending cancellation to other answers or tools.

Each `task_id` identifies exactly one original tool call and one final result. Retries, recovery, and compaction
do not allocate a new task identifier. Provider-native tool-call IDs remain relevant to output parsing and source
record verification; input replay uses stable `task_id` values and does not require the Provider to reuse native
call IDs across answers. Hidden reasoning, Provider metadata, and output fragments that were never fully accepted
do not produce replay events.

### 3.2 User content, attachments, and reply references

Agent handles complete user `turn.single` messages only. `text` joins valid parts in their original order without
adding separators or trimming whitespace. `attachments` preserves source-message order.
`text` may be empty: an attachment-only input is valid. A message with neither text nor attachments is not new user input.
Attachment information belongs only to the message referencing it. While that message remains in history, its metadata accompanies it, without being copied into later unrelated messages.

Each attachment contains exactly `name`, `type`, `nbytes`, and `object_id`. The first three come from object
metadata; `object_id` is the positive ID in the source ObjectKey.
Metadata and the internal ObjectKey mapping must be prepared before model input is submitted.
Attachment content is not downloaded in advance, and a preparation failure cannot silently remove an attachment.
Attachments are not sent as native vision, audio, or other multimodal input; the model reads them as described in section 3.3.
The media-type fallback is `application/octet-stream`, not a value guessed from the filename;
object-reading environment settings are defined by Runtime.
Input contains no object-download credentials, complete ObjectKeys, cache paths, or external storage addresses.

When a user message has reply references, `reply_context` supplies each target's final visible content without exposing its reference seq:

```json
{"reply_context":[{"role":"assistant","status":"completed","text":"日志检查完成。","attachments":[]}]}
```

- Omit the field when there are no references. Otherwise preserve reply-list order and do not recursively expand
  the targets' own replies.
- Every entry contains exactly `role`, `status`, `text`, and `attachments`. `role` is the Agent's mapped `user`
  or `assistant`; `status` is always `completed`. Read reply targets at the effective normal terminal positions
  defined by Chat; cancelled targets cannot be replied to.
- `text` and `attachments` follow this section's rules. The target completed before the user message was created;
  its content and terminal state no longer change. A Chat error end has empty final text and attachments.
- Referenced attachments also require metadata and an internal mapping. Referenced content is context for this
  user message, not an additional new input.

### 3.3 Reading attachment objects through Cmd

The model invokes the application command `openevent-read-object <object_id>` through the existing `exec` tool.
This does not add a fourth tool or a new Cmd action. The project installs the command, and Runtime configures its environment.

- The argument is the attachment's decimal `object_id`, also selected in this call's `exec.object_ids`.
  Agent passes only the selected attachment credentials. The command obtains ObjectKeys and the object-service
  address from its environment; the model does not supply credentials or service addresses.
- Success writes the complete original object bytes to stdout and exits with 0, without adding a filename,
  status text, or encoding wrapper. The tool layer encodes and limits model-visible output under section 6.
- The command writes to stdout only after reading the complete object. An unresolved reference or object-read failure
  exits nonzero and writes a credential-free reason to stderr. A stdout write failure also exits nonzero;
  use the exit status to determine whether the complete object was obtained.
- Every read accesses the immutable object. A redirected working copy may be edited, but those edits do not
  change later reads of the original object under the same ID.
- To continue reading output already produced by the command, call `read_stdout` with the real
  `exec_result.exec_id`; downloading the object again is unnecessary.

```json
{"command":"openevent-read-object 12345","object_ids":[12345]}
```

The [Agent attachment contract](CHAT_MODEL_AGENT.md#model-tools-and-attachments) defines object references and
credential delivery; Runtime defines working-directory settings. A general shell's ordinary filesystem
access is not an attachment-isolation mechanism.

### 3.4 Two consecutive inputs

Complete OpenAI-compatible requests are provided as [the first input](examples/context-input-1.json) and
[the second input](examples/context-input-2.json). They contain complete `messages`, JSON-string `user.content`,
and all three tool definitions; these files are the authoritative examples.

The first input retains user request A and accepted tool call T, then includes user request B, which arrived
while the tool was executing. A carries two attachments; B's unrelated message does not inherit them.
Answer A contains only a tool call, so no empty assistant text event is generated.
The second input preserves the same history prefix and appends the explanation for B and T's actual result.
There is no receipt event between these inputs and no native tool-history exchange to complete.

The [compression request](examples/context-compaction.json) for this history selects only complete old events.
The [ordinary request with a summary](examples/context-with-summary.json) shows the compression model retaining
useful attachment information and discarding obsolete attachment information. Task information appears in summary prose under section 4.1.

## 4. History summaries and task information

Context consists of a history summary followed by original events. Summary prose describes task information still needed,
without attaching a complete tool-call table.
History compaction can cover a complete consecutive prefix of old events, including calls whose results have
not arrived. Fully confirmed output can be compacted directly without first appearing in an ordinary model input.
User messages and normal tool results awaiting processing retain their original events rather than being summarized early;
the WAL protocol below defines the selection boundary. A batch may end between complete events, including between the
assistant and tool_call events from one accepted output. A call and its result need not both be in the compactable range.
Compaction does not reorder or delete source events or change an already frozen model input.
Agent behavior and [WAL checkpoints](AGENT_WAL_PROTOCOL.md#4-event-references-order-and-compaction-checkpoints)
govern batch selection, compaction, failure handling, and recovery;
byte limits and fixed thresholds are defined by the [context byte budget](CHAT_MODEL_AGENT.md#context-byte-budget).

### 4.1 Task information in summaries

Summary prose retains each unfinished task's original `task_id`, its purpose, and information needed for subsequent work.
When user requests, answer ownership, or cancellation matter, preserve their original identifiers and explicit scope.
Later `tool_result` events enter `events` in source order and associate through `task_id`.
A summary describes facts at its fixed boundary; later events may have updated a task's result, so an older summary saying
“awaiting a result” does not establish that the task is still executing.

Commands and arguments may be summarized as task purposes and necessary details; complete commands, all arguments,
and every historical call need not be listed again. Retained identifiers and specific argument values must remain faithful
to their sources and cannot be invented. Completed details no longer useful to subsequent work may be omitted.
Uncovered `tool_call` events still carry their complete original arguments, without truncation inside the event;
their representation after compaction follows this section.

Actual tool arguments, ownership, execution, and cleanup state are always reconstructed from original tool, Cmd,
and cancellation records, never from summary prose. Omitting arguments from a summary changes no execution record,
reruns or cancels no tool, and alters no established call. Model input has no separate call table that must be retransmitted in full.

### 4.2 Summary format and compaction calls

Ordinary and compression inputs use the same `summary` format: null when there is no history summary, otherwise a nonblank summary-text string, without an object wrapper.
For example, a summary that covers a call but not its result is:

```json
"用户请求 301 要检查 app.py 是否可读；回答 10000000-0000-4000-8000-000000000001 已发起任务 20000000-0000-4000-8000-000000000001 执行该检查，截至本摘要尚无结果。"
```

The summary text must be confirmed for adoption, organized around goals, constraints, completed facts, and remaining work.
It must not turn plans or calls into successful results.
Attachment metadata belongs only to the original user event or reply reference that mentions the attachment;
it is not copied into later unrelated messages. The compression model decides which attachment `object_id` values,
names, or purposes to retain in summary prose and may discard information it no longer needs.
The summary has no separate attachment field, and Agent does not reconstruct a full historical attachment catalog;
actual tool credentials still come from the original-message sources defined in section 5.
The summary contains no credentials, WAL keys, or internal execution state. Exact sources remain authoritative
for task identifiers, arguments, cancellation targets, and execution decisions.

The complete compression prompt must require preservation of user goals and constraints, confirmed results,
unfinished work and its `task_id` values, and the explicit targets of user cancellation.
Pending user requests and requests involved in cancellation must retain their `input_id` values and associations.
Calls without results and plans remain unfinished work, not successful outcomes; task IDs cannot be rewritten,
while arguments may be summarized under section 4.1. The compression model only summarizes supplied material: it does not continue the user's task,
make new tool calls, or output anything except summary text, and must follow the explicit size target supplied by this call's `max_summary_bytes`.
The prompt must require combining the adopted summary with the current complete events. Absence of unfinished
work from these new events does not mean it has ended. These prose requirements do not replace original execution
records and decisions retained by the program or require it to restore every argument outside the summary.

Compaction uses the ordinary call's model, Provider, endpoint, `stream=true`, and `n=1`.
`messages` contains only a system message with the complete compression prompt and one user message;
there is no `tools` field. Decoded user.content contains exactly:

```json
{"max_summary_bytes":16384,"summary":null,"events":[{"type":"user","input_id":301,"time":"2026-10-27T08:00:00Z","text":"检查 app.py 是否可读","attachments":[]}]}
```

`max_summary_bytes` is a positive integer giving this call's hard limit for summary prose. The example value `16384` only illustrates the format;
the actual value is calculated under the public budget rules. It measures the encoded-byte increase in the complete request JSON under those rules
when summary text replaces an empty string, including escaping in user.content and the outer JSON. It is not raw UTF-8 bytes, characters,
or tokens. Agent performs exact measurement; the model should write concisely toward this target.
`summary` is the adopted history-summary text or null. `events` is the selected complete continuous prefix of
historical events immediately after its coverage boundary, in original order. Each request selects only a prefix
that fits; it never splits an individual event or JSON string, and never separately compresses
a tool result's text. Accepting a complete summary immediately establishes that prefix's checkpoint and advances by at least one complete event;
each individual batch need not be smaller than the preceding representation.
If more history needs compression, use this summary with the next complete events.
There are no intermediate summaries that must wait for all text fragments before adoption. WAL defines source ranges and recovery checkpoints.
Individual-event admission, the hard target for compressed history, and actual request checks follow the
[context byte budget](CHAT_MODEL_AGENT.md#context-byte-budget). Room for the next compliant input is established
only when the summary and all retained uncompressed history together meet that target; making summary prose shorter alone is insufficient.

Compaction output accepts only non-whitespace text with `finish_reason="stop"`, no tool calls, and no nonempty refusal.
The text itself is the summary; the model does not emit an attachment catalog or task-state table.
It is not displayed in Chat and does not generate ordinary `assistant` or `tool_call` events.
Exceeding this call's `max_summary_bytes` fails the whole attempt; a truncated prefix cannot be accepted instead.
WAL references the accepted text and its source; recovery does not select a different summary.
New user text is not summarized before input. Oversized messages are rejected under Agent admission rules;
oversized tool results use the compact error presentation in section 6.1.

### 4.3 Cancellation and failure facts

User cancellation generates a `user_cancelled` event with explicit `target_answer_id` and `target_input_ids`.
Its target does not depend on adjacent events or old native message groups.
If no complete model output was accepted before cancellation, the target may have no `assistant` text event;
`target_input_ids` still identifies the original requests. No answer is fabricated to complete the history.
An answer triggered solely by tool results also uses its original frozen reply targets and does not incorporate
user messages that arrived meanwhile. An Agent-issued cancellation for failure handling is not a user-cancellation
event. After seeing cancellation, the model must not automatically continue the cancelled answer's task.

Cancellation does not revoke accepted model facts or rewrite determined tool results.
Unsent tools get actual cancellation-before-send results. Sent requests use their confirmed actual results or
timeout decisions; cancelling a paged read early preserves the fact that it started.
Sending kill is not proof of termination. Each tool's unique result remains a `tool_result`; no acknowledgement
is fabricated to complete a pair. The Agent contract determines cancellation scope, which results do not independently
trigger another inference, and how new user messages continue the conversation.

When an ordinary model call ultimately fails, its fixed input and previously accepted facts remain.
Unaccepted partial output is not replayed. A failure notice is Chat display text, not a new user task or a successful answer.
A summary involving cancellation must preserve its explicit target and stopped-work facts; it cannot revoke
cancellation, reassign tasks, or describe already executed command effects as rolled back.

## 5. Schemas for the three tools

Tool names are fixed to `exec`, `read_stdout`, and `read_stderr`; there is no model-side cancellation tool.
The output-reading tools use explicit byte ranges rather than an implicit paging cursor.
Schemas sent to the model omit `maximum`; Agent still validates the uint64 ranges below before accepting tool calls.
Every function-argument object rejects unknown fields. JSON integer excludes boolean.

```json
[
  {
    "type": "function",
    "function": {
      "name": "exec",
      "description": "Execute a non-interactive shell command through Cmd; its actual result is provided later as a tool_result event. The session working directory is the default. Do not use it to send Chat messages or request stdin, interactive programs, or persistent background tasks.",
      "parameters": {
        "type": "object", "additionalProperties": false,
        "properties": {
          "command": {"type": "string", "minLength": 1, "description": "Complete command line without explanatory text."},
          "workdir": {"type": "string", "minLength": 1, "description": "Optional absolute working directory. Omission uses the session working directory."},
          "timeout_ms": {"type": "integer", "minimum": 1, "description": "Optional command timeout in milliseconds."},
          "object_ids": {
            "type": "array", "default": [],
            "items": {"type": "integer", "minimum": 1},
            "description": "Only attachment IDs present in the current input and required by this command. At most 1024 distinct IDs; duplicates are removed in first-occurrence order. Omission or an empty array carries no attachment credentials."
          }
        },
        "required": ["command"]
      }
    }
  },
  {
    "type": "function",
    "function": {
      "name": "read_stdout",
      "description": "Read a stdout byte range associated with an existing exec_result; the actual result is provided later as a tool_result event. exec_id must come from that result. Advance offset explicitly to continue; repeating the same arguments reads the same range.",
      "parameters": {
        "type": "object", "additionalProperties": false,
        "properties": {
          "exec_id": {"type": "integer", "minimum": 1},
          "offset": {"type": "integer", "minimum": 0, "default": 0, "description": "Starting byte position; omission reads from the beginning."},
          "nbytes": {"type": "integer", "minimum": 1, "description": "Required raw byte count. Success reads exactly the smaller of this value and the output allowance; the effective range must exist."}
        },
        "required": ["exec_id", "nbytes"]
      }
    }
  },
  {
    "type": "function",
    "function": {
      "name": "read_stderr",
      "description": "Read a stderr byte range associated with an existing exec_result; the actual result is provided later as a tool_result event. exec_id must come from that result. Advance offset explicitly to continue; repeating the same arguments reads the same range.",
      "parameters": {
        "type": "object", "additionalProperties": false,
        "properties": {
          "exec_id": {"type": "integer", "minimum": 1},
          "offset": {"type": "integer", "minimum": 0, "default": 0, "description": "Starting byte position; omission reads from the beginning."},
          "nbytes": {"type": "integer", "minimum": 1, "description": "Required raw byte count. Success reads exactly the smaller of this value and the output allowance; the effective range must exist."}
        },
        "required": ["exec_id", "nbytes"]
      }
    }
  }
]
```

When `exec.workdir` is omitted, Agent explicitly passes the current session workdir to Cmd rather than relying
on the worker's default directory. Names and arguments are validated against the schemas above;
`workdir` must additionally be an absolute path.
`timeout_ms` and `exec_id` are limited to Cmd's positive uint64 range; out-of-range integers are rejected before publication.
Omitted or empty `exec.object_ids` carries no attachments. Otherwise IDs are deduplicated in first-occurrence
order, retaining at most 1024 distinct positive uint64 values.
Each ID must have an original attachment source within this frozen input: an unsummarized user event or its reply references,
or an original user event or reply reference covered by the adopted summary. Agent obtains credentials from those messages' ObjectKeys,
not from IDs mentioned in summary prose or other text. Compression changes which attachment information the model sees;
it does not revoke original references or cause Agent to list all historical attachments again.
Agent does not infer attachments from shell text, attach all input attachments automatically, or add attachments
from messages that arrived after the model request or from other conversations.
Agent validates every tool's selection before accepting the complete assistant output. An invalid type, count,
or source follows section 7's output-failure rules: the whole output is rejected and none of its tools execute.
The output-reading tools accept no `object_ids` and carry no attachments.
`offset` is an integer from 0 through 18446744073709551615 (the uint64 maximum), defaulting to 0.
`nbytes` is a required integer from 1 through the same maximum, without a default. Out-of-range arguments are rejected before accepting the tool call.
When recording the tool intent, Agent fills in `offset` and freezes `max_tool_output_bytes`.
Configuration changes during recovery do not alter that operation's range or output allowance.
Cmd's contract handles output-read target relationships and permissions; Agent does not duplicate Cmd authorization rules.

## 6. Model-visible tool results

Each `tool_result.result` is one object described below, not a JSON string or a result array.
Stable `task_id` associates the event with its original call; no native tool message or receipt is sent.
First form the complete original result according to this section. Results that fail Agent admission use
the rejected presentation in section 6.1.
Every result requires `type` and `status`; `status` is either `ok` or `error`.
An error requires a nonempty `error_message`, with `error_code` when a machine-readable category is needed.
This contract defines `timeout`, `cancelled`, and `result_too_large`; other errors use readable explanations without guessing execution state.

| `type` | Specific fields |
| --- | --- |
| `exec_result` | Accepted Cmd run results require `exec_id`, `stdout_bytes`, and `stderr_bytes`; original `SUCCESS` or `FAILED` results require the real `exit_code`; may include `stdout` and `stderr` |
| `read_stdout_result` | Requires the original call's `exec_id` and effective `offset` and `nbytes`; success requires `stdout` and `eof` |
| `read_stderr_result` | Requires the original call's `exec_id` and effective `offset` and `nbytes`; success requires `stderr` and `eof` |

`exec_result` does not carry `command`; normal, failed, timed-out, and cancelled results follow the same rule.
The full command remains in the `tool_call.arguments` associated by `task_id` and in its source task record,
rather than being repeated in the result.
Once a call is summarized, summary prose retains task associations and necessary information. Complete arguments
remain in original execution records rather than being attached again to every ordinary input.

`exec_id` is the positive seq of the original `cmd.run.request`, not a result seq, string, or output-file path.
`stdout_bytes` and `stderr_bytes` are nonnegative total sizes observed and recorded by Cmd when it generated
the original run result. They must be present even when long output is not inlined.
The model can use these counts to select ranges. They do not limit later actual reads or guarantee that output
files will remain unchanged.
A timeout while waiting for a run result, or cancellation before execution, supplies neither `exec_id` nor those
counts and explicitly forbids continuing to read that execution's output.
A real Cmd `TIMEOUT` or `KILLED` run result may still provide `exec_id`, expressing the reason with timeout or cancelled.

Accepted Cmd run results are mapped as follows; the Cmd protocol defines the underlying statuses:

| Cmd `status` | Model result `status` | `exit_code` | Error expression |
| --- | --- | --- | --- |
| `SUCCESS` | `ok` | Original 0 | No error explanation |
| `FAILED` | `error` | Original nonzero integer | `error_message` states the actual exit code and retains the existing cause |
| `TIMEOUT` | `error` | Omitted | `error_code="timeout"`, explaining that Cmd reported timeout |
| `KILLED` | `error` | Omitted | `error_code="cancelled"`, explaining that Cmd reported termination |
| `START_FAILED` | `error` | Omitted | Explain that the command did not start and retain the original cause |
| `RECOVERY_FAILED` | `error` | Omitted | Retain Cmd's recovery-failure cause without inferring that the command had no effects |

`exit_code` only forwards a real result's integer; it is never filled with 0 when absent or replaced by an error
message when present. A nonzero exit code does not automatically rerun the command. The model can interpret its
business meaning from the actual exit code.

When Agent's own tool-result wait expires, its accepted timeout decision produces `status="error"`,
`error_code="timeout"`, and an explicit explanation, without an exit code that has not been received.
A run-result wait timeout also supplies no `exec_id`.
The call ends as a failure. A late result does not change it, add an `exec_id`, produce a second `tool_result`,
or introduce a subsequent result-query interface for this failure.
An Agent wait timeout cannot claim that Cmd actually reported `TIMEOUT` or `KILLED`.
The Agent contract separately defines individual RPC retries and process exit; they are not tool-result wait timeouts.

When a result includes `stdout` or `stderr`, its value must be an explicitly encoded object:

```json
{"encoding":"utf-8","content":"hello\n","bytes":6,"truncated":false}
```

- `encoding` is `utf-8` or `base64`; `content` is a string, and `bytes` counts the original bytes it represents.
- UTF-8 content is decoded text; base64 content encodes original bytes and is not itself command text.
- Required boolean `truncated` says whether the model-visible output allowance returned only a prefix of this
  requested range. Inlined `exec_result` output treats the whole stream from offset 0 as its requested range.
  Range reads follow the rules below; `truncated=false` does not mean the entire file was read.
- Empty output uses utf-8, an empty string, 0 bytes, and `truncated=false`.
  An absent output field means that stream was not inlined in this result.
- `exec_result.stdout.bytes` and `exec_result.stderr.bytes` count only actually inlined content and are subject
  to the output allowance. They do not replace the original run result's total `stdout_bytes` and `stderr_bytes`.
- stdout and stderr are handled independently. Cmd output-file internal paths must not be exposed to the model.

`read_stdout`/`read_stderr` start at the given `offset` and, on success, return exactly
`min(nbytes, max_tool_output_bytes)` original bytes.
Agent uses the configured chunk size to issue exact-range Cmd reads, advancing by actual successful byte counts
until the effective range is filled. Failure of any page fails the whole tool read; the previously read prefix
cannot be presented as a successful result.
Agent neither shortens ranges using historical output counts nor infers EOF locally. Empty files, starts at
or past EOF, and effective ranges crossing EOF return tool errors according to actual Cmd results.

Successful `offset` and `nbytes` echo the model's range after filling the default offset;
`stdout.bytes` or `stderr.bytes` is the amount actually returned.
The next position is `offset + bytes`, not the requested count. Repeating the same arguments does not implicitly advance a cursor.
Boolean `eof` comes from the last successful Cmd read page and does not guarantee that no output will be appended later.

- All requested `nbytes` were read but more file content remains: `truncated=false`, `eof=false`; the model may select another range.
- Requested `nbytes` exceeds the output allowance: a successful result has `truncated=true`.
  If the last page ends at EOF, `eof` may also be true.
- Requested `nbytes` fits the allowance and ends exactly at EOF: `truncated=false`, `eof=true`.
- Missing files and read failures map to tool errors. An error result carrying `eof=true` cannot be presented as successful empty output.

Decode every page to original bytes before concatenating in order; do not concatenate base64 strings directly.
Merge before decoding even when a UTF-8 character crosses page boundaries.
If a requested boundary falls inside a UTF-8 character, do not adjust the range. Use base64 when the selected
bytes cannot be completely decoded; do not discard bytes or replace characters silently.

For example, `read_stdout(exec_id=123, offset=65536, nbytes=4096)` reads a range starting at byte 65536.
If it returns 4096 bytes with `eof=false`, the next range starts at 69632 without re-executing the original command.

### 6.1 Rejected presentation of oversized tool results

When a complete tool result fails [Agent admission](CHAT_MODEL_AGENT.md#context-byte-budget), its oversized text
does not enter model history. Agent neither calls a separate compression model nor presents truncated content as complete.
Instead it supplies a fixed compact error, preserving the event's original `task_id`, source, timestamp, and order.
For example, a command that actually succeeded but produced oversized output is presented as:

```json
{
  "type":"exec_result",
  "status":"error",
  "error_code":"result_too_large",
  "error_message":"Tool result is too large to provide. Request a smaller range.",
  "exec_id":701,
  "exit_code":0,
  "stdout_bytes":65536,
  "stderr_bytes":0
}
```

- `status="error"` and `result_too_large` mean the result could not be fully provided to the model, not that Cmd execution failed.
  The actual exit code remains unchanged.
- Omit stdout/stderr content objects and oversized original error text. Use the fixed `error_message` above.
  Preserve known short facts such as real `exec_id`, `exit_code`, `stdout_bytes`, and `stderr_bytes`, and required
  read-result fields such as `offset`, `nbytes`, and `eof`; do not invent facts that were never obtained.
- Original Cmd data remains retained. Do not rerun commands or reread files to substitute a result.
  The model may request a smaller range using the real `exec_id` when exact output is needed.
- The compact error must itself pass admission; startup reserves bounded fields, and fields depending on call arguments are checked using actual parameters before tool acceptance.
  WAL preserves the presentation choice, which is not reselected during recovery; recording it does not move the real result in event order.

## 7. Streaming aggregation and complete validation

Agent first accepts valid messages for the current call under `llm.v1`, then interprets the Provider body.
Protocol termination and model-output validation are separate.
Every append body must be a Chat Completions chunk object within the supported format.
An empty `choices` array, such as a usage-only metadata chunk, contributes no text or tools.
A nonempty array must contain the sole choice 0.
Other choices, hidden reasoning, and response fields not defined here are not forwarded to Chat or input events.
Content, refusal, tool IDs, and argument strings must be UTF-8 encodable; parsed commands and paths are checked as well.
Unencodable characters, such as isolated surrogate code points produced by JSON escapes, cause model-output failure;
characters are not replaced before execution.
Streaming text is validated before it enters the display queue; the full response is validated before accepting
the assistant output or executing tools. Each complete normalized event must also pass
[Agent admission](CHAT_MODEL_AGENT.md#context-byte-budget). Any oversized event rejects the whole attempt and executes no tools;
partial text, a subset of calls, or oversized arguments do not enter model history. Already displayed stream fragments
and failure retries follow the Agent contract.

Text aggregation rules:

- Missing or null `delta.content` adds no text. A present value must be a string; other types and content-part arrays are invalid.
- A nonempty string enters the pending display queue immediately and is appended in order to this attempt's
  assistant content. Empty strings are not displayed. The queue retains the source OpenEvent model-event seq
  for display batching and WAL references; display batches do not change the formal aggregated assistant content.
- Preserve all spaces, newlines, and tabs. A fragment is not discarded because it looks like whitespace.
- Chat placeholders, first-content replacement, subsequent batching, retry resets, publication order, and
  confirmation boundaries follow the Agent behavior and WAL display-source contract.
  Source associations live in WAL; this document neither defines nor depends on Chat-extension source fields.

Explicit refusal rules:

- Only the Provider's explicit `refusal` field identifies refusal. Words such as "cannot" or "sorry" in content are not used to infer it.
- Missing or null streaming `delta.refusal` adds nothing. A present value must be a string, concatenated in arrival order.
  Only a nonempty aggregate is an explicit refusal. Refusal text is not mixed into `content` and is not displayed
  before successful termination and complete validation.
- At a successful model terminal event with `finish_reason="stop"`, nonempty `refusal` is an acceptable answer,
  even without content. It does not trigger failure retries or freezing. It cannot coexist with `tool_calls`;
  that combination is a response-format conflict and no tools execute.
- When refusal and ordinary content coexist, the replayed `assistant` event preserves both original strings
  in `text` and `refusal`, while Chat ultimately displays only the refusal.
  If ordinary text has already been displayed, replace it according to Agent display rules;
  otherwise replace the placeholder. Display the refusal once.
  Accepting refusal ends the answer without tools; subsequent normal user input can still be processed.
- Hidden reasoning, `content_filter` completion reasons, and a rejected `llm.v1` result are not this explicit model refusal.

Tool aggregation rules:

- Present `delta.tool_calls` must be an array. Every element must carry a nonnegative integer `index` used to aggregate this call.
- For the same index, append `id`, `function.name`, and `function.arguments` string fragments separately in arrival order.
- When present, `type` must be `function`. Missing fields mean the fragment adds no value for them;
  missing intermediate fields are not an early final-validation failure.
- Do not parse or execute incomplete arguments. Final tools are ordered by increasing index, which must be consecutive starting at 0.
- Completed IDs must be nonempty and unique; names must be one of the three tools;
  arguments must parse as a JSON object.
- Validate complete arguments strictly under section 5. Any invalid tool rejects the entire assistant output,
  and the other valid tools do not execute.
- Extra outer Provider metadata does not enter the normalized assistant; unknown argument fields remain invalid.

Final acceptance requires the current call's successful model terminal event, complete validation, and confirmation
of still-applicable required Chat display; ordering between cancellation and results follows [Agent Conversations and Replies](CHAT_MODEL_AGENT.md#conversations-and-replies).
HTTP success, receipt of some fragments, or a `finish_reason` alone does not replace
the `llm.v1` terminal event. Choice 0's final completion reason follows these rules:

- Without tool calls, only `finish_reason="stop"` is accepted; with calls, only `finish_reason="tool_calls"` is accepted.
- Intermediate fragments may omit finish_reason or set it to null. A valid nonempty reason must appear before the stream ends.
- Repeating the same reason does not change the result. Conflicting reasons invalidate it;
  content, refusal, or tool arguments cannot be appended after the reason.
- `length`, `content_filter`, legacy `function_call`, unknown values, and a missing reason at termination are not successful results.
- The reason does not replace tool-schema validation. Refusal is accepted under its own rules above;
  hidden reasoning is not visible content.
- Null or empty aggregated content with neither tool calls nor nonempty refusal is not a usable result and
  follows model-failure handling. The input cannot silently be marked answered.
  Nonempty strings containing only whitespace are still preserved as specified above.

After failure, output is reset to the placeholder before a new attempt writes to the same turn only when the
Agent retry rules permit it and the Chat turn remains writable.
Unresolved placeholder writes, failure before the first text fragment, retry limits, cancellation, freezing,
and waking through new user input follow the Agent contract rather than a separate rule here.
A new attempt initializes content, refusal, tool_calls, finish_reason, and all fragment-aggregation state afresh;
no prefix from the old attempt can be joined to new output.
Events are first filtered by model-stream ownership. Late valid fragments from an old attempt cannot enter
Chat or input events through the new aggregator.

A streaming request may legally receive an ordinary JSON response. In that case completed `infer.end.body`
contains the full response, possibly without any append events.
Agent extracts string/null content, string/null refusal, and complete tool_calls from its choice 0 assistant message.
Missing, null, or empty refusal means no explicit refusal; other non-string values are invalid.
The choice's finish_reason undergoes the same final checks, and a missing reason still fails.
No append events does not imply an empty response. The complete body follows the same content/refusal selection
and Agent display rules to form the final display unit, replacing the placeholder or temporary content when needed.
It is displayed once and is not concatenated again with streamed increments.
Display batching and message-size handling follow the Agent behavior and WAL display-source rules.
A rejected result with a body, a failed/interrupted end, or a winning cancel is not successful output from which tools can execute.

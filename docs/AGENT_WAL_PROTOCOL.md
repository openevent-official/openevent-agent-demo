# Agent WAL protocol agent.wal.v5

> Implementation status: the current implementation uses `agent.wal.v5`. See the [README](../README.md) for test coverage and external integration limits.

[中文版本](AGENT_WAL_PROTOCOL_cn.md)

## 1. Scope and Channel

This is the authoritative specification of Chat Agent durable records, fields, and source references. Configuration and
message limits are defined by [Chat Agent](CHAT_MODEL_AGENT.md); deployment resources by [Runtime](RUNTIME_RECONCILER.md).
The `agent.wal.v5` contract freezes an event input for each ordinary answer, executes tools independently, and freezes
compaction sources before recording summary references. WAL stores decisions and sources, without copying complete
model requests, answers, tool output, or summary text.

All Chat conversations in a deployment share one private WAL Channel. Its only member and business-record writer is the
Agent principal. Its protocol is `agent.wal.v5`. Runtime creates this description:

```json
{"version":"v5","updated_at_ms":1790000000000,"metadata":{"deployment_id":"demo"}}
```

A deployment permanently binds its principals and Model, Cmd, and WAL Channels. Conversation records identify their
Chat Channel. Recovery requires retained source WAL, Chat, Model, Cmd events and attachment objects, not merely local caches.
Channel system messages written by OpenEvent are not WAL business records. The shared read entry point routes them by the
system protocol first; see [Agent conversation discovery](CHAT_MODEL_AGENT.md#conversation-discovery).

## 2. Record envelope and stable keys

Each payload is UTF-8 JSON with exactly these envelope fields. Empty data illustrates only the envelope;
actual fields are specified below.

```json
{"version":5,"kind":"answer.prepare","key":"chat:30:answer:0","chat_channel_id":30,"data":{}}
```

| Field | Type and meaning |
| --- | --- |
| `version` | Integer, always `5` |
| `kind` | A record kind listed below |
| `key` | Nonempty string identifying an immutable fact throughout this WAL Channel |
| `chat_channel_id` | Nonnegative integer; `0` for deployment records, actual Channel ID for conversation records |
| `data` | Object containing configuration, decisions, or source references |

Record time and position come from top-level OpenEvent `ts_ms` and `seq`. Event positions in model input are derived from
sources as specified in section 4; the position of the WAL record storing a fact cannot simply replace its source position.
Identical records with the same key, kind, conversation, and data use the earliest seq. Conflicting key reuse, unknown
versions or kinds, invalid envelopes, and foreign writers in WAL business records stop the process.

These abbreviations are used only in the tables, not as additional fields:

| Abbreviation | Complete key or prefix |
| --- | --- |
| `A` | Answer key `chat:<channel_id>:answer:<answer-index>` |
| `C` | Compaction batch key `chat:<channel_id>:compaction:<batch-index>` |
| `P` | Attempt key `<A-or-C>:attempt:<attempt-index>` |
| `T` | Ordinary tool prefix `<P>:tool:<call-index>` |
| `R` | Rejection key `chat:<channel_id>:input:<source-user-seq>:reject` |
| `E` | Effect action key defined in the action table below |

Numbers use decimal notation. Answer and compaction indexes each start at 0 and increase with their respective confirmed
preparation records in this conversation. Attempt indexes start at 0 within their owner; call indexes start at 0 in model
call order within an ordinary attempt. Complete record keys and ownership are defined below; `kind` alone does not
determine whether `:prepare` is appended:

| kind | Complete record key | Ownership or reference constraint |
| --- | --- | --- |
| `deployment` | `deployment:<deployment_id>` | `chat_channel_id=0`; the ID matches `data.deployment_id` |
| `input.reject` | `R` | Source seq in the key equals `data.input_seq` |
| `answer.prepare` | `A` | Owning Chat conversation |
| `compaction.prepare` | `C` | Owning Chat conversation |
| `attempt.prepare` | `P` | `data.owner` is the key's `A` or `C`; `purpose` matches the owner's preparation kind and `number` matches the attempt index |
| `attempt.accept` | `<P>:accept` | `data.attempt=P` |
| `attempt.timeout` | `<P>:timeout` | `data.attempt=P`; this attempt's unique expiry trigger |
| `attempt.fail` | `<P>:fail` | `data.attempt=P` |
| `tool.prepare` | `<T>:prepare` | `data.tool_key=T`; `data.answer` is the ordinary attempt's owning `A` |
| `tool.timeout` | `<T>:timeout` | `data.tool_key=T`; this tool's unique expiry trigger |
| `tool.done` | `<T>:done` | `data.tool_key=T`, referencing `<T>:prepare` |
| `answer.cancel` | `<A>:cancel` | Removing the final `:cancel` gives the owning `answer.prepare` key |
| `answer.freeze` | `<A>:freeze` | Owned by `A`; `notice_seq` identifies its failure notice |
| `answer.wake` | `<A>:wake` | Lifts `<A>:freeze`; `input_seq` identifies the user source |
| `compaction.commit` | `<C>:commit` | `data.attempt` must belong to `C` and have a corresponding `attempt.accept` |
| `effect.prepare` | `<E>:prepare` | Fixed publication intent for action `E` |
| `effect.commit` | `<E>:commit` | Corresponds to `<E>:prepare` and references the committed business event |
| `effect.error` | `<E>:error:<random-UUID-string>` | `data.effect_key=E`; one action may retain multiple publication-evidence records, distinguished by this random suffix rather than a message UUID |

Except for deployment records, the conversation in the key, envelope `chat_channel_id`, and referenced owner must agree.
`T` and `E` are prefixes without separate records at those exact keys; other ownership follows the table's preparation
references. Effect action keys are defined only here:

| Action key `E` | Module and action | Ownership and purpose |
| --- | --- | --- |
| `<A>:start` | Chat `turn.start` | Ordinary answer's placeholder reply |
| `<P>:placeholder` | Chat `turn.reset` | Restore the placeholder before an ordinary retry |
| `<P>:text:<start_seq>` | Chat `turn.reset` or `turn.append` | Ordinary display batch; `start_seq` equals its model `source.start_seq` |
| `<P>:final` | Chat `turn.reset` | Replace display with final text or refusal; restore the placeholder when a complete tool-only response supersedes provisional text |
| `<A>:failure-cancel` | Chat `turn.cancel` | Close an ordinary answer on failure |
| `<A>:notice` | Chat `turn.single` | Separate failure notice whose `turn_id` is the answer's `turn_id` plus `:failure` |
| `<A>:tool-completion` | Chat `turn.reset` | Completion notice for a tool-only reply |
| `<A>:end` | Chat `turn.end` | End an ordinary answer |
| `<R>:notice` | Chat `turn.single` | Rejection notice using that `input.reject.turn_id` |
| `<P>:request` | Model `infer.request` | Request for this ordinary or compaction attempt |
| `<P>:cancel` | Model `infer.cancel` | Cancel the same attempt's committed request |
| `<T>:request:<page-index>` | Cmd `cmd.run.request` or `cmd.output.read.request` | Exec uses only page 0; read pages are numbered consecutively from 0, each with its own intent and confirmation |
| `<T>:kill` | Cmd `cmd.kill.request` | Request termination of this exec tool's actual execution, targeting its run request seq |

The action key's `A`, `P`, `T`, or `R` identifies its owner; Chat actions with a `P` prefix belong only to ordinary attempts.
Action keys and their preparations and confirmations do not change on retry or recovery. Answers and rejections each store
random `turn_id` values; attempts store random `stream_id` values unique within the shared Model Channel.
Tool preparation stores a random UUID string `task_id`, the model-visible task identifier, which is neither an OpenEvent
message UUID nor a Cmd `exec_id`. Rejection does not consume an answer or model-attempt index.

## 3. Data fields

Seq values are global OpenEvent event positions; record keys refer to this WAL. Configuration and parameters contain no credentials.

| kind | data |
| --- | --- |
| `deployment` | `deployment_id`; integer principals `agent`, `user`, `model_principal`, `cmd_principal`; integer Channels `model`, `cmd`, `wal` |
| `input.reject` | `input_seq`: source seq of the rejected user `turn.single`; `reason`: always `message_too_long`; `snapshot`: read and processing cutoffs used for the decision; `turn_id`: stable random ID of the rejection notice |
| `answer.prepare` | `events`: ordered references for all events in this complete input that are not covered by its history summary; `input_through`: event cursor through which this preparation confirms consumption; `summary`: history `compaction.commit` key or null; `snapshot`: read and processing cutoffs from section 4; `reply_to_seqs`: frozen user Chat reply targets; `settings`: ordinary model configuration; `turn_id`; `max_model_attempts` |
| `attempt.prepare` | `purpose`: `answer` or `compaction`; `owner`: the corresponding preparation record key; `number`: attempt index; `stream_id` |
| `attempt.accept` | `attempt`; `request_seq`, `stream_id`, `terminal_seq`: the uniquely accepted model source. Ordinary attempts require full validation and confirmation of still-applicable Chat display; when a later effective cancellation has already closed the reply, omit further display while retaining the earlier result. Compaction attempts validate only the summary, without display, tool registration, or entry into ordinary answer history |
| `attempt.timeout` | `attempt`: expired ordinary or compaction attempt; this record's seq is the expiry trigger position, not a final failure decision |
| `attempt.fail` | `attempt`, positive integer `source_seq`, fixed short user-facing diagnostic string `reason`, boolean `retryable`. `source_seq` references the original model event, `attempt.timeout`, or owning local preparation fact that caused failure; `reason` identifies the failed stage and actual cause. `retryable` is false for local overflow of an already fixed input or an explicit model context-length error; rejection of an unprepared user input uses `input.reject`. Persisted decisions are not revoked by late output or configuration changes |
| `effect.prepare` | Target `channel` and module fields from section 5 |
| `effect.commit` | `seq`, `uuid`: the committed source business event. uuid may be null when the SDK returned seq before the source event was read |
| `effect.error` | `effect_key`; SDK-reported `state` (`COMMITTED`, `NOT_COMMITTED`, or `UNKNOWN`); nullable `uuid` |
| `tool.prepare` | Tool intent and stable `task_id` from section 6 |
| `tool.timeout` | `tool_key`; `request_seq`: the tool's first Cmd request. Every page of a paginated read shares this expiry trigger; the record is not the final tool result |
| `tool.done` | The unique tool-result decision from section 6 |
| `answer.cancel` | `cancel_seq`: effective user Chat cancellation of this answer; recorded after observing cancellation, stopping this answer and its direct tools without claiming cleanup has finished |
| `answer.freeze` | `notice_seq`: confirmed failure-notification single; pauses ordinary inference |
| `answer.wake` | `input_seq`: source event of the user single that passed its own content and size checks and unfreezes inference, strictly after the failure notice; rejected inputs are ineligible |
| `compaction.prepare` | `snapshot`; `settings`: compaction configuration; `summary`: previously adopted history-summary commit key or null; `events`: ordered source references to this batch's complete old events; `range`: `after` and `through` event cursors covering this consecutive old prefix |
| `compaction.commit` | `attempt`: the uniquely accepted compaction attempt for this batch; sources, coverage, and configuration are obtained through that attempt's owning `compaction.prepare` |

Ordinary `settings` contains `model`, nullable `provider`, the complete `system_prompt` text, `context_max_input_bytes`,
the derived positive integer `context_history_max_bytes`, and `io_version=3`. Compaction settings use the complete
`context_compression_prompt` text instead of `system_prompt`, with the other fields unchanged.
`context_max_input_bytes` preserves the user's original configured cap `B`, not the effective cap reduced by current publication capacity.
`context_history_max_bytes` preserves the positive integer target actually used for this preparation, not another user configuration option.
Normally, ordinary and compaction preparations preserve `T` from the same budget rules for single-event admission, the hard history target,
and summary output acceptance. When the effective budget is insufficient, an ordinary preparation may preserve the configured-budget admission
target, while a new compaction preparation still preserves the compression target under actual sending capacity.
Compaction request `max_summary_bytes` comes from this field. The [Agent context byte budget](CHAT_MODEL_AGENT.md#context-byte-budget)
defines the exception and failure fallback for a zero target, without adding budget fields.
Retry and recovery retain the frozen value rather than recomputing it from a current other prompt or publication capacity;
continuing an actual publication still checks current sending limits.
A file path is not a prompt snapshot; recovery cannot reread a current file to replace the
text. The UTF-8 JSON byte budget for the complete request body, compaction output limit, maximum of two attempts per batch,
and waiting-timeout rules are defined by the public Agent guide, without additional parameters or formulas here.

`attempt.fail.reason` is the durable source of the cause shown in a failure notice. It uses short Agent-defined diagnostics,
without copying arbitrary raw model fields. `source_seq` preserves the actual failure source position rather than the later
`attempt.fail.seq`; a local check before publication references the owning preparation record.
When a compaction batch's final failure causes its fallback ordinary answer to fail locally, that answer's
failure record preserves that batch's stage and cause. It neither relabels the current user message as too long nor inherits failures from other historical batches.
The displayed cause and notice wording follow [Agent Conversations and Replies](CHAT_MODEL_AGENT.md#conversations-and-replies);
no separate copy of the notice cause or text is stored.

An ordinary answer's input is fixed when `answer.prepare` is confirmed. All its attempts use the same event references,
summary, consumption cursor, cutoffs, and configuration. Only confirmed `answer.prepare` advances the ordinary input consumption cursor;
rejecting a message, starting compaction, reading messages, or preparing tools does not advance it. `events` contains only older and new events
not covered by the summary. The preceding and new consumption cursors plus original sources determine the newly consumed range;
some non-triggering history within it may already be summarized, so `events` alone cannot reconstruct the entire consumed range.
Ordinary preparation must include at least one unconsumed valid user input or inference-triggering tool result;
covered historical output does not independently trigger ordinary preparation.
Reply targets are selected at preparation and are not recomputed during recovery.

A local failure before publication uses the same `answer.prepare` to preserve candidate sources, reply targets, and settings; only the
[Agent context byte budget](CHAT_MODEL_AGENT.md#context-byte-budget) defines its budget exception. Confirmation advances the consumption cursor as usual;
the corresponding `attempt.prepare` and `attempt.fail` with `retryable=false` do not require a preceding model request.
When the local failure is confirmed and no normal Chat reply was created, save no start, reset, end, or cancel intents for it and no Model request intent; reserved turn_id and stream_id values do not imply publication.
Confirmation of the final failure, the notice's `effect.commit`, and the corresponding `answer.freeze` completes this failure handling
without waiting for a nonexistent normal reply's terminal. The separate failure-notification single cannot stand in for a normal reply's
end/cancel; no completion record or field is added.

`input.reject` applies to one user input that has never entered an `answer.prepare`, after checking that message's own sources and size.
Its `input_seq` must not exceed `snapshot.read_through_seq`; the contiguous processing cutoff may still lag behind it.
A source message cannot have both rejection and model-consumption facts. Conditions and user behavior are defined in
[Oversized User Messages](CHAT_MODEL_AGENT.md#oversized-user-messages). Confirmation durably excludes that independent model event
without advancing the ordinary consumption cursor or changing the processing state of other source messages.
The rejection record copies neither source text, attachments, quoted content, nor another model input or prompt.
Recovery keeps the decision instead of reconsidering it under the current budget. The rejection notice uses the fixed action key
from section 2 and the rejection's original turn. Its `effect.commit` determines completion, without a separate rejection-done record,
`answer.prepare`, model attempt, or freeze record.

Model-stage completion is derived from `attempt.accept` and every `tool.prepare` for that output, without another completion record
or model-visible receipt. Derive closure of the whole reply from the original facts specified in section 6; durable results, failures,
and cancellations are not redecided during recovery. Model and tool scheduling, unfreezing, and cancellation behavior are defined in
[Agent Conversations and Replies](CHAT_MODEL_AGENT.md#conversations-and-replies).

Wait deadlines follow [Agent Conversations and Replies](CHAT_MODEL_AGENT.md#conversations-and-replies), without another stored deadline or timeout configuration copy.
Expiry enters the same event sequence through `attempt.timeout` or `tool.timeout`, then competes with original results, failures,
and effective cancellations by source position. An expiry trigger cannot revoke an earlier result. Recovery reuses an existing
trigger instead of creating another one from a restarted clock. Explicit `exec.timeout_ms` is a command parameter
preserved in tool and Cmd intents; `tool.done.request_seq` references the first request of a paginated read.
Memory-only state without a durable decision is not a recovery fact.

## 4. Event references, order, and compaction checkpoints

Event references identify source records without copying model-visible text. The [model I/O contract](MODEL_IO.md) defines the input format.

| `type` | Reference fields |
| --- | --- |
| `user` | `seq`: source Chat seq of the user's `turn.single` |
| `assistant` | `record`: ordinary `attempt.accept` key |
| `tool_call` | `record`: `tool.prepare` key |
| `tool_result` | `record`: `tool.done` key |
| `cancel` | `record`: `answer.cancel` key |

Event identity comes from `type` and its source reference. Rendering a tool result as a fixed short error does not change identity,
task identifiers, or ordering position. Acceptance of a compaction attempt produces no ordinary `assistant` or `tool_call` event.

An event cursor is `{"seq":123,"index":0}`, ordered first by seq and then by index; null means before the first event.
A `user` uses its source Chat seq. An ordinary `assistant` uses the accepted output's `terminal_seq` with index 0;
`tool_call` events from that output use the same terminal seq with indexes starting at 1 in model call order.
Complete `exec` and `read` results use the last Cmd source event required to form the complete result,
namely `tool.done.result_seq`, with index 0. A `timeout` tool result uses its corresponding `tool.timeout` seq and top-level `ts_ms`, with index 0.
A user `cancel` uses the real Chat cancellation event's seq and top-level `ts_ms`, referenced by `answer.cancel.cancel_seq`, with index 0.
`cancelled` and `read_cancelled` tool results use that same real Chat cancellation source, with index equal to the original tool call index plus 1.
Thus one cancellation presents the user cancellation first, then its direct tools' cancellation results in original call order, with a unique cursor for each event.
`answer.cancel` only stores that fact; its later record position or time cannot replace the source event. Time spent storing,
combining, or summarizing text does not change these positions.

`snapshot` contains exactly `read_through_seq` and `processed_through_seq`. The former bounds the source interval already
scanned by the shared reader and the usable WAL facts. The latter is the contiguous processing cutoff within that interval,
through which decisions, reconstruction, and required record confirmations have finished; it cannot exceed the read cutoff.
A complete result that has arrived but is not yet saved holds its ordering position. Later events cannot pass it into an
input or history summary. The same rule applies to a received model terminal awaiting validation or tool registration.
A tool without a complete result does not reserve an invented future position.

An ordinary input's range is derived from the coverage cursor of its referenced `summary` (null without a summary) and `input_through`,
excluding the former and including the latter; no separate `history` field is stored. A compaction's `range` means `(after, through]`.
For both preparation types, `events` must exactly cover the model events within the selected range
in order, without skipping intervening events. A user source with a confirmed `input.reject` produces no model event;
the rejection evidence must be within that input or compaction's fixed read cutoff. Its notice does not generate an assistant event either.
A later user explicitly quoting the rejected message is reconstructed as reply context in the new user event, without restoring the original independent event.
Summary coverage may temporarily lead the preceding ordinary consumption cursor, but ordinary preparation must include a new trigger after the summary,
so its `input_through` is strictly after the adopted summary's coverage cursor. Ranges cannot exceed the frozen processing cutoff. WAL records confirming an earlier source event may be later than its
ordering seq, but must be within the frozen read cutoff. Reconstruction from these references must produce the same input,
without selecting again using the latest summary or processing progress.

History compaction preserves the sources for a preceding summary plus the next consecutive prefix of complete history.
`range.after` must equal that summary's coverage cursor, or null without a summary. `range.through` may exceed the preceding ordinary consumption cursor,
but cannot exceed the frozen processing cutoff or include or pass the first unconsumed valid user input or inference-triggering tool result in that snapshot.
This permits direct coverage of confirmed historical output and cancellation facts without consuming new tasks early.
Ordinary consumption comes from confirmed `answer.prepare` records within the snapshot; summary coverage comes from confirmed `compaction.commit` records.
Restore them independently, without additional cursor records. The batch's `events` is nonempty and reconstructs complete events under the
[model I/O v3 rules](MODEL_IO.md#42-summary-format-and-compaction-calls). Individual events are never split, and there is no separate tool-result compaction.
Complete events from one accepted output may belong to adjacent compaction batches, located by the existing seq/index cursors;
whole-output acceptance and tool-preparation rules remain unchanged.
Confirmed preparation fixes event sources, range, and configuration. A `compaction.commit` must reference an accepted attempt
that meets the frozen output target, cover at least one complete event, and advance coverage to `range.through`;
ordinary input or the next compaction batch may reference it as `summary`.
Preparation without a commit leaves the adopted summary and coverage unchanged; a batch commit alone does not establish
that the whole history meets its target. Derive overall target attainment from original events, the summary, and the frozen target,
without storing character intervals, intermediate-summary chains, or additional completion flags.
Existing `attempt.fail` records determine a finally failed batch. A later new batch uses a new preparation key and attempt
records even if it selects the same range; old records remain unchanged. Batch selection, retry allowances, failure fallback,
and later processing are defined in the [Agent context byte budget](CHAT_MODEL_AGENT.md#context-byte-budget).

Tool parameters, attachment choices, ownership, result decisions, and actual execution states still reference original tool,
Cmd, cancellation, and Chat records; they cannot be restored from summary text. Summary text and coverage remain fixed,
and original data and object references remain retained. Summary task and attachment information follows
[model I/O](MODEL_IO.md#41-task-information-in-summaries); WAL stores no additional task state, full argument appendix, or attachment catalog.

## 5. Effects and display sources

Besides `channel`, `effect.prepare.data` carries these fields according to `module`:

| module | Fields |
| --- | --- |
| `chat` | Action `kind`, `turn_id`; expected `pre_seq` for append/reset/end, otherwise null; content `source` |
| `model` request | `kind="infer.request"`, `attempt`, `stream_id`; reconstruct fixed input through the attempt's purpose and owner |
| `model` cancel | `kind="infer.cancel"`, `stream_id`, `request_seq` |
| `cmd` | `kind`, `uuid`, `tool_key`, `request` |

An ordinary answer's Chat start and single obtain fixed reply targets from its `answer.prepare`;
a rejection notice single targets the owning `input.reject.input_seq`. Publication intents do not duplicate
`reply_to_seqs`. Append, reset, end, and cancel do not use reply targets. Writers and terminal states recover independently
for each reply; the currently executing model answer cannot replace an older tool's ownership.

Each Cmd request's OpenEvent UUID is allocated and stored only in its own `effect.prepare`; the same rule applies to the first
request, later read pages, and kill requests. Confirm intent before publication, and reuse that UUID on retry or recovery.
Before a publication intent exists, no message UUID is needed and no request may be published.

Chat `source` is null for actions without text, `{"type":"placeholder"}`, `{"type":"failure"}`, `{"type":"tool_completion"}`,
or `{"type":"input_rejection","record":"<input.reject-key>"}`. The last form is valid only for its rejection notice single;
the referenced fixed reason determines the text under the Agent usage contract.
`failure` is valid only for the owning ordinary answer's failure-notification single. Its action key identifies that answer;
reconstruct the cause from the confirmed `attempt.fail.reason` that finally ended the answer.
Confirm that final failure decision before preparing the notice intent. Recovery uses the same decision, without changing the cause
based on current model state or later requests; `source` gains no fields.
`tool_completion` is valid only for the owning answer's tool-completion notice reset; section 2 defines its action key and ownership.
Text and eligibility follow
[Agent Conversations and Replies](CHAT_MODEL_AGENT.md#conversations-and-replies), without another completion flag, text copy, or model attempt.
The Agent defines status-notice wording and cause presentation. Model text sources use:

```json
{"type":"model","attempt":"...","field":"content","start_seq":210,"end_seq":213}
```

A model `source` contains exactly these five fields. Its `attempt` must belong to an ordinary answer; that attempt and its
request commit identify the model stream and original request. `start_seq` and `end_seq` are positive integers with
`start_seq <= end_seq`, both inclusive. The range selects valid events from this attempt only, excluding other Channels and streams.

| `field` | Range and text meaning |
| --- | --- |
| `content` | Endpoints are the first and last model events contributing nonempty incremental text to this batch. Concatenate their complete text contributions in seq order; tool or metadata events contribute no text. Do not split an event or skip intermediate text fragments |
| `display` | From the original model request seq through its successful terminal seq. Reconstruct and fully validate the response; the final text or refusal selected by model I/O replaces display as one unit. An existing `attempt.accept` is not required |

Display action keys are defined in section 2. Ordinary batches cover complete fragments in order without overlaps or omissions. Final replacement
may cover sources already displayed provisionally. Recovery retains recorded ranges and purposes without rebatching.
Text is reconstructed from model sources, not copied into WAL.

Confirm intent before publishing its effect; confirm the business seq and `effect.commit` before dependent work.

## 6. Tool intents, results, and reply closure

`tool.prepare.data` contains:

- `tool_key`, `task_id`, `answer`, `call_id`, `name`, `arguments`: stable task identifiers, direct ownership, and fully validated model parameters.
- `request`: exec stores the resolved fixed `workdir` and payload `ts_ms`; `command` and optional `timeout_ms` come from the same record's `arguments`. Reads store `ts_ms`, `target_seq`, `stream`, and initial `offset`; `nbytes` is added to individual page intents.
- `output_limit`, `chunk_bytes`, `nbytes`: output quota, page size, and model-requested total length, freezing result interpretation.

`task_id` is generated once and reused by later
tool events and result rendering. A real `exec_id` is the committed Cmd run request seq, never a task_id or preallocated substitute.
Attachment IDs selected for exec appear only in `arguments.object_ids`, deduplicated in first-occurrence order; omission or an
empty array means no attachments. Complete ObjectKey credentials are reconstructed from source Chat events, not stored in WAL.
Tool preparation contains no second copy of an OpenEvent message UUID.

`tool.done.data` always contains `tool_key` and `outcome`:

| outcome | Other fields |
| --- | --- |
| `exec` | `request_seq`, `result_seq`; optional `rejection`, allowing only `result_too_large` |
| `read` | First `request_seq`; positive `page_count`; last page's `result_seq`; optional `rejection`, allowing only `result_too_large` |
| `read_cancelled` | First `request_seq`; earlier pages completed, cancellation prevents the next page, and no read request remains in flight |
| `timeout` | `request_seq` |
| `cancelled` | No additional fields; only for undispatched tools |

Read results are reconstructed from per-page request commits and indexed Cmd results. First request, page count, and final
result must agree with the decision; no per-page result array is stored. Read offsets advance by actual returned bytes.
Cancelled and timed-out results contain no unused page lists. Preparation, dispatch, and completion record sizes must all be
checked before accepting ordinary model output.

When a complete result fails model-history admission, retain its real `exec` or `read` sources and fix `rejection="result_too_large"`.
This decision rejects only the model-visible text presentation, replacing it with the [I/O fixed short error](MODEL_IO.md#61-rejected-presentation-of-oversized-tool-results).
Real exec_id, exit code, byte counts, and read positions still come from original records. Do not change command success, delete the original
Cmd result, or rerun the command. No text or error body is copied into WAL and no compaction attempt is added; recovery retains the
original presentation rejection instead of reconsidering it under a larger current budget.

Confirmed `tool.done` is the unique model result, reused on recovery and never replaced by late results.
Result text comes from source events; timeout or undispatched cancellation without a real run result exposes no invented exec_id.

`tool.done` is a business-result decision, not proof that actual execution has ended. Actual execution and cleanup still derive
from the original Cmd requests and real terminal events, retaining original answer ownership. Cancellation, FIFO progression,
deadline decisions, and reply closure follow [Agent Conversations and Replies](CHAT_MODEL_AGENT.md#conversations-and-replies).

Reply closure writes no separate completion record. Except for a local failure with no normal reply created under section 3,
completion is derived jointly from the owning model stage's decision or confirmed cancellation, the original Chat reply's winning
end/cancel, Model stream terminals needed for cancellation, every direct tool's unique result and Cmd terminals for submitted tool requests,
and required in-flight publication reconciliation and actual execution cleanup.
A Chat terminal alone or tool results alone cannot replace the remaining facts. Recovery continues unfinished work from the same
sources without triggering another model continuation. Effects retain `effect.prepare` and `effect.commit` for reconciliation
against their original intents and committed sources; no extra copy of the reply terminal is stored.

## 7. Shutdown and recovery

Shutdown and restart preconditions are defined in [Agent shutdown, restart, and troubleshooting](CHAT_MODEL_AGENT.md#shutdown-restart-and-troubleshooting);
process-control steps are defined in [Runtime startup, updates, and shutdown](RUNTIME_RECONCILER.md#startup-updates-and-shutdown).
WAL stores no separate shutdown summary. Recovery facts come from the records defined here and original events;
unresolved actions retain their original keys, sources, and known publication evidence.

# Chat Agent

> Implementation status: the event-driven conversations, context compression, oversized-message rejection, and configuration described here are implemented. See the [README](../README.md) for test coverage and external integration limits.

[中文版](CHAT_MODEL_AGENT_cn.md)

Chat Agent serves multiple Chat conversations between the configured user and Agent within one deployment.
Conversations have separate input queues, working directories, and model histories, while sharing the deployment's
Model, Cmd, and WAL Channels. See [Runtime Reconciler](RUNTIME_RECONCILER.md) for full deployment setup.
This document specifies Agent configuration and explains user-facing behavior.
The [Agent WAL protocol](AGENT_WAL_PROTOCOL.md) owns persistent fields and source references; the current protocol is `agent.wal.v5`, with model I/O version `3`.
The [model input/output format](MODEL_IO.md) owns inputs, summaries, compression requests, tools, and outputs, with links to default prompts and complete request examples.
Conversations advance through events, with model calls and actual tool execution serialized separately. Waiting for a tool does not prevent a new user message from triggering a model call.
Idle conversations and those awaiting results do not hold worker threads; adding conversations does not increase the worker thread count.

This guide maintains the agreed configuration and usage contract without compatibility branches for old code.
See [Conversation Discovery](#conversation-discovery)
for event reads and the [WAL protocol](AGENT_WAL_PROTOCOL.md) for persistent fields.
Text batches combine complete model fragments without splitting them, and sources use seq ranges.
See [WAL display sources](AGENT_WAL_PROTOCOL.md#5-effects-and-display-sources)
for source fields.

## Installation and Startup

The deployment environment must have compatible installed distributions of `openevent-sdk >= 0.13.0`,
`openevent-modules-chat`, `openevent-model-proxy >= 0.1.1`, `openevent-modules-cmd`, `openevent-view`, and `PyYAML`.
See the [Ubuntu environment guide](UBUNTU_ENVIRONMENT.md). Build and install this project's current source:

```bash
make install PYTHON=python3
chat-model-agent --config /etc/openevent-agent/agent.json
```

Startup checks the installed SDK and module public APIs. A missing, outdated, or source-shadowed OpenEvent SDK fails startup.
Configure model provider credentials in Model Proxy; do not put them in model messages.

## Agent Configuration

Configuration is UTF-8 JSON with version `chat-agent.v1`. Unknown or duplicate fields and invalid types are rejected.
Runtime normally generates it. The following example shows its structure; replace the principal, token, and Channel
values with the actual deployment values:

```json
{
  "version": "chat-agent.v1",
  "deployment_id": "local-chat-agent",
  "openevent": {"target": "127.0.0.1:9527"},
  "agent": {
    "principal": 90002,
    "token": "replace-with-agent-token",
    "user_principal": 10001,
    "system_prompt": "Runtime writes the complete system prompt text here.",
    "context_compression_prompt": "Runtime writes the complete context compression prompt text here.",
    "context_max_input_bytes": 1048576,
    "model": "replace-with-model-name",
    "work_root": "/var/lib/openevent-agent/work"
  },
  "model_proxy": {"principal": 20001},
  "cmd_worker": {"principal": 30001},
  "channels": {"model": 101, "cmd": 102, "wal": 103},
  "rpc": {"max_retries": 3, "timeout_ms": 10000}
}
```

All illustrated top-level and Agent fields are required except `rpc`. `deployment_id` identifies this deployment;
`channels` contains three distinct, already bound auxiliary Channels, not a list of Chat conversations.
Agent and user principals must differ. `system_prompt` is the complete system prompt and may be empty;
`context_compression_prompt` is the complete compression prompt and must contain non-whitespace text.
Agent uses these UTF-8 texts directly, without reading their source files or appending a second built-in protocol prompt.
[Runtime prompt files](RUNTIME_RECONCILER.md#prompt-files) defines file paths and updates.
`context_max_input_bytes` is required, has no default, and is defined below; the example is not a model token window.
Work directory paths must be absolute deployment data paths, independent of a developer workspace.
An independently started Agent must also satisfy the [Runtime work-directory deployment prerequisites](RUNTIME_RECONCILER.md#directory-layout).

`openevent.max_payload_bytes` is optional and defaults to `16777216` (16 MiB), with an integer range of `1..62914560`.
It represents the target OpenEvent server's payload cap. Runtime supplies the server's configured value; independently deployed Agents must also match their target server.
Agent checks actual encoded payload bytes for business content it constructs and WAL records, including Chat text and fixed notices,
ordinary and compaction preparation records, tool preparation/dispatch/completion records, and actual Model and Cmd requests.
Reserve the maximum legal encoded width for identifiers not yet allocated, then separately check the 64 MiB transport limit including attachment credentials and framing.
The Chat SDK constructs the terminal content list and handles size rejection under its public contract; Agent accepts the resulting error end with empty final text and attachments.
The payload cap excludes framing and attachment credentials; it does not count raw text or tool-argument characters.

Display-size checks for model sources use the answer's fixed prepared `turn_id` and reserve the maximum uint64 value for `pre_seq`.
Measure each complete nonempty content fragment as its corresponding reset or append, and a complete response or refusal requiring replacement as a reset, including the full payload and transport framing.
The same source therefore receives the same size decision regardless of the current display tail, cancellation, or restart. Actual publication still checks the complete message again using its real fields.

Optional Agent fields:

| Field | Default | Meaning |
| --- | --- | --- |
| `model_provider` | Unspecified | Provider name used for Model Proxy selection |
| `model_timeout_ms` | `60000` | Shared by normal and compression requests; current result-wait timeout from the original model request's top-level `ts_ms`, in milliseconds |
| `model_retry_interval_ms` | `1000` | Fixed retry interval shared by normal inference and history compression, in positive integer milliseconds; see [Model Retry Interval](#model-retry-interval) |
| `max_model_attempts` | `3` | Maximum attempts for the same normal model input, including the first call; compression uses the fixed allowance in [Context Byte Budget](#context-byte-budget) |
| `max_model_concurrency` | `4` | Maximum normal and compression model attempts this Agent process actively advances and waits on at once |
| `cmd_result_timeout_ms` | `330000` | Current result-wait timeout from the tool request's top-level `ts_ms`, in milliseconds |
| `max_tool_output_bytes` | `32768` | Raw bytes shown to the model per tool result and output stream |
| `output_read_chunk_bytes` | `32768` | Maximum bytes Agent requests from the command service in each read of standard output (stdout) or standard error (stderr) |
| `idle_sleep_ms` | `50` | Idle delay in milliseconds after an event read reaches the tail and returns no messages; reads continue immediately when messages or unread pages remain. Model/tool results and text batching wake on events or their own deadlines, without this delay |

There is no `agent.discovery_interval_ms` field or separate discovery loop; Runtime does not generate this field and
Agent does not accept it.

`max_model_concurrency` controls Agent scheduling slots. Attempts acquire a slot before entering the stage that sends
a model request or continues processing its result. They retain it while waiting for results, confirming
display, and recording the outcome, then release it when attempt processing exits. Actual tool execution holds no slot. Normal calls and compression share this allowance, and each conversation has at most one model attempt under Agent scheduling.
After a restart, existing unfinished requests also wait for a slot before Agent resumes processing them. Their background
tasks may still be running, so this setting does not cap all submitted requests that have yet to finish. Model Proxy
execution concurrency is controlled separately by Runtime's `model.max_concurrency`; see
[Runtime configuration](RUNTIME_RECONCILER.md#configuration). Agent's worker thread count is also independent of this setting.

Numeric options require positive integers, not booleans. `rpc.max_retries` may be zero, meaning no retry after the
initial failure; the per-call timeout must be positive. All Agent RPC retries use a fixed internal interval of
100 milliseconds, matching the current Chat SDK; there is no RPC retry-interval configuration field. Defaults allow three
retries and a 10000-millisecond wait per RPC. RPC retries and model business attempts have separate budgets;
SDK internal retries must not multiply the RPC budget.

### Model Retry Interval

Optional `agent.model_retry_interval_ms` defaults to 1000 milliseconds and applies to both normal inference and history-compression retries.
Wait this fixed interval before the next model attempt only when the failure is retryable and attempts remain.
The initial attempt has no such delay; success, non-retryable failures, and exhausted attempts add no wait either.

Measure the interval from the previous confirmed `attempt.fail` record's top-level `ts_ms`, using the current runtime configuration.
This setting is not frozen in WAL. After a restart, use the original failure time and wait only the remaining duration;
if the interval has elapsed, retry may proceed. Waiting happens only before creating the next attempt. If that attempt's
`attempt.prepare` is already confirmed, recovery continues it directly without waiting again, even if the current interval has increased.
Waiting holds no global model slot and remains responsive to user cancellation and shutdown.

This is a fixed interval. It does not read Provider `Retry-After`, perform exponential backoff, or impose a requests-per-minute (RPM) limit.

## Context Byte Budget

`context_max_input_bytes`, denoted `B`, is a positive integer, excluding booleans.
Measure the complete JSON request content by encoding it with these uniform rules: UTF-8, object keys sorted lexicographically by Unicode code point,
array order preserved, no extra whitespace, non-ASCII characters unescaped, and non-JSON numeric values forbidden.
Both normal and compression user.content use the same rules. Count the system prompt, model messages,
summary, call parameters in events, attachment descriptions, tool definitions, and all other request fields.
This is Agent's logical input measurement. The Model event envelope and transport framing are measured separately.
Both normal and compression requests must fit `B` under these rules and also pass the OpenEvent payload and transport checks above.
This is not a token count or a guarantee that every model accepts the request. No fixed byte-to-token conversion or separate output-token parameter is used.

At runtime, also calculate the largest body that the current Model publication can carry under the measurement rules above.
Reserve enough space for the complete Model event envelope and transport framing, including credentials, so the complete message fits both
the OpenEvent payload allowance and the 64 MiB transport limit. Use `C = min(B, the largest currently publishable body size)` as the effective runtime cap.
`C` is not a new setting and does not replace the configured `B`. A `B` larger than publication capacity is not a configuration conflict and does not prevent startup.

Compute two fixed baselines using the effective runtime cap. These measurement requests are never sent to the model:

- `F_normal`: the complete normal request with its full system prompt, tool definitions, and fixed fields, using `events=[]`;
  take the larger encoded size for `summary=""` and `summary=null`.
- `F_compression`: the complete compression request with its full compression prompt and fixed fields, using `events=[]`;
  take the larger encoded size for `summary=""` and `summary=null`. Temporarily measure `max_summary_bytes` as `C`,
  reserving the target number's encoded width before calculating the real target, without a circular dependency.
- `F = max(F_normal, F_compression)`; `T = floor((C - F) / 2)`. Derive `F` and `T` from the effective runtime cap, without new user settings;
  leave the one remaining byte unused when the difference is odd.

`T` is both the **hard limit for the entire history after compression** and the **limit for one complete input event**.
Actual compression requests carry `max_summary_bytes=T`. History includes the summary text and every remaining uncompressed old event
before the next pending input; checking the summary string alone is insufficient. Summary text uses this same limit, with no separate allowance.

All contributions are measured after final request encoding. For summary text, take the larger increase in a normal or compression
request body when replacing the empty summary string with that text. For an event, take the complete request body's increase when
placing that complete event in an empty `events` array, then conservatively add one byte for an array comma.
Both request forms use the same event encoding. The history charge is the summary contribution plus these charges for every remaining
old event; empty history costs 0. Charge the comma even for a sole event so appending later events cannot introduce unreserved separators.
Both JSON escaping layers for newlines, quotes, and backslashes count; these are not raw UTF-8 text bytes or tokens.
Actual requests without a summary still use null; the empty summary string is only a measurement baseline.

When the effective runtime budget can hold the smallest required events described below, the guarantee follows from
`complete request size <= F + history charge + new event charge <= F + T + T <= C <= B`.
Once history meets its target, **there is room for any one user message or tool result admitted under this effective budget**;
it need not happen to be short. For example, configure `B=100000`, while current publication capacity allows a 60000-byte body and
the recalculated `F=20000`. Then `C=60000` and `T=20000`: up to 20000 bytes of history plus one admitted event of up to 20000 bytes
still fits publication. Do not wait until the request approaches the configured 100000 bytes before compressing.
For actual publication, Agent checks the complete payload prepared by the Model SDK for that model attempt.
Checking, publishing, and retries within that SDK publishing call use the same bytes. The [Model SDK API](https://github.com/openevent-official/openevent-modules-model-proxy/blob/main/docs/SDK_API.md#22-input-models)
defines the public preparation-object contract.
Other Chat, Cmd, and WAL publications retain their own actual-record checks; this formula does not replace their protocol limits.

Every user message, including attachments and reply context, and every normalized model-output event is admitted
against its applicable single-event limit `T`. A model output is still validated and accepted as a whole: if any complete event exceeds its limit,
the entire output fails and none of its tools execute. Multiple individually valid events need not have a combined charge below `T`.
Results of already accepted tools are checked against `min(the owning answer's frozen T, the target derived from current configured B)`.
A reduction in `C` alone does not turn an old tool result into a fatal error; subsequent model requests and compression still obey current `C`.
Oversized tool results use the existing compact error while retaining original results. Complete call parameters remain in original
call events and execution records; after history compaction, no full-parameter table is attached.

When preparing a reply, construct its possible future `user_cancelled` event using the batch's complete reply targets and check `T`.
If it exceeds the limit, shorten the new-event prefix and leave remaining events for later replies. Tool-result-only replies also check
their merged source-user targets. An individual result inherits its original reply's already checked target range; do not wait until
cancellation to discard targets. Before accepting a tool, use its actual arguments to construct the complete compact error it may need,
such as a read result's `exec_id`, `offset`, and `nbytes`, and verify its charge is at most `T`; reserve maximum legal widths for other
bounded fields. History that meets the target can then always fit this error.

Validate the configuration itself using the same rules with `B` alone: the derived admission target must be positive, and a minimal valid
user event (`text="a"`, `attachments=[]`, no reply context) and complete required compact errors must fit. A configuration that cannot
hold these remains a configuration error. Startup does not reject `B` for exceeding OpenEvent publication capacity or precheck fixed notices;
fixed rejection and failure notices check their complete encoding on the actual send path.

If publication capacity alone makes the budget derived from `C` unable to hold the smallest request or required compact errors, do not
reject startup globally or classify every user message as oversized. Temporarily retain the baseline and ordinary input-admission target
derived from configured `B` alone, without treating failure of the conservative budget calculation as a prohibition on model requests.
Check both new and frozen requests using their concrete body and complete encoding: continue when they actually fit, and record a local failure,
notify, and pause only when they still cannot fit. A frozen request is not blocked by baseline calculations using the current new prompt.
Only if the preparation record or notice itself cannot be published does the existing deployment-limit failure and exit rule apply.

This exception does not enlarge compression capacity. Its target is always `max(0, floor((C - F) / 2))`, which equals `T` in normal operation.
Even while ordinary input admission temporarily uses the configuration target, use this actual compression target to decide whether history
needs compression, and preserve it in each new compaction preparation. A zero target or a next complete old event that cannot fit `C` enters
the existing compression-failure fallback. A concrete normal fallback request may still proceed if it fits; otherwise it fails locally.

Configuration-change compatibility checks still use the target derived from the current `B` and prompts. Check individual history events
still needed by the model, the adopted summary, unfinished replies' cancellation targets, and accepted tools' future compact errors;
report incompatibility and exit if they no longer qualify. Also reject a new configuration when an unfinished normal or compression
model stage has a frozen `T` larger than that configuration target. A reduction in publication capacity alone, lowering effective `C` or `T`,
does not reject recovery of the entire Agent. Try compression when history exceeds the runtime target; requests that cannot fit follow local failure handling.
Frozen requests retain their original configuration, sources, and derived-target snapshots as defined by WAL. Already published model requests
and adjudicated sources continue reading results under their original facts; reduced sending capacity does not revoke those results.
Unpublished requests keep their fixed body and sources, and check current actual capacity before continuing publication; an oversized request fails locally.

When preparing the next normal input, first locate the next pending user message or normal tool result and inspect all old history before it.
History at or below the actual compression target above already qualifies. Otherwise compress complete consecutive old events until the summary
plus remaining old events meets that target, then add this new input. Include additional new events only while the complete request stays within effective cap `C`;
unselected events remain queued and cannot displace the one input whose space is guaranteed.
Compression shares the ordinary model, Provider, timeout, and scheduling slots. Each batch selects only a complete prefix that fits the
request; its output summary contribution must not exceed that batch's frozen compression target. Each batch gets at most two attempts, with unchanged input on retry.
Each valid summary can immediately establish a checkpoint, but if the remaining old history still exceeds the actual compression target, continue rather than declaring
the compression target met. Individual batches need not be shorter than their original prefixes; every successful batch must advance coverage
by at least one complete event, and final acceptance uses the hard target for the entire history.
Confirmed historical output can be compacted directly, while unprocessed new user messages and normal tool results retain their original
events; the [WAL compaction checkpoints](AGENT_WAL_PROTOCOL.md#4-event-references-order-and-compaction-checkpoints) define the range.

An oversized summary fails the attempt. Request cancellation as soon as streamed output exceeds the limit; never truncate it into acceptance.
If history cannot meet the target but a normal request built from the last committed summary and uncovered events is still legal, processing may continue with that request.
Otherwise follow the existing model-failure notification and pause flow. Do not revoke committed checkpoints or record an unmet target as successful compression.
Solely to record this local failure before publication, `answer.prepare` may preserve references to a candidate input that still exceeds effective cap `C`
(and possibly `B`). Whether the request can be published still follows the frozen-request checks above. Single-event admission still follows the normal-runtime or insufficient-effective-budget rules.
The preparation record and failure notice must still pass their own publication limits;
see the [WAL protocol](AGENT_WAL_PROTOCOL.md#3-data-fields) for the records.
Keep the attempt records of a batch that has ended in failure. Processing subsequent new input may compress the same history range again;
a paused conversation resumes only through a valid new user message as described below.
The guarantee is that reaching the target leaves room for one admitted input. It does not guarantee a valid summary on every model attempt
or replace the Provider's actual token limits. Raw OpenEvent history, Agent memory, and disk output are outside this model-input budget.
Agent keeps the current summary and uncovered messages with their direct reply context in memory. Covered standalone history bodies
are read from OpenEvent when needed, without a separate historical-body cache. When a new message quotes covered content, that reply
context stays with the new message until the new message is also covered; Chat's reply-position references remain unchanged.
Necessary historical location information and unfinished task state remain available. Compaction neither deletes original OpenEvent
records nor gives the entire process a fixed memory footprint.

## Oversized User Messages

Chat Server checks individual message text at entry, using the limit supplied by [Runtime's Chat configuration](RUNTIME_RECONCILER.md#configuration).
It does not measure model prompts, tools, attachment descriptions, or reply context. Agent therefore checks model input independently;
passing Chat's check does not guarantee that a message fits a model request.

For each unconsumed user message, Agent performs the single-event admission check above, including all attachment descriptions and reply
references; its complete encoded charge must not exceed `T`. Text length alone is insufficient. When the effective runtime budget meets the
minimum requirements above, this limit ensures that the message fits ordinary inference together with history that meets its target,
and a complete compression request together with a summary within its target.

If the event exceeds its limit, Agent uses Chat `single_turn` to reply with exactly **“Message is too long to process.”**,
replying only to that source user message. It does not show a processing placeholder,
call a normal or compression model, consume a model attempt, or freeze the conversation because of this rejection.
The rejection is recorded independently of progress through earlier queued records and does not trigger new inference;
during normal operation, other messages and tools continue under their existing scheduling rules without waiting for the rejection notice to complete.
If the conversation is already paused, a rejected message neither lifts the pause nor wakes queued work;
resume conditions are defined in [Conversations and Replies](#conversations-and-replies).
The user may resend after shortening the text or reducing attachments or reply references. The rejected message stays in Chat history,
but its text, quoted context, and attachments are excluded from later model events and compaction material.
The rejection notice is not a model-generated assistant event either. If a later user message explicitly quotes the rejected message,
that quoted content belongs to the new input and is measured again against the single-event limit: process it if it fits, otherwise reject it.
Quoted text and attachments then occur only through the new input and its later summaries, without reinstating the rejected source as an independent history event.

Persist rejection before sending its notice and reuse existing Chat effect intents and confirmations, so recovery neither handles the message again
nor creates another notice. Fields and consumption boundaries are defined by the [WAL protocol](AGENT_WAL_PROTOCOL.md#3-data-fields).
Reconcile an unknown notice-publication result against that effect intent, without triggering inference or advancing history consumption prematurely.
After restart, the conversation reconciles and completes pending notices for recorded rejections before resuming new-input processing;
progress by later ordinary replies does not establish that earlier rejection notices have completed.
This rejection only prevents model processing of the oversized input;
it does not cancel accepted tools or other replies.

If the single message passes admission but accumulated history makes the real request too large, apply normal context compaction first.
Only when failed compaction still leaves no legal request does the model-failure notification and waiting flow below apply; do not label the current
user message oversized. A context-limit error returned by the Provider also follows model failure, rather than retroactively rejecting
an input already fixed or attempted under this local rule.

## Conversation Discovery

Agent discovers conversations from OpenEvent system messages and adopts every `chat.v1` Channel that the Agent principal has joined.
Creator, visibility, name, member count, and the deployment user principal impose no additional adoption conditions; read access alone does not imply adoption.
User input and cancellation still use the configured `user_principal`. New conversations require no new apply or restart of Agent or workers.

The deployment must add Agent before the first Chat business message, either at Channel creation or immediately afterward.
It must not add Agent to old Channels with business history or remove Agent. The Agent process may be offline and read messages published after joining later.

Every startup restores conversations and unfinished work from retained OpenEvent history before handling new input; replay does not reexecute completed actions.
Discovery and message reception share Fetch without a separate discovery interval or conversation list. Fetch also reads business data from other readable Channels,
so it does not promise lower network traffic. For shutdown, restart, and history-retention requirements, see [Shutdown, Restart, and Troubleshooting](#shutdown-restart-and-troubleshooting).

## Conversations and Replies

Agent-generated fixed notices and failure reasons use English; user messages, model replies, and original tool output retain their original language.

User input consists only of complete Chat `turn.single` messages, including attachment-only messages.
Agent replies still stream: “Processing...” appears first, model text replaces it, and later text appends. A tool-only model reply is valid.
If the finally accepted output contains only tool calls, with no text or refusal, wait until that reply's direct tools all have results
and necessary cleanup is complete. If the reply is still open, replace the placeholder with “Tool processing has ended.” before ending it.
Keep existing model text, and never rewrite a cancelled or already ended reply. This notice does not claim command success or completion of the user's entire task,
and it does not enter later history as a model answer.
New user messages and real tool results enter the conversation in their original OpenEvent event order. Logs never move results next to an earlier tool call.
Model results, failures, effective cancellations, and expiry triggers also determine outcomes in original event-seq order; normal execution and recovery use the same rule.
When a model terminal or definite failure source precedes cancellation, preserve that outcome first; the later cancellation only stops undispatched actions and cleans up in-flight work.
When cancellation precedes the model source, do not accept later output. A cancelled reply receives no further text, but its earlier established result is retained.
Each normal input consists of the complete system prompt, a committed summary, event history after that summary,
and a contiguous prefix of new events. Summary text retains necessary task identifiers, goals, results, and unfinished work;
the compression model may summarize command parameters rather than retransmitting a complete call table.
Later results enter `events` in order and are associated with the original task identifiers. Execution, cancellation, and recovery still use exact
original records, never summary prose as an execution instruction or a replacement for task state.
See the [request examples](examples/README.md) for the complete format and a comparison of two consecutive calls.

Each conversation has at most one model attempt, including compression. Actual tools execute FIFO in accepted order,
with at most one actual tool execution slot per conversation. Native `tool_calls` returned by the model drive dispatch and
then enter history as events. Agent does not fabricate native tool acknowledgements such as accepted, queued, or pending,
and the next model request need not wait for every native call/result pair. Real results still reference their original calls.
For example, while A's tool runs, B's complete user message can trigger another model call; the model combines event history
with task information preserved in the summary to understand progress. Tools produced by the new call join the same queue without overlapping the running tool.

Multiple replies may await their own tools, retaining separate tool ownership, reply targets, and cancellation scope.
Cancellation affects only the selected reply: undispatched tools are withheld and submitted commands receive cleanup requests;
other replies and new user messages may continue. Sending kill, reaching a wait timeout, or recording a model-visible tool failure
does not prove execution stopped. Release the actual tool slot only after confirming a real Cmd terminal event.
A late terminal event ends actual execution occupancy without rewriting a confirmed timeout/cancellation decision or rerunning commands.
Cancelled chunked output reads publish no next chunk and finish cleanup according to the actual in-flight request.

Input preparation freezes the summary reference and its coverage boundary, event range, and prompt text. Retries and restarts neither
insert later events nor reinterpret prepared requests after prompt-file updates. See the [WAL protocol](AGENT_WAL_PROTOCOL.md) for ranges and links.
New complete user messages and final tool results outside user-cancellation cleanup trigger work in the order above, including
tool success, business failure, and wait timeout. Otherwise Agent waits;
synthetic tool acknowledgements do not drive the loop. Compression replaces only the old-event representation read by the next model call;
it never deletes source events or changes actual tool state and ownership.

Retryable normal model failures use `max_model_attempts`; compression uses the fixed allowance above.
Exhausted attempts, an explicit Provider context-limit error, or inability to prepare legal input after history compaction preserve the final failure reason
and publish one separate Chat single using the answer's fixed reply targets. Its text is “Processing failed: {reason}. This conversation is paused. Send a new message to retry or revise your request.”
Use an Agent-defined fixed short English diagnostic identifying the failed stage and actual cause, such as “Normal inference timed out waiting for the model response”,
“History compression timed out waiting for the model response”, “History compression produced a summary that exceeds the size limit”,
or “Normal inference returned invalid tool arguments”. Do not show only a generic model-call failure
or directly forward an entire service error or model output.
When a compression request exceeds its size limit before publication, report “The history compression request exceeds the size limit”;
report an oversized summary only when the model's returned summary exceeds its limit.
If compression failure also prevents sending a normal request, preserve the specific compression cause,
rather than calling the current user message oversized. Reconstruct the notice from the persisted final failure reason.
If the local failure is determined before creating a normal reply, send that failure notice directly, without adding a processing placeholder and then cancelling it or creating an empty reply.
This failure handling ends after confirmation of the notice and pause records. Recovery after a confirmed failure decision only completes missing records or the notice; it does not wait to close a normal reply that was never created.
When the final failure source precedes effective cancellation, complete the failure notice and pause as well as cancellation cleanup; cancellation first prevents a later model failure from creating a new pause.
The conversation pauses new normal inference so the user can decide whether to retry or adjust the request. Check each new user message's own content and size first;
only the first passing `turn.single` in the checked prefix whose source follows the failure notification can lift the pause. Messages arriving after that check wait for their own admission check.
Local rejection of a single user message follows the [section above](#oversized-user-messages); it neither introduces a freeze nor lifts an existing pause.
Previously queued inputs and tool results cannot lift this wait on their own.
Accepted tools continue executing and recording results; open replies can still be cancelled separately. Model failure never automatically revokes or reruns tools.
After new user input lifts the wait, processing continues in event order. If compression is needed, it may create a new batch covering the same range as a failed batch.
The new batch has its own attempt allowance, while the old failure records remain unchanged. Restarting alone neither lifts the pause nor restarts a failed batch.
An input that cannot fit is not repeatedly sent. Explicit refusals are displayed without retrying.
Agent never silently truncates an individual user message, attachment description, necessary tool result, or tool argument to fit the limit.
If any complete event in a model output fails admission, reject the entire output and execute none of its tools;
already displayed stream fragments follow the existing failure and retry flow.
When an actual tool result fails admission, retain the original result and provide the fixed `result_too_large` error
with accurate short fields, as defined by [oversized tool-result presentation](MODEL_IO.md#61-rejected-presentation-of-oversized-tool-results).
Do not call a separate compression model or rerun the tool.
Encoded upper bounds for tool calls and their records, requests, and results are still checked before execution.
If even a preparation record or fixed Chat reply cannot be published, Agent reports an error and exits.

Exhausted infrastructure RPC retries exit the entire Agent process; tool business failures do not.
Normal-model, compression-model, and tool wait deadlines use the original request's OpenEvent top-level `ts_ms` plus the current corresponding timeout,
with all pages sharing the first request's deadline and no restarted clock during recovery. Expiry first records one trigger,
whose seq is compared with the source seqs of results and cancellation. An earlier persisted result does not become a timeout because processing, display, or persistence is slow.
Trigger records and source fields are defined in the [WAL protocol](AGENT_WAL_PROTOCOL.md#3-data-fields).
Wait-timeout configuration is not frozen; configuration changes never reverse confirmed acceptance, timeout, or failure decisions.

## Model Tools and Attachments

The model receives exactly three tools:

| Tool | Parameters and purpose |
| --- | --- |
| `exec` | Required `command`; optional absolute `workdir`, positive integer `timeout_ms`, and `object_ids` for attachments needed by this call. An omitted directory uses the current conversation directory; omitted attachment IDs pass no attachment credentials. |
| `read_stdout` | Required numeric `exec_id` from an existing result and positive integer `nbytes`; optional `offset` specifies an explicit stdout byte range. |
| `read_stderr` | The same parameters, for stderr. |

Range reads default to `offset=0` with no implicit cursor. A successful read returns exactly the smaller of `nbytes`
and that operation's output allowance. The effective range must exist; empty files or ranges past the file end return tool errors.
The next offset is the returned `offset + bytes`. `eof` indicates the observed end of the file; `truncated` indicates that
the allowance prevented completing this range. Both can be true. Output is UTF-8 text when all bytes decode successfully
and base64 otherwise, without dropping bytes. Output allowances count original bytes and do not replace admission checks;
even output within its allowance may be too large after full encoding, in which case the
[result-rejection rules](MODEL_IO.md#61-rejected-presentation-of-oversized-tool-results) supply a compact error.
Execution results preserve the actual exit code and expose total byte
counts `stdout_bytes` and `stderr_bytes` observed when Cmd generated the run result so the model can choose read ranges, including when
long output is not inlined. A wait timeout without an actual run result exposes neither `exec_id` nor these counts.
Execution results link to their original calls through recorded tool identifiers and enter later input as real result events,
without placeholder acknowledgements completing a native tool dialogue. Commands remain in call arguments; neither successful nor error results include `command`.

Only the user message or reply reference mentioning an attachment contains its name, media type, byte count, and `object_id`,
without ObjectKey credentials or a pre-downloaded path. While the original message remains in the event history,
its metadata accompanies it; later unrelated messages do not inherit that metadata. During compression, the model
decides which attachment information to retain in summary prose and which to discard. Agent neither maintains nor resends a full historical attachment catalog.
To read contents, the model lists the needed attachment IDs in `exec.object_ids`, for example:

```json
{"command":"openevent-read-object 12345","object_ids":[12345]}
```

`object_ids` can select only original attachments from unsummarized user messages or their reply references in this frozen input,
or original user messages or reply references covered by the adopted summary. IDs are deduplicated in first-occurrence order,
with at most 1024 distinct IDs. Agent obtains credentials through original ObjectKeys, not by generating credentials from summary prose;
later-arriving attachments and those from other conversations are excluded. Summary text controls which attachment information the model sees,
without revoking original references or requiring a new list of every historical attachment.
Omitting the field or passing an empty array passes no attachments;
ordinary commands do not automatically receive all attachment credentials. An invalid selection fails model-output validation,
and none of that response's tools execute. For a working copy, use `openevent-read-object 12345 > attachment.txt` as the command.

On success the command writes the original object bytes to stdout. On failure it exits nonzero and distinguishes
invalid IDs, missing environment or dependencies, unregistered or malformed references, and object service
read failures on stderr, without credentials or internal paths. The command writes to stdout only after reading the complete object;
use the exit status to determine whether it was fully written. A redirected file is an editable copy and does not
change the original object. Agent registers each message's `object_id` and read credentials in memory, restoring and
updating them while reading history, without retaining all historical bodies or writing a separate JSON file, KV store, or database. Agent attaches the credentials
only for attachments selected by this call's `exec.object_ids` to the Cmd run request's top-level `object_keys`. Under its protocol, Cmd passes them to
the command through `OPENEVENT_OBJECT_KEYS`. The command reads the object service directly using these credentials,
without requiring Agent to be online. Runtime sets the object service address
`OPENEVENT_OBJECT_TARGET` in the Cmd worker environment; the model need not supply these
variables or object credentials. The general-purpose shell retains normal file access; this is not filesystem isolation.

## Shutdown, Restart, and Troubleshooting

Agent graceful shutdown stops starting new work and waits for in-flight RPCs and publication confirmations to finish.
Shutdown itself neither initiates business cancellation nor waits for remote model or command execution to end.
For full-stack shutdown steps, effects on running tasks, and restart prerequisites, see
[Runtime startup, updates and shutdown](RUNTIME_RECONCILER.md#startup-updates-and-shutdown).

Fix configuration or dependency errors in the environment. For exhausted RPC retries, inspect Agent and OpenEvent logs.
After a model-call-failed notification, fix provider issues and submit a new complete user message to continue requesting work.
Do not delete WAL history or replace auxiliary Channels to bypass failures.

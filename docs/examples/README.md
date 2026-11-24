# Model request examples

[中文版](README_cn.md)

These files contain complete `POST /v1/chat/completions` request bodies for model I/O v4, matching the current implementation's format. The examples have not been executed against a real external Provider.
The [public model I/O contract](../MODEL_IO.md) defines fields, tools, and compaction formats.

## Two consecutive ordinary inputs

1. [First input](context-input-1.json): the user requested a readability check for `app.py`, followed by comparison with the attached `app-reference.py`; another attachment, `old-notes.txt`, is obsolete. The Agent accepted the check, and before its result was delivered, the user asked what the check means. This new message has no attachments and does not copy attachment information from the earlier message.
2. [Second input](context-input-2.json): the first input's events remain, followed by the model's explanation and the actual tool result, linked to its original call by `task_id`.

These are two consecutive ordinary model calls within a conversation, not its first two calls from an empty history. An earlier model output initiated the check before the first request shown here.
No history compression occurs between them, so `summary` is null in both. The second request supplies the complete current context, not only new events; the tool result still follows the new user message and explanation.

## Compaction and using a summary

3. [History compaction request](context-compaction.json): user request 301 and tool call T from that history form a complete old prefix, supplied directly in the `events` array. There is no previous summary, so `summary` is null. `max_summary_bytes` explicitly supplies this summary's size cap, and the compaction request omits `tools`.
4. [Ordinary request using a summary](context-with-summary.json): shows how the second ordinary input can use a summary followed by later events, assuming a valid summary of that prefix has been obtained. The summary text retains `app-reference.py` and its original `object_id=901` for the later comparison, while discarding the obsolete `old-notes.txt`; there is no separate complete attachment catalog. T's original task identifier, owning answer, and check purpose remain in the summary text so the later result can be matched by `task_id`; the program no longer appends a table of complete command arguments.

The latter two examples are an alternative presentation of the same history. They illustrate the compaction format; they do not claim this short history needs compression or show actual Provider output.
The summary is manually written illustrative text, not a required verbatim model response. It does not cover user 703, the explanation, or the tool result, which remain intact in `events`.
Every compression batch consists of complete, consecutive events. The program chooses a batch within the request budget without cutting JSON, fields, or text. The model decides which attachment information belongs in the summary text, and the program does not rebuild a full file catalog from all history. Keeping one attachment and discarding another illustrates this choice; summary text cannot create attachment provenance or read credentials.

These examples use `context_max_input_bytes=1048576` and assume sufficient default sending capacity, so the effective runtime cap equals the configured cap.
With the current default prompts, tool definitions, and
[budget rules](../CHAT_MODEL_AGENT.md#context-byte-budget), the complete request's fixed baseline is 7461 bytes, deriving
`max_summary_bytes=520557`. The whole history and a single input each receive 520557 bytes, measured as their final request
encoding contributions, with one extra array comma reserved per event. The history target includes summary text and retained old events;
checking only the summary text is insufficient. `7461 + 520557 + 520557 = 1048575 <= 1048576`, so history meeting the target can accept one
complete input at the admission limit. This is derived from configuration, not a separate summary setting; changes to prompts or
tool definitions require recalculation. A batch summary meeting its cap may still require further compression until the whole old history meets the target.

## Default prompts and use

The repository supplies two complete UTF-8 default files:

- [Ordinary system prompt](../../openevent-stack/prompts/system.txt)
- [Context compression prompt](../../openevent-stack/prompts/context_compression.txt)

The system text in ordinary examples exactly matches the first file; the compaction example matches the second, including the final newline. There are no hidden appended instructions or template substitutions.
The deployment spec must still explicitly set `agent.system_prompt_file` and `agent.context_compression_prompt_file`. With `openevent-stack/stack.yaml` as the entry point, use
`./prompts/system.txt` and `./prompts/context_compression.txt`, respectively. See [Runtime prompt files](../RUNTIME_RECONCILER.md#prompt-files) for loading and updates.
Runtime implements loading and configuration generation from these file paths. See [Runtime prompt files](../RUNTIME_RECONCILER.md#prompt-files) for usage.

The outer request follows the [OpenAI Chat Completions request format](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create).
Replace `example-model` with the deployment's model name; it does not identify an available model. Ordinary requests include the same three tool definitions, while compaction requests provide no tools.
The files are indented for readability; budgeting uses the [public measurement rules](../CHAT_MODEL_AGENT.md#context-byte-budget).

`messages[1].content` is a JSON **string**: an ordinary request decodes to only `summary` and `events`; a compaction request
additionally contains the positive integer `max_summary_bytes`. Both use a summary-text string or null for `summary`.
These are not outer API fields.
Here `role=user` is a context container; only inner events with `type=user` represent user messages. `stream=true` requests streamed model output and has no bearing on user messages.
Tool definitions let the model emit new native `tool_calls`; earlier calls and actual results are replayed as events, without tool acceptance receipts.

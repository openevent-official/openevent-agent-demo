"""Encoded Provider request budgets, shared by admission and compaction."""
from __future__ import annotations

from dataclasses import dataclass

from .errors import FailureCode, ModelOutputError
from .prompt import (
    UINT64_MAX, canonical_json, build_model_messages, model_tools,
    parse_tool_call, tool_result_event, user_event,
)
from .tool_results import future_error_results


class BudgetError(ValueError):
    """The configured budget cannot support the required protocol operations."""


INPUT_REJECTION = "Message is too long to process."
_ANSWER_FAILURE_REASONS = {
    FailureCode.TIMEOUT: "Normal inference timed out waiting for the model response",
    FailureCode.CONTEXT_LIMIT: "Normal inference exceeded the model context limit",
    FailureCode.PROVIDER_NOT_CONFIGURED: "Normal inference requested an unconfigured model provider",
    FailureCode.INVALID_TOOL_ARGUMENTS: "Normal inference returned invalid tool arguments",
    FailureCode.INVALID_OUTPUT: "Normal inference returned invalid model output",
    FailureCode.EVENT_TOO_LARGE: "Normal inference returned a complete event that exceeds the size limit",
    FailureCode.DISPLAY_TOO_LARGE: "The normal inference display message exceeds the publication limit",
    FailureCode.TOOL_REQUEST_TOO_LARGE: "The normal inference tool request exceeds the publication limit",
    FailureCode.REQUEST_TOO_LARGE: "The normal inference request exceeds the size limit",
    FailureCode.REQUEST_FAILED: "The model request failed during normal inference",
}
_COMPACTION_FAILURE_REASONS = {
    FailureCode.TIMEOUT: "History compression timed out waiting for the model response",
    FailureCode.CONTEXT_LIMIT: "History compression exceeded the model context limit",
    FailureCode.PROVIDER_NOT_CONFIGURED: "History compression requested an unconfigured model provider",
    FailureCode.EVENT_TOO_LARGE: "History compression produced a summary that exceeds the size limit",
    FailureCode.INVALID_TOOL_ARGUMENTS: "History compression returned an invalid summary",
    FailureCode.INVALID_OUTPUT: "History compression returned an invalid summary",
    FailureCode.DISPLAY_TOO_LARGE: "History compression produced a summary that exceeds the size limit",
    FailureCode.TOOL_REQUEST_TOO_LARGE: "History compression produced a summary that exceeds the size limit",
    FailureCode.REQUEST_TOO_LARGE: "The history compression request exceeds the size limit",
    FailureCode.REQUEST_FAILED: "The model request failed during history compression",
}
FAILURE_REASONS = tuple(dict.fromkeys([
    *_ANSWER_FAILURE_REASONS.values(), *_COMPACTION_FAILURE_REASONS.values(),
]))


def failure_reason(error: ModelOutputError | FailureCode, purpose="answer"):
    """Choose the existing short diagnostic using only the internal category."""
    code = error.code if isinstance(error, ModelOutputError) else error
    reasons = _COMPACTION_FAILURE_REASONS if purpose == "compaction" else _ANSWER_FAILURE_REASONS
    return reasons[code]


def failure_notice(reason):
    return (f"Processing failed: {reason}. This conversation is paused. "
            "Send a new message to retry or revise your request.")


def body_size(body):
    return len(canonical_json(body).encode("utf-8"))


def event_size(event):
    # The event JSON is itself inside user.content. Remove only the outer
    # string delimiters; reserve one array comma even for the first event.
    return body_size(canonical_json(event)) - 2 + 1


def summary_size(text):
    # Both request forms encode summary text through the same two layers.
    return body_size(canonical_json(text)) - body_size(canonical_json(""))


def history_size(summary, events):
    return (summary_size(summary) if summary is not None else 0) + sum(map(event_size, events))


def build_normal_body(settings, summary, events):
    return {"model": settings["model"], "stream": True, "n": 1, "tools": model_tools(),
            "messages": build_model_messages(system_prompt=settings["system_prompt"], summary=summary,
                                              events=events, allow_empty=True)}


def build_compaction_body(settings, summary, events):
    return {"model": settings["model"], "stream": True, "n": 1, "messages": [
        {"role": "system", "content": settings["context_compression_prompt"]},
        {"role": "user", "content": canonical_json({"summary": summary, "events": list(events),
             "max_summary_bytes": settings["context_history_max_bytes"]})}]}


@dataclass(frozen=True)
class ContextBudget:
    limit: int
    fixed: int
    target: int

    @classmethod
    def from_config(cls, agent):
        return cls.from_settings(model=agent.model, system_prompt=agent.system_prompt,
                                 context_compression_prompt=agent.context_compression_prompt,
                                 context_max_input_bytes=agent.context_max_input_bytes)

    @classmethod
    def for_runtime(cls, config, configured):
        from openevent.model_proxy_sdk import InferRequestInput, prepare_payload
        from .display_limits import publication_payload_limit

        agent = config.agent
        empty = prepare_payload(InferRequestInput(stream_id="f" * 36, method="POST", path="/v1/chat/completions",
                                                  body={}, provider=agent.model_provider))
        # Replace the generated timestamp's width with the reserved uint64 width.
        envelope = len(empty.payload) - 2 + len(str(UINT64_MAX)) - len(str(empty.ts_ms))
        limit = max(0, publication_payload_limit(config, config.channels.model,
                    maximum=envelope + configured.limit) - envelope)
        if limit == configured.limit:
            return configured
        try:
            return cls.from_settings(model=agent.model, system_prompt=agent.system_prompt,
                                     context_compression_prompt=agent.context_compression_prompt,
                                     context_max_input_bytes=limit)
        except BudgetError:
            # A small publication allowance must not reject every user input.
            # Keep configured admission; actual requests still use the smaller limit.
            return cls(limit, configured.fixed, configured.target)

    @classmethod
    def from_settings(cls, *, model, system_prompt, context_compression_prompt, context_max_input_bytes):
        limit = context_max_input_bytes
        if type(limit) is not int or not 1 <= limit <= UINT64_MAX:
            raise BudgetError("context_max_input_bytes must be a positive uint64 integer")
        settings = {"model": model, "system_prompt": system_prompt,
                    "context_compression_prompt": context_compression_prompt,
                    "context_history_max_bytes": limit}
        normal = max(body_size(build_normal_body(settings, summary, [])) for summary in (None, ""))
        compacting = max(body_size(build_compaction_body(settings, summary, [])) for summary in (None, ""))
        fixed = max(normal, compacting)
        target = (limit - fixed) // 2
        if target <= 0:
            raise BudgetError("fixed model request leaves no history/input budget")
        result = cls(limit, fixed, target)
        result.check_baseline()
        return result

    def check_baseline(self):
        events = [user_event(UINT64_MAX, UINT64_MAX, "a", [])]
        for name, arguments in (("exec", {"command": "a"}),
                                ("read_stdout", {"exec_id": UINT64_MAX, "offset": 0, "nbytes": 1}),
                                ("read_stderr", {"exec_id": UINT64_MAX, "offset": 0, "nbytes": 1})):
            call = parse_tool_call({"type": "function", "id": "call", "function": {
                "name": name, "arguments": canonical_json(arguments)}})
            events.extend(tool_result_event("f" * 36, UINT64_MAX, value)
                          for value in future_error_results(call))
        if any(event_size(event) > self.target for event in events):
            raise BudgetError("context budget cannot fit a minimal user event and required tool errors")

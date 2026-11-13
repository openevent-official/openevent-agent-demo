import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from chat_model_agent.budget import (
    BudgetError, ContextBudget, FAILURE_REASONS, body_size, event_size, summary_size, history_size,
    build_normal_body, build_compaction_body, failure_notice, failure_reason,
)
from chat_model_agent.config import parse_config
from chat_model_agent.errors import FailureCode, ModelOutputError
from chat_model_agent.prompt import canonical_json, model_tools, user_event
from test_chat_agent_config import configuration


ROOT = Path(__file__).resolve().parents[1]


class BudgetTests(unittest.TestCase):
    def test_budget_admission_works_without_installed_sdks(self):
        # Disable site-packages in a fresh interpreter so prior test imports
        # cannot hide an accidental dependency on a tool execution SDK.
        result = subprocess.run([sys.executable, "-I", "-S", "-B", "-c", """
import sys
sys.path.insert(0, sys.argv[1])
from chat_model_agent.budget import BudgetError, ContextBudget
settings = dict(model="model", system_prompt="system", context_compression_prompt="compress",
                context_max_input_bytes=20000)
budget = ContextBudget.from_settings(**settings)
assert budget.target > 0
try:
    ContextBudget.from_settings(**{**settings, "context_max_input_bytes": budget.fixed + 2})
except BudgetError as error:
    assert "minimal user" in str(error), str(error)
else:
    raise AssertionError("undersized budgets must still be rejected")
""", str(ROOT)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def settings(self, **changes):
        value = {"model": "model", "system_prompt": "full system prompt\n",
                 "context_compression_prompt": "full compaction prompt\n",
                 "context_max_input_bytes": 20000}
        value.update(changes)
        budget = ContextBudget.from_settings(**value)
        value["context_history_max_bytes"] = budget.target
        return value, budget

    def test_exact_public_examples_and_default_target(self):
        normal = json.loads((ROOT / "docs/examples/context-input-1.json").read_text())
        compression = json.loads((ROOT / "docs/examples/context-compaction.json").read_text())
        settings, budget = self.settings(model=normal["model"],
            system_prompt=(ROOT / "openevent-stack/prompts/system.txt").read_text(encoding="utf-8"),
            context_compression_prompt=(ROOT / "openevent-stack/prompts/context_compression.txt").read_text(encoding="utf-8"),
            context_max_input_bytes=1048576)
        self.assertEqual((budget.fixed, budget.target), (7347, 520614))
        for name in ("context-input-1.json", "context-input-2.json", "context-with-summary.json"):
            with self.subTest(example=name):
                example = json.loads((ROOT / "docs/examples" / name).read_text())
                context = json.loads(example["messages"][1]["content"])
                self.assertEqual(build_normal_body(settings, context["summary"], context["events"]), example)
        context = json.loads(compression["messages"][1]["content"])
        self.assertEqual(build_compaction_body(settings, None, context["events"]), compression)
        self.assertEqual(normal["tools"], model_tools())

    def test_event_and_summary_measure_both_json_escape_layers(self):
        settings, _ = self.settings()
        for text in ('hello', '中文é😀', '\n\t\x00', '"\\' * 20):
            with self.subTest(text=text):
                event = user_event(5, 1000, text, [])
                for build in (build_normal_body, build_compaction_body):
                    self.assertEqual(event_size(event),
                                     body_size(build(settings, "", [event])) - body_size(build(settings, "", [])) + 1)
                    self.assertEqual(summary_size(text),
                                     body_size(build(settings, text, [])) - body_size(build(settings, "", [])))
        self.assertGreater(summary_size('"\\'), len('"\\'.encode()))

    def test_normal_and_compaction_requests_share_plain_summary_format(self):
        settings, _ = self.settings()
        events = [user_event(5, 1000, "next input", [])]
        for summary in (None, '旧事实\n"quoted"\\path'):
            with self.subTest(summary=summary):
                for build in (build_normal_body, build_compaction_body):
                    context = json.loads(build(settings, summary, events)["messages"][1]["content"])
                    self.assertEqual(context["summary"], summary)
                    self.assertEqual(context["events"], events)

    def test_target_history_always_leaves_one_full_event(self):
        settings, budget = self.settings()
        incoming = user_event(7, 1000, "", [])
        incoming["text"] = "x" * (budget.target - event_size(incoming))
        summary = "x" * budget.target
        self.assertEqual(event_size(incoming), budget.target)
        self.assertEqual(summary_size(summary), budget.target)
        for build in (build_normal_body, build_compaction_body):
            self.assertLessEqual(body_size(build(settings, summary, [incoming])), budget.limit)
        self.assertEqual(history_size(summary, []), budget.target)
        self.assertEqual(history_size(None, []), 0)
        old = user_event(6, 500, '"\\\n' * 10, [])
        summary = "x" * (budget.target - event_size(old))
        self.assertEqual(history_size(summary, [old]), budget.target)
        self.assertLessEqual(body_size(build_normal_body(settings, summary, [old, incoming])), budget.limit)

    def test_empty_summary_and_larger_compression_prompt_baselines(self):
        settings, budget = self.settings(context_compression_prompt="c" * 12000)
        baseline_settings = {**settings, "context_history_max_bytes": budget.limit}
        expected = max(body_size(build(baseline_settings, summary, []))
                       for build in (build_normal_body, build_compaction_body) for summary in (None, ""))
        self.assertEqual(budget.fixed, expected)
        self.assertEqual(budget.target, (budget.limit - expected) // 2)
        self.assertNotIn("tools", build_compaction_body(settings, None, []))

    def test_inadequate_or_invalid_budget_is_configuration_failure(self):
        for limit in (True, 0, -1, "20000", 1.5, 1 << 64, 100):
            with self.subTest(limit=limit), self.assertRaises(BudgetError):
                self.settings(context_max_input_bytes=limit)
        _, budget = self.settings()
        with self.assertRaisesRegex(BudgetError, "minimal user"):
            self.settings(context_max_input_bytes=budget.fixed + 2)

    def test_runtime_budget_uses_available_publication_space(self):
        from openevent.model_proxy_sdk import InferRequestInput, prepare_payload
        from chat_model_agent.display_limits import publication_fits
        data = configuration()
        data["openevent"]["max_payload_bytes"] = 16384
        config = parse_config(data)
        configured = ContextBudget.from_config(config.agent)
        for transport_limit in (64 * 1024 * 1024, 12000):
            with self.subTest(transport_limit=transport_limit), patch(
                    "chat_model_agent.display_limits.MAX_MESSAGE_BYTES", transport_limit):
                budget = ContextBudget.for_runtime(config)
                self.assertLess(budget.limit, configured.limit)
                self.assertLess(budget.target, configured.target)
                body = {"input": ""}
                body["input"] = "x" * (budget.limit - body_size(body))
                def payload():
                    with patch("openevent.model_proxy_sdk.publishing.time.time_ns", return_value=((1 << 64) - 1) * 1_000_000):
                        return prepare_payload(InferRequestInput(stream_id="f" * 36, method="POST", path="/v1/chat/completions",
                            body=body, provider=config.agent.model_provider)).payload
                self.assertTrue(publication_fits(config, config.channels.model, payload()))
                body["input"] += "x"
                self.assertFalse(publication_fits(config, config.channels.model, payload()))

    def test_model_payload_size_is_body_size_plus_fixed_envelope(self):
        from openevent.model_proxy_sdk import InferRequestInput, prepare_payload
        settings, _ = self.settings()
        def encode(body):
            with patch("openevent.model_proxy_sdk.publishing.time.time_ns", return_value=((1 << 64) - 1) * 1_000_000):
                return prepare_payload(InferRequestInput(stream_id="a" * 36, method="POST", path="/v1/chat/completions",
                                         body=body, provider="模型")).payload
        envelope = len(encode({})) - 2
        for text in ("中文" * 100, "é" * 100, "😀" * 100, "\x7f" * 100, '"\\\x00' * 100):
            for build in (build_normal_body, build_compaction_body):
                with self.subTest(text=text, request=build.__name__):
                    body = build(settings, None, [user_event(5, 1000, text, [])])
                    self.assertEqual(len(encode(body)), envelope + body_size(body))

    def test_short_failure_reasons_never_include_raw_model_diagnostics(self):
        reason = failure_reason(ModelOutputError("private" * 10000, code=FailureCode.INVALID_TOOL_ARGUMENTS))
        self.assertEqual(reason, "Normal inference returned invalid tool arguments")
        self.assertIn(reason, FAILURE_REASONS)
        self.assertEqual(failure_reason(FailureCode.INVALID_TOOL_ARGUMENTS), reason)
        self.assertEqual(failure_reason(FailureCode.TIMEOUT, "compaction"), "History compression timed out waiting for the model response")
        self.assertIn("This conversation is paused.", failure_notice(reason))
        self.assertNotIn("private", failure_notice(reason))


if __name__ == "__main__":
    unittest.main()

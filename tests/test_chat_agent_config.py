from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from chat_model_agent.config import ConfigError, load_config, parse_config
from chat_model_agent.dependencies import DependencyError, require_dependencies, require_sdk


BUILD = Path(__file__).resolve().parents[1] / "build" / "tests"


def configuration():
    return {
        "version": "chat-agent.v1", "deployment_id": "demo",
        "openevent": {"target": "127.0.0.1:9527"},
        "agent": {"principal": 11, "token": "private-agent-token", "user_principal": 12,
                  "system_prompt": "", "context_compression_prompt": "compress history",
                  "context_max_input_bytes": 1048576, "model": "test-model", "work_root": "/var/lib/demo/work"},
        "model_proxy": {"principal": 13}, "cmd_worker": {"principal": 14},
        "channels": {"model": 21, "cmd": 22, "wal": 23},
    }


class ConfigurationTest(unittest.TestCase):
    def test_new_schema_defaults_and_secret_safe_repr(self):
        config = parse_config(configuration())
        self.assertEqual(config.version, "chat-agent.v1")
        self.assertEqual(config.openevent.max_payload_bytes, 16 * 1024 * 1024)
        self.assertEqual(config.agent.model_timeout_ms, 60000)
        self.assertEqual(config.agent.model_retry_interval_ms, 1000)
        self.assertEqual(config.agent.cmd_result_timeout_ms, 330000)
        self.assertEqual(config.agent.max_tool_output_bytes, 32768)
        self.assertEqual(config.agent.work_root, Path("/var/lib/demo/work"))
        self.assertEqual(config.rpc.max_retries, 3)
        self.assertEqual(vars(config.rpc), {"max_retries": 3, "timeout_ms": 10000})
        self.assertNotIn("private-agent-token", repr(config))

    def test_model_retry_interval_is_a_positive_uint64(self):
        data = configuration()
        for interval in (1, 2500, 2**64 - 1):
            with self.subTest(interval=interval):
                data["agent"]["model_retry_interval_ms"] = interval
                self.assertEqual(parse_config(data).agent.model_retry_interval_ms, interval)
        for interval in (0, -1, True, False, 1.5, "1000", None, 2**64):
            with self.subTest(interval=interval), self.assertRaisesRegex(ConfigError, "agent.model_retry_interval_ms"):
                data["agent"]["model_retry_interval_ms"] = interval
                parse_config(data)

    def test_zero_rpc_retries(self):
        data = configuration()
        data["rpc"] = {"max_retries": 0, "timeout_ms": 50}
        self.assertEqual(parse_config(data).rpc.max_retries, 0)

    def test_openevent_payload_cap_matches_the_server_configuration_range(self):
        data = configuration()
        for cap in (1, 4096, 60 * 1024 * 1024):
            with self.subTest(cap=cap):
                data["openevent"]["max_payload_bytes"] = cap
                self.assertEqual(parse_config(data).openevent.max_payload_bytes, cap)
        for cap in (True, 0, -1, 1.5, "4096", 60 * 1024 * 1024 + 1):
            with self.subTest(cap=cap), self.assertRaisesRegex(ConfigError, "openevent.max_payload_bytes"):
                data["openevent"]["max_payload_bytes"] = cap
                parse_config(data)

    def test_model_sdk_uses_fixed_retry_interval_without_configuration(self):
        from chat_model_agent.session import Conversation
        engine = SimpleNamespace(config=parse_config(configuration()), events=Mock(), journal=Mock())
        with patch("chat_model_agent.session.create_chat"):
            conversation = Conversation(engine, 30)
        self.assertEqual(conversation.model.retry_interval_ms, 100)

    def test_rejects_old_or_unknown_configuration_fields(self):
        for location, name in ((None, "sessions"), ("agent", "im_principal"),
                               ("agent", "max_context_messages"),
                               ("agent", "discovery_interval_ms"),
                               ("agent", "shutdown_timeout_ms"),
                               ("agent", "object_socket_path"), ("agent", "ready_path"), ("agent", "dispatch_path"),
                               ("agent", "object_index_path"), ("rpc", "retries"), ("channels", "im")):
            data = configuration()
            parent = data if location is None else data.setdefault(location, {})
            parent[name] = 1
            with self.subTest(location=location), self.assertRaises(ConfigError):
                parse_config(data)
        data = configuration()
        data["version"] = "im-agent.v1"
        with self.assertRaises(ConfigError):
            parse_config(data)

    def test_requires_valid_integer_budgets_and_absolute_paths(self):
        for name, value in (("principal", True), ("model_timeout_ms", 0), ("max_model_attempts", -1),
                            ("context_max_input_bytes", True), ("context_max_input_bytes", 0),
                            ("context_compression_prompt", "  "), ("max_tool_output_bytes", 1.2), ("work_root", "relative/work"), ("work_root", None)):
            data = configuration()
            data["agent"][name] = value
            with self.subTest(name=name, value=value), self.assertRaises(ConfigError):
                parse_config(data)
        data = configuration()
        data["rpc"] = {"max_retries": True}
        with self.assertRaises(ConfigError):
            parse_config(data)

    def test_rpc_retry_interval_is_not_a_configuration_field(self):
        data = configuration()
        for interval in (0, 50, 100, 1000):
            data["rpc"] = {"retry_interval_ms": interval}
            with self.subTest(interval=interval), self.assertRaisesRegex(ConfigError, "invalid rpc fields"):
                parse_config(data)

    def test_rejects_missing_required_values_and_conflicting_bindings(self):
        for field in ("token", "work_root", "context_compression_prompt", "context_max_input_bytes"):
            data = configuration()
            del data["agent"][field]
            with self.subTest(field=field), self.assertRaises(ConfigError):
                parse_config(data)
        data = configuration()
        data["channels"]["cmd"] = data["channels"]["model"]
        with self.assertRaises(ConfigError):
            parse_config(data)

    def test_json_load_rejects_duplicate_fields(self):
        BUILD.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=BUILD) as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(configuration()))
            self.assertEqual(load_config(path), parse_config(configuration()))
            path.write_text('{"version":"chat-agent.v1","version":"duplicate"}')
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_parser_does_not_modify_input(self):
        data = configuration()
        before = deepcopy(data)
        parse_config(data)
        self.assertEqual(data, before)


class DependenciesTest(unittest.TestCase):
    def test_current_installed_packages_provide_required_capabilities(self):
        require_dependencies()

    def test_rejects_old_sdk_even_if_module_is_importable(self):
        with patch("chat_model_agent.dependencies.metadata.distribution") as distribution:
            for version in ("0.4.1", "0.12.0"):
                with self.subTest(version=version), self.assertRaises(DependencyError):
                    distribution.return_value.version = version
                    require_sdk()

    def test_rejects_legacy_chat_writer_even_when_package_version_is_unchanged(self):
        class LegacyWriter:
            def __init__(self, client, turn_id, creation_seq, last_seq):
                pass

            def reset(self):
                pass

        with patch("openevent.chat_sdk.TurnWriter", LegacyWriter):
            with self.assertRaisesRegex(DependencyError, "current writer/cancel state"):
                require_dependencies()

    def test_requires_chat_terminal_content_reader(self):
        with patch("openevent.chat_sdk.ChatProtocolClient.get_turn_content", None):
            with self.assertRaisesRegex(DependencyError, "terminal content reads"):
                require_dependencies()


if __name__ == "__main__":
    unittest.main()

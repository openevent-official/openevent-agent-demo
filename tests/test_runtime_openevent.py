import io
from pathlib import Path
import runpy
import sys
import tempfile
from types import SimpleNamespace
import unittest
from contextlib import redirect_stderr
from unittest.mock import Mock, patch

import grpc
import yaml

from runtime_reconciler.model import ApplyError
from runtime_reconciler.openevent import OpenEventRuntime


ROOT = Path(__file__).resolve().parents[1]


class RpcFailure(grpc.RpcError):
    def __init__(self, status):
        self.status = status

    def code(self):
        return self.status


class OpenEventRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.runtime = OpenEventRuntime.__new__(OpenEventRuntime)
        self.runtime.event = Mock()
        self.runtime.admin = Mock()

    def operations(self):
        return (
            (self.runtime.event.get_status, lambda: self.runtime.token_usable(1, "token")),
            (self.runtime.admin.add_token, lambda: self.runtime.add_token(1)),
            (self.runtime.event.list_channels, lambda: self.runtime.list_channels(1, "token")),
            (self.runtime.event.get_channel, lambda: self.runtime.get_channel(1, "token", 10)),
            (self.runtime.event.create_channel, lambda: self.runtime.create_channel(
                1, "token", name="channel", protocol="chat.v1", visibility=2, members=[1])),
        )

    def test_rpc_failures_are_coordination_failures_without_retry(self):
        for method, operation in self.operations():
            for status in (grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED,
                           grpc.StatusCode.PERMISSION_DENIED):
                with self.subTest(method=method._mock_name, status=status):
                    method.reset_mock()
                    failure = RpcFailure(status)
                    method.side_effect = failure
                    with self.assertRaises(ApplyError) as caught:
                        operation()
                    self.assertEqual(caught.exception.exit_code, 3)
                    self.assertIs(caught.exception.__cause__, failure)
                    method.assert_called_once()

    def test_expected_missing_credentials_and_channel_keep_their_results(self):
        self.runtime.event.get_status.side_effect = RpcFailure(grpc.StatusCode.UNAUTHENTICATED)
        self.assertFalse(self.runtime.token_usable(1, "token"))
        self.runtime.admin.add_token.assert_not_called()
        self.runtime.event.get_channel.side_effect = RpcFailure(grpc.StatusCode.NOT_FOUND)
        self.assertIsNone(self.runtime.get_channel(1, "token", 10))
        self.runtime.event.create_channel.assert_not_called()

    def test_unexpected_errors_are_not_reclassified(self):
        for method, operation in self.operations():
            with self.subTest(method=method._mock_name):
                failure = ValueError("unexpected SDK result")
                method.side_effect = failure
                with self.assertRaises(ValueError) as caught:
                    operation()
                self.assertIs(caught.exception, failure)

    def test_cli_exits_three_when_resource_creation_loses_connection(self):
        directory = ROOT / "build/runtime-rpc-tests"
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=directory) as temporary:
            root = Path(temporary)
            spec = root / "stack.yaml"
            spec.write_text(yaml.safe_dump({
                "version": "chat-runtime.v1",
                "runtime": {"name": "rpc-test", "root": str(root / "runtime")},
                "openevent": {"grpc_addr": "127.0.0.1:9527", "admin_addr": "127.0.0.1:9528"},
                "principals": {"user": 1, "agent": 2, "model_proxy": 3, "cmd_worker": 4},
                "chat": {"web_token": "test-web-token"},
                "model": {"base_url": "https://example.test", "api_key": "test-key", "model": "test"},
                "agent": {
                    "system_prompt_file": str(ROOT / "openevent-stack/prompts/system.txt"),
                    "context_compression_prompt_file": str(ROOT / "openevent-stack/prompts/context_compression.txt"),
                    "context_max_input_bytes": 1048576,
                },
            }), encoding="utf-8")
            self.runtime.admin.add_token.side_effect = RpcFailure(grpc.StatusCode.UNAVAILABLE)
            error = io.StringIO()
            # Execute the real CLI exception handler with only external actions mocked.
            with patch("runtime_reconciler.resources.OpenEventRuntime", return_value=self.runtime), \
                    patch("runtime_reconciler.process.stop_program"), \
                    patch("runtime_reconciler.process.ensure_program_running"), \
                    patch("runtime_reconciler.openevent.wait_openevent_ready"), \
                    patch.object(sys, "argv", ["runtime_reconciler.cli", "--spec", str(spec), "--apply"]), \
                    patch.dict(sys.modules), redirect_stderr(error):
                sys.modules.pop("runtime_reconciler.cli", None)
                with self.assertRaises(SystemExit) as caught:
                    runpy.run_module("runtime_reconciler.cli", run_name="__main__")
            self.assertEqual(caught.exception.code, 3)
            self.assertIn("error: AddToken RPC failed", error.getvalue())
            self.assertNotIn("Traceback", error.getvalue())
            self.runtime.admin.add_token.assert_called_once()
            self.runtime.event.get_status.assert_not_called()

    def test_successful_rpc_results_are_preserved(self):
        channel = SimpleNamespace(channel_id=10)
        self.runtime.admin.add_token.return_value = SimpleNamespace(token="issued-token")
        self.runtime.event.list_channels.return_value = SimpleNamespace(channels=[channel])
        self.runtime.event.get_channel.return_value = SimpleNamespace(channel=channel)
        self.runtime.event.create_channel.return_value = SimpleNamespace(channel=channel)
        self.assertTrue(self.runtime.token_usable(1, "token"))
        self.assertEqual(self.runtime.add_token(1), "issued-token")
        self.assertEqual(self.runtime.list_channels(1, "token"), [channel])
        self.assertIs(self.runtime.get_channel(1, "token", 10), channel)
        self.assertIs(self.runtime.create_channel(1, "token", name="channel"), channel)

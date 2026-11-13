from __future__ import annotations
import json
import io
import shutil
from contextlib import redirect_stderr, redirect_stdout
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import yaml

from runtime_reconciler.model import ApplyError, SpecError, PROCESS_START_ORDER
from runtime_reconciler.common import _atomic_write
from runtime_reconciler.spec import parse_spec
from runtime_reconciler.resources import apply_resolve, dry_resolve
from runtime_reconciler.render import render_configs, validate_configs
from runtime_reconciler.state import (begin_apply, complete_start, load_state, save_state,
                                      write_diagnostics, write_runtime_files)
from runtime_reconciler.process import start_configured, stop_program
from runtime_reconciler.cli import _apply, main

ROOT = Path(__file__).resolve().parents[1]
TEMP_ROOT = ROOT / "build" / "runtime-tests"

def spec_data(root):
    return {
        "version": "chat-runtime.v1",
        "runtime": {"name": "test-chat", "root": str(root)},
        "openevent": {"grpc_addr": "127.0.0.1:9527", "admin_addr": "127.0.0.1:9528"},
        "principals": {"user": 1, "agent": 2, "model_proxy": 3, "cmd_worker": 4},
        "channels": {"cmd": 90},
        "chat": {"web_token": "test-web-token"},
        "model": {"base_url": "https://example.test", "api_key": "test-api-key", "model": "test-model"},
        "agent": {
            "system_prompt_file": str(ROOT / "openevent-stack/prompts/system.txt"),
            "context_compression_prompt_file": str(ROOT / "openevent-stack/prompts/context_compression.txt"),
            "context_max_input_bytes": 1048576,
        },
    }

class FakeRuntime:
    def __init__(self):
        self.tokens = {}
        self.channels = [SimpleNamespace(channel_id=90, creator=4, name="commands", protocol="cmd.v3",
                                         visibility=2, members=[2, 4], description="command channel")]
        self.created_tokens = 0
        self.created_channels = 0
        self.fail_validation = False
        self.fail_create = False
        self.on_validate = None

    def token_usable(self, principal, token):
        if self.fail_validation:
            raise ApplyError("network failure")
        if self.on_validate:
            self.on_validate(principal, token)
        return self.tokens.get(principal) == token

    def add_token(self, principal):
        self.created_tokens += 1
        token = f"token-{principal}"
        self.tokens[principal] = token
        return token

    def list_channels(self, principal, token):
        return self.channels[:]

    def get_channel(self, principal, token, channel_id):
        return next((c for c in self.channels if c.channel_id == channel_id), None)

    def create_channel(self, principal, token, **kwargs):
        self.created_channels += 1
        if self.fail_create:
            raise ApplyError("creation outcome unknown")
        channel = SimpleNamespace(channel_id=100 + self.created_channels, creator=principal, **kwargs)
        self.channels.append(channel)
        return channel

class RuntimeTests(unittest.TestCase):
    def setUp(self):
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.root = Path(self.temp.name)
        self.path = self.root / "stack.yaml"
        self.data = spec_data(self.root / "runtime")
        self.write_spec()
        self.fake = FakeRuntime()

    def tearDown(self):
        self.temp.cleanup()

    def write_spec(self):
        self.path.write_text(yaml.safe_dump(self.data), encoding="utf-8")
        self.spec = parse_spec(self.path)
        return self.spec

    def apply(self, previous=None):
        with patch("runtime_reconciler.resources.OpenEventRuntime", return_value=self.fake):
            return apply_resolve(self.spec, previous)

    def assert_model_worker_accepts_channel(self):
        from openevent.sdk import OpenEventClient
        from openevent.model_proxy.config import parse_config
        from openevent.model_proxy.worker import Worker

        config = parse_config(yaml.safe_load((self.spec.paths.config_dir / "model-proxy.yaml").read_text()))
        transport = Mock(spec=OpenEventClient)
        transport.get_channel.side_effect = lambda **kwargs: SimpleNamespace(
            channel=self.fake.get_channel(**kwargs))
        # Exercise the installed Worker's real validation of the rendered config
        # and resolved Channel; only the external GetChannel transport is local.
        Worker(config, client=transport)._validate_channels()

    def assert_initialization_requires_operator_review(self):
        saved = self.spec.paths.state_path.read_bytes()
        with patch("runtime_reconciler.process._control") as control, \
                patch("runtime_reconciler.resources.OpenEventRuntime") as runtime, \
                patch("runtime_reconciler.cli.wait_openevent_ready") as ready:
            for mode in ("--apply", "--start"):
                with self.subTest(mode=mode), self.assertRaisesRegex(ApplyError, "require operator review"):
                    main(["--spec", str(self.path), mode])
            with self.assertRaisesRegex(ApplyError, "require operator review"):
                _apply(self.spec, None)
            control.assert_not_called()
            runtime.assert_not_called()
            ready.assert_not_called()
        self.assertEqual(self.spec.paths.state_path.read_bytes(), saved)

    def test_old_spec_and_old_state_rejected(self):
        self.data["version"] = "v1"
        self.path.write_text(yaml.safe_dump(self.data))
        with self.assertRaises(SpecError):
            parse_spec(self.path)
        self.spec.paths.state_path.parent.mkdir(parents=True)
        self.spec.paths.state_path.write_text("version: v1\n")
        with self.assertRaises(ApplyError):
            load_state(self.spec.paths.state_path)

    def test_resolved_chat_configs_pass_installed_parsers(self):
        resolved = self.apply()
        configs = render_configs(self.spec, resolved)
        validate_configs(configs)
        self.assertEqual(set(configs), {"openevent", "model_proxy", "cmd_worker", "chat_server", "agent", "view"})
        self.assertEqual(configs["agent"]["data"]["channels"], resolved.channels.as_dict())
        self.assertNotIn("object_target", configs["agent"]["data"]["agent"])
        self.assertNotIn("max_context_messages", configs["agent"]["data"]["agent"])
        self.assertEqual(configs["agent"]["data"]["agent"]["work_root"],
                         str(self.spec.paths.runtime_root / "workdir"))
        self.assertEqual(configs["model_proxy"]["data"]["channels"], [resolved.channels.model])
        self.assertEqual(configs["cmd_worker"]["data"]["channel_ids"], [resolved.channels.cmd])
        self.assertEqual(configs["chat_server"]["data"]["max_user_message_bytes"], 65536)
        self.assertEqual(configs["agent"]["data"]["agent"]["max_tool_output_bytes"], 32768)
        self.assertEqual(configs["agent"]["data"]["agent"]["output_read_chunk_bytes"], 32768)
        self.assertEqual(configs["agent"]["data"]["agent"]["model_retry_interval_ms"], 1000)

    def test_model_retry_interval_reaches_agent_configuration_validation(self):
        from chat_model_agent.config import parse_config as parse_agent

        self.assertEqual(self.spec.agent["model_retry_interval_ms"], 1000)
        resolved = self.apply()
        for interval in (1, 2500, 2**64 - 1):
            with self.subTest(interval=interval):
                self.data["agent"]["model_retry_interval_ms"] = interval
                self.write_spec()
                config = render_configs(self.spec, resolved)["agent"]["data"]
                self.assertEqual(config["agent"]["model_retry_interval_ms"], interval)
                self.assertEqual(parse_agent(config).agent.model_retry_interval_ms, interval)
        self.data["agent"]["model_retry_interval_ms"] = 2**64
        self.write_spec()
        with self.assertRaisesRegex(ValueError, "agent.model_retry_interval_ms"):
            validate_configs(render_configs(self.spec, resolved))

    def test_model_retry_interval_spec_requires_a_positive_integer(self):
        for interval in (0, -1, True, False, 1.5, "1000", None):
            with self.subTest(interval=interval), self.assertRaisesRegex(SpecError, "agent.model_retry_interval_ms"):
                self.data["agent"]["model_retry_interval_ms"] = interval
                self.write_spec()

    def test_rpc_interval_is_internal_and_not_an_agent_configuration_field(self):
        self.assertEqual(self.spec.rpc, {"max_retries": 3, "timeout_ms": 10000})
        configs = render_configs(self.spec, self.apply())
        self.assertEqual(configs["agent"]["data"]["rpc"], self.spec.rpc)
        self.assertEqual(configs["model_proxy"]["data"]["worker"]["retry_interval_ms"], 100)
        for interval in (0, 50, 100, 1000):
            with self.subTest(interval=interval):
                self.data["rpc"] = {"retry_interval_ms": interval}
                with self.assertRaisesRegex(SpecError, "unknown rpc field"):
                    self.write_spec()

    def test_server_payload_cap_reaches_the_agent_publication_check(self):
        from chat_model_agent.config import parse_config as parse_agent
        from chat_model_agent.display_limits import publication_fits

        cap = 4 * 1024 * 1024
        self.data["openevent"]["max_payload_bytes"] = cap
        self.write_spec()
        configs = render_configs(self.spec, self.apply())
        validate_configs(configs)
        self.assertEqual(configs["openevent"]["data"]["limits"]["max_payload_bytes"], cap)
        self.assertEqual(configs["model_proxy"]["data"]["max_payload_bytes"], cap)
        self.assertEqual(configs["cmd_worker"]["data"]["open_event"]["max_payload_bytes"], cap)
        agent = parse_agent(configs["agent"]["data"])
        self.assertTrue(publication_fits(agent, agent.channels.wal, b"x" * cap))
        self.assertFalse(publication_fits(agent, agent.channels.wal, b"x" * (cap + 1)))

    def test_preview_explains_automatic_tokens_without_rpc_validation_or_writes(self):
        self.data.pop("channels")
        self.data["tokens"] = {"agent": "private-agent-token"}
        self.write_spec()
        with patch("runtime_reconciler.resources.OpenEventRuntime") as runtime, \
                patch("runtime_reconciler.render.validate_configs") as validate:
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["--spec", str(self.path), "--dry-run"]), 0)
            plan = yaml.safe_load(output.getvalue())
            self.assertNotIn("channels", plan)
            self.assertNotIn("unresolved", plan)
            self.assertEqual(plan["notes"], [
                "OpenEvent access tokens will be reused or created automatically during --apply.",
            ])
            self.assertNotIn("private-agent-token", output.getvalue())
            for component in ("agent", "model_proxy", "cmd_worker", "chat_server"):
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(main(["--spec", str(self.path), "--print-config", component]), 0)
                config = yaml.safe_load(output.getvalue())
                if component == "agent":
                    self.assertEqual(config["channels"], {"model": None, "cmd": None, "wal": None})
                    self.assertEqual(config["agent"]["token"], "private-agent-token")
                elif component == "model_proxy":
                    self.assertEqual(config["channels"], [None])
                    self.assertIsNone(config["token"])
                elif component == "cmd_worker":
                    self.assertEqual(config["channel_ids"], [None])
                    self.assertIsNone(config["token"])
                else:
                    self.assertIsNone(config["user_openevent_token"])
            runtime.assert_not_called()
            validate.assert_not_called()
        self.assertFalse(self.spec.paths.runtime_root.exists())

    def test_preview_shows_only_known_channel_ids(self):
        bound = {"model": 71, "cmd": 90, "wal": 73}
        for previous, expected in ((None, {"cmd": 90}), ({"channels": bound}, bound)):
            with self.subTest(previous=previous):
                resolved = dry_resolve(self.spec, previous)
                plan = write_runtime_files(self.spec, resolved, dry_run=True)
                self.assertEqual(plan["channels"], expected)
        self.assertFalse(self.spec.paths.runtime_root.exists())

    def test_unresolved_preview_cannot_be_written_as_runtime_config(self):
        with self.assertRaisesRegex(ValueError, "channels.model"):
            write_runtime_files(self.spec, dry_resolve(self.spec), False)
        self.assertFalse(self.spec.paths.runtime_root.exists())

    def test_former_placeholder_ids_preview_and_apply_as_real_channels(self):
        for channels in ({"cmd": 10001}, {"cmd": 10003}, {"cmd": 90, "model": 10003},
                         {"cmd": 90, "wal": 10001}):
            with self.subTest(channels=channels):
                self.data["runtime"]["root"] = str(self.root / ("runtime-" + "-".join(map(str, channels.values()))))
                self.data["channels"] = channels
                self.write_spec()
                self.fake = FakeRuntime()
                self.fake.channels[0].channel_id = channels["cmd"]
                for kind in ("model", "wal"):
                    if kind in channels:
                        self.fake.channels.append(SimpleNamespace(
                            channel_id=channels[kind], protocol="llm.v1" if kind == "model" else "agent.wal.v5",
                            visibility=2, members=[2, 3] if kind == "model" else [2],
                            description=json.dumps({"metadata": {}} if kind == "model" else
                                                   {"version": "v5", "updated_at_ms": 0, "metadata": {}}),
                        ))
                preview = dry_resolve(self.spec).channels.as_dict()
                self.assertEqual(preview, {"model": channels.get("model"), "cmd": channels["cmd"],
                                           "wal": channels.get("wal")})
                with patch("runtime_reconciler.resources.OpenEventRuntime", return_value=self.fake), \
                        patch("runtime_reconciler.cli.stop_program"), \
                        patch("runtime_reconciler.cli.ensure_program_running"), \
                        patch("runtime_reconciler.cli.wait_openevent_ready"), \
                        patch("runtime_reconciler.cli.start_configured") as start, \
                        patch("runtime_reconciler.render.validate_configs", wraps=validate_configs) as validate, \
                        redirect_stdout(io.StringIO()):
                    self.assertEqual(main(["--spec", str(self.path), "--apply"]), 0)
                validate.assert_called_once()
                start.assert_called_once()
                final = load_state(self.spec.paths.state_path)
                actual = json.loads((self.spec.paths.config_dir / "chat-model-agent.json").read_text())
                self.assertEqual(actual["channels"], final["channels"])
                for kind, channel_id in channels.items():
                    self.assertEqual(actual["channels"][kind], channel_id)
                if "model" in channels:
                    self.assert_model_worker_accepts_channel()
                self.assertEqual(actual["agent"]["token"], self.fake.tokens[2])
                self.assertTrue(final["initialized"])
                self.assertEqual(final["pending_restarts"], [])

    def test_explicit_channels_reject_duplicate_and_out_of_range_ids_locally(self):
        for channels in ({"cmd": 90, "model": 90}, {"cmd": 2**64}, {"cmd": 90, "wal": 2**64}):
            with self.subTest(channels=channels):
                self.data["channels"] = channels
                with self.assertRaisesRegex(SpecError, "distinct positive uint64"):
                    self.write_spec()
        self.assertFalse(self.spec.paths.runtime_root.exists())

    def test_auto_channels_match_omitted_configuration_and_keep_known_bindings(self):
        for channels, expected in (({"cmd": "auto"}, {}),
                                   ({"model": "auto", "cmd": "auto", "wal": "auto"}, {}),
                                   ({"model": "auto", "cmd": 90, "wal": "auto"}, {"cmd": 90})):
            with self.subTest(channels=channels):
                self.data["channels"] = channels
                automatic = self.write_spec()
                self.assertEqual(automatic.channels, expected)
                self.data["channels"] = expected
                omitted = self.write_spec()
                self.assertEqual(automatic.fingerprint(), omitted.fingerprint())
                self.assertEqual(dry_resolve(automatic).channels.as_dict(),
                                 {"model": None, "cmd": expected.get("cmd"), "wal": None})
                bound = {"model": 71, "cmd": 90, "wal": 73}
                self.assertEqual(dry_resolve(automatic, {"channels": bound}).channels.as_dict(), bound)
        self.assertFalse(self.spec.paths.runtime_root.exists())

    def test_apply_checks_complete_configs_after_resolving_resources_before_writing_them(self):
        self.data["agent"]["model_timeout_ms"] = 2**64
        self.write_spec()
        with patch("runtime_reconciler.resources.OpenEventRuntime", return_value=self.fake), \
                patch("runtime_reconciler.cli.stop_program"), \
                patch("runtime_reconciler.cli.ensure_program_running") as ensure, \
                patch("runtime_reconciler.cli.wait_openevent_ready"), \
                patch("runtime_reconciler.cli.start_configured") as start:
            with self.assertRaisesRegex(ValueError, "agent.model_timeout_ms"):
                main(["--spec", str(self.path), "--apply"])
            self.assertEqual(self.fake.created_tokens, 4)
            self.assertEqual(self.fake.created_channels, 2)
            ensure.assert_called_once_with(self.spec, "openevent")
            start.assert_not_called()
            state = load_state(self.spec.paths.state_path)
            self.assertFalse(state.get("initialized"))
            self.assertNotIn("applied_spec_sha256", state)
            self.assertFalse(self.spec.paths.desired_path.exists())
            for name, item in render_configs(self.spec, dry_resolve(self.spec, state)).items():
                if name != "openevent":
                    self.assertFalse((self.spec.paths.config_dir / item["path"]).exists())
            ensure.reset_mock()
            with self.assertRaisesRegex(ApplyError, "require operator review"):
                main(["--spec", str(self.path), "--start"])
            ensure.assert_not_called()
            start.assert_not_called()
        self.assert_initialization_requires_operator_review()

    def test_prompt_files_preserve_full_text_and_allow_empty_system_prompt(self):
        prompt = self.root / "system.txt"
        self.data["agent"]["system_prompt_file"] = "./system.txt"
        for value in ("", "   ", "custom instruction\r\n末尾\n"):
            with self.subTest(value=value):
                prompt.write_bytes(value.encode("utf-8"))
                self.write_spec()
                configs = render_configs(self.spec, self.apply())
                validate_configs(configs)
                self.assertEqual(configs["agent"]["data"]["agent"]["system_prompt"], value)

    def test_prompt_and_budget_fields_are_required_without_inline_alternatives(self):
        original = dict(self.data["agent"])
        for field in ("system_prompt_file", "context_compression_prompt_file", "context_max_input_bytes"):
            with self.subTest(missing=field):
                self.data["agent"] = {key: value for key, value in original.items() if key != field}
                with self.assertRaisesRegex(SpecError, field):
                    self.write_spec()
        for field in ("system_prompt", "context_compression_prompt"):
            with self.subTest(inline=field):
                self.data["agent"] = {**original, field: "inline prompt"}
                with self.assertRaisesRegex(SpecError, "unknown agent fields"):
                    self.write_spec()
        for value in (None, "", " ", 42, False, []):
            with self.subTest(path=value):
                self.data["agent"] = {**original, "system_prompt_file": value}
                with self.assertRaisesRegex(SpecError, "system_prompt_file"):
                    self.write_spec()
        for value in (None, 0, -1, True, "1048576"):
            with self.subTest(budget=value):
                self.data["agent"] = {**original, "context_max_input_bytes": value}
                with self.assertRaisesRegex(SpecError, "context_max_input_bytes"):
                    self.write_spec()

    def test_prompt_read_failures_precede_process_control_and_state_writes(self):
        source = self.root / "compression.txt"
        self.data["agent"]["context_compression_prompt_file"] = str(source)
        for contents in (None, b"\xff", b" \r\n\t"):
            if contents is None:
                source.unlink(missing_ok=True)
            else:
                source.write_bytes(contents)
            self.path.write_text(yaml.safe_dump(self.data), encoding="utf-8")
            for mode in ("--apply", "--start", "--dry-run", "--print-config"):
                with self.subTest(contents=contents, mode=mode), \
                        patch("runtime_reconciler.process._control") as control, \
                        patch("runtime_reconciler.resources.OpenEventRuntime") as runtime:
                    args = ["--spec", str(self.path), mode]
                    if mode == "--print-config":
                        args.append("agent")
                    with self.assertRaisesRegex(SpecError, "context_compression_prompt_file"):
                        main(args)
                    control.assert_not_called()
                    runtime.assert_not_called()
                    self.assertFalse(self.spec.paths.runtime_root.exists())

    def test_relative_paths_use_spec_directory_and_prompt_paths_do_not_affect_fingerprint(self):
        prompt = self.root / "system.txt"
        prompt.write_bytes(b"complete prompt\r\n")
        self.data["runtime"].update(root="relative-runtime", supervisor={"ctl": "./process.sh"})
        self.data["agent"].update(system_prompt_file="system.txt", work_root="work")
        self.data["chat"]["channels_dir"] = "sessions"
        self.data["cmd"] = {"output_dir": "output"}
        self.data["openevent"]["storage"] = {"path": "storage"}
        first = self.write_spec()
        self.assertEqual(first.paths.runtime_root, self.root / "relative-runtime")
        self.assertEqual(first.supervisor_ctl, str(self.root / "process.sh"))
        self.assertEqual(first.agent["work_root"], str(self.root / "work"))
        self.assertEqual(first.chat["channels_dir"], str(self.root / "sessions"))
        self.assertEqual(first.cmd["output_dir"], str(self.root / "output"))
        self.assertEqual(first.openevent["storage_path"], str(self.root / "storage"))
        alternate = self.root / "other-system.txt"
        alternate.write_bytes(prompt.read_bytes())
        self.data["agent"]["system_prompt_file"] = str(alternate)
        second = self.write_spec()
        self.assertEqual(first.fingerprint(), second.fingerprint())
        alternate.write_bytes(b"different body")
        self.assertNotEqual(second.fingerprint(), self.write_spec().fingerprint())

    def test_each_operation_reads_each_prompt_once_and_uses_the_snapshot(self):
        system = self.root / "system.txt"
        compression = self.root / "compression.txt"
        system.write_bytes(b"before\r\n")
        compression.write_bytes(b"compress before\n")
        self.data["agent"].update(system_prompt_file=str(system), context_compression_prompt_file=str(compression))
        self.write_spec()
        read = Path.read_bytes
        observed = []

        def read_once(path):
            result = read(path)
            if path in (system, compression):
                observed.append(path)
                path.write_bytes(b"changed after read")
            return result

        output, errors = io.StringIO(), io.StringIO()
        with patch.object(Path, "read_bytes", read_once), redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(main(["--spec", str(self.path), "--print-config", "agent"]), 0)
        config = yaml.safe_load(output.getvalue())["agent"]
        self.assertEqual(config["system_prompt"], "before\r\n")
        self.assertEqual(config["context_compression_prompt"], "compress before\n")
        self.assertEqual(observed, [system, compression])
        self.assertIn("B=1048576", errors.getvalue())
        self.assertIn("F=", errors.getvalue())
        self.assertIn("T=", errors.getvalue())
        self.assertIn("Chat body limit=65536", errors.getvalue())
        self.assertIn("tool output limit per stream=32768", errors.getvalue())
        self.assertNotIn("system_prompt_file", config)
        self.assertNotIn("context_compression_prompt_file", config)
        self.assertFalse(self.spec.paths.runtime_root.exists())

    def test_context_baseline_failure_is_a_spec_error_before_apply_side_effects(self):
        self.data["agent"]["context_max_input_bytes"] = 1
        self.path.write_text(yaml.safe_dump(self.data), encoding="utf-8")
        with patch("runtime_reconciler.process._control") as control, \
                patch("runtime_reconciler.resources.OpenEventRuntime") as runtime:
            with self.assertRaisesRegex(SpecError, "context budget"):
                main(["--spec", str(self.path), "--apply"])
            control.assert_not_called()
            runtime.assert_not_called()
        self.assertFalse(self.spec.paths.runtime_root.exists())

    def test_every_cli_mode_rejects_invalid_explicit_cmd_even_with_saved_binding(self):
        resolved = self.apply()
        write_runtime_files(self.spec, resolved, False)
        before = {p: p.read_bytes() for p in self.spec.paths.config_dir.iterdir()}
        for channels in ({"cmd": None}, {"cmd": 0}, {"cmd": "90"}, {"cmd": "automatic"},
                         {"cmd": False}, {"cmd": []}, {"cmd": {}}):
            data = {**self.data, "channels": channels}
            self.path.write_text(yaml.safe_dump(data), encoding="utf-8")
            for mode in ("--apply", "--start", "--dry-run", "--print-config"):
                with self.subTest(channels=channels, mode=mode):
                    with patch("runtime_reconciler.process._control") as control, \
                            patch("runtime_reconciler.resources.OpenEventRuntime") as runtime:
                        args = ["--spec", str(self.path), mode]
                        if mode == "--print-config":
                            args.append("cmd_worker")
                        with self.assertRaisesRegex(SpecError, "channels.cmd"):
                            main(args)
                        control.assert_not_called()
                        runtime.assert_not_called()
                    self.assertEqual({p: p.read_bytes() for p in self.spec.paths.config_dir.iterdir()}, before)

    def test_first_apply_prepares_all_channels_from_one_spec_and_reuses_them(self):
        self.data["channels"] = {"cmd": "auto"}
        self.write_spec()
        self.fake.channels.clear()
        with patch("runtime_reconciler.resources.OpenEventRuntime", return_value=self.fake) as runtime, \
                patch("runtime_reconciler.cli.stop_program"), \
                patch("runtime_reconciler.cli.ensure_program_running"), \
                patch("runtime_reconciler.cli.wait_openevent_ready"), \
                patch("runtime_reconciler.cli.start_configured") as start, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--spec", str(self.path), "--apply"]), 0)
            first = load_state(self.spec.paths.state_path)
            self.assertTrue(first["initialized"])
            self.assertEqual(self.fake.created_tokens, 4)
            self.assertEqual(self.fake.created_channels, 3)
            self.assertEqual(first["pending_restarts"], [])
            self.assertNotIn("initializing_channels", first)
            cmd = next(c for c in self.fake.channels if c.channel_id == first["channels"]["cmd"])
            self.assertEqual(cmd.protocol, "cmd.v3")
            self.assertEqual(cmd.visibility, 2)
            self.assertEqual(set(cmd.members), {2, 4})
            self.assertEqual(json.loads(cmd.description), {"metadata": {"deployment_id": self.spec.deployment_id}})
            model = next(c for c in self.fake.channels if c.channel_id == first["channels"]["model"])
            self.assertEqual(json.loads(model.description), {"metadata": {"deployment_id": self.spec.deployment_id}})
            self.assert_model_worker_accepts_channel()
            agent = json.loads((self.spec.paths.config_dir / "chat-model-agent.json").read_text())
            self.assertEqual(agent["channels"], first["channels"])
            worker = yaml.safe_load((self.spec.paths.config_dir / "cmd-worker.yaml").read_text())
            self.assertEqual(worker["channel_ids"], [cmd.channel_id])
            self.assertEqual(main(["--spec", str(self.path), "--apply"]), 0)
            self.assertEqual(load_state(self.spec.paths.state_path)["channels"], first["channels"])
            self.assertEqual(self.fake.created_channels, 3)
            runtime.reset_mock()
            self.assertEqual(main(["--spec", str(self.path), "--start"]), 0)
            runtime.assert_not_called()
            self.assertEqual(start.call_count, 3)

    def test_cmd_discovery_ignores_unmarked_and_non_mapping_descriptions(self):
        self.data["channels"] = {}
        self.write_spec()
        self.fake.channels.clear()
        for channel_id, description in enumerate(("ordinary channel", '{"metadata":"text"}',
                                                   '{"metadata":["other"]}', "[]"), 90):
            self.fake.channels.append(SimpleNamespace(
                channel_id=channel_id, protocol="cmd.v3", visibility=2, members=[2, 4],
                description=description))
        resolved = self.apply()
        self.assertGreater(resolved.channels.cmd, 100)
        self.assertEqual(self.fake.created_channels, 3)

    def test_cmd_creation_interruption_requires_cleanup_before_initializing_again(self):
        self.data.pop("channels")
        self.write_spec()
        create_channel = self.fake.create_channel

        def create_then_interrupt(principal, token, **kwargs):
            channel = create_channel(principal, token, **kwargs)
            if kwargs["protocol"] == "cmd.v3":
                raise ApplyError("creation outcome unknown")
            return channel

        with patch.object(self.fake, "create_channel", side_effect=create_then_interrupt), \
                patch("runtime_reconciler.resources.OpenEventRuntime", return_value=self.fake), \
                patch("runtime_reconciler.cli.stop_program"), \
                patch("runtime_reconciler.cli.ensure_program_running"), \
                patch("runtime_reconciler.cli.wait_openevent_ready"):
            with self.assertRaisesRegex(ApplyError, "outcome unknown"):
                _apply(self.spec, None)
        state = load_state(self.spec.paths.state_path)
        self.assertFalse(state.get("initialized"))
        self.assertNotIn("initializing_channels", state)
        self.assertNotIn("channels", state)
        self.assert_initialization_requires_operator_review()
        self.assertEqual(self.fake.created_channels, 2)
        # Operations clears generated files and the deployment's OpenEvent data.
        shutil.rmtree(self.spec.paths.runtime_root)
        self.fake = FakeRuntime()
        with patch("runtime_reconciler.resources.OpenEventRuntime", return_value=self.fake), \
                patch("runtime_reconciler.cli.stop_program"), \
                patch("runtime_reconciler.cli.ensure_program_running"), \
                patch("runtime_reconciler.cli.wait_openevent_ready"), \
                patch("runtime_reconciler.cli.start_configured"):
            _apply(self.spec, None)
        self.assertEqual(self.fake.created_channels, 3)
        self.assertTrue(load_state(self.spec.paths.state_path)["initialized"])

    def test_first_apply_process_start_failure_requires_cleanup_despite_committed_configs(self):
        def fail_start(spec, plan, *, stopped=()):
            state = load_state(spec.paths.state_path)
            self.assertFalse(state.get("initialized"))
            self.assertEqual(set(state["channels"]), {"model", "cmd", "wal"})
            self.assertEqual(state["applied_spec_sha256"], spec.fingerprint())
            raise ApplyError("initial process start failure")

        errors = io.StringIO()
        with patch("runtime_reconciler.resources.OpenEventRuntime", return_value=self.fake), \
                patch("runtime_reconciler.cli.stop_program"), \
                patch("runtime_reconciler.cli.ensure_program_running"), \
                patch("runtime_reconciler.cli.wait_openevent_ready"), \
                patch("runtime_reconciler.cli.start_configured", side_effect=fail_start), redirect_stderr(errors):
            with self.assertRaisesRegex(ApplyError, "initial process start failure"):
                _apply(self.spec, None)
        self.assertIn("require operator review", errors.getvalue())
        self.assert_initialization_requires_operator_review()

    def test_explicit_cmd_cannot_replace_saved_binding(self):
        resolved = self.apply()
        write_runtime_files(self.spec, resolved, False)
        complete_start(self.spec)
        self.data["channels"]["cmd"] = 91
        self.write_spec()
        for mode in ("--apply", "--start", "--dry-run", "--print-config"):
            with self.subTest(mode=mode):
                with patch("runtime_reconciler.process._control") as control, \
                        patch("runtime_reconciler.resources.OpenEventRuntime") as runtime:
                    args = ["--spec", str(self.path), mode]
                    if mode == "--print-config":
                        args.append("cmd_worker")
                    with self.assertRaisesRegex((SpecError, ApplyError), "permanent binding|run --apply first"):
                        main(args)
                    control.assert_not_called()
                    runtime.assert_not_called()

    def test_missing_explicit_cmd_is_not_discovered_or_created(self):
        self.fake.channels[0].channel_id = 91
        self.fake.channels[0].description = json.dumps({"metadata": {"deployment_id": self.spec.deployment_id}})
        with self.assertRaisesRegex(ApplyError, "explicit cmd Channel is missing"):
            self.apply()
        self.assertEqual([c.channel_id for c in self.fake.channels if c.protocol == "cmd.v3"], [91])
        self.assertIsNone(load_state(self.spec.paths.state_path))

    def test_cli_paths_are_relative_to_spec_not_installed_package(self):
        self.data["runtime"]["root"] = "relative-runtime"
        self.path.write_text(yaml.safe_dump(self.data), encoding="utf-8")
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["--spec", str(self.path), "--dry-run"]), 0)
        plan = yaml.safe_load(output.getvalue())
        self.assertEqual(Path(plan["configs"]["agent"]["path"]), self.root / "relative-runtime/config/chat-model-agent.json")
        self.assertFalse((self.root / "relative-runtime").exists())

    def test_new_token_saved_before_validation_and_reused(self):
        def saved_before_validation(principal, token):
            saved = yaml.safe_load(self.spec.paths.secrets_path.read_text())
            self.assertTrue(any(v["principal"] == principal and v["token"] == token for v in saved["tokens"].values()))
        self.fake.on_validate = saved_before_validation
        with patch("runtime_reconciler.resources._atomic_write", wraps=_atomic_write) as written:
            first = self.apply()
            self.assertEqual(written.call_count, 4)
        self.assertEqual(self.fake.created_tokens, 4)
        with patch("runtime_reconciler.resources._atomic_write", wraps=_atomic_write) as written:
            second = self.apply(load_state(self.spec.paths.state_path))
            written.assert_not_called()
        self.assertEqual(self.fake.created_tokens, 4)
        self.assertEqual(first.channels, second.channels)

    def test_adopting_changed_explicit_token_saves_only_its_mapping(self):
        first = self.apply()
        before = yaml.safe_load(self.spec.paths.secrets_path.read_text())
        self.data["tokens"] = {"agent": "replacement-agent-token"}
        self.write_spec()
        self.fake.tokens[self.spec.principals["agent"]] = "replacement-agent-token"
        with patch("runtime_reconciler.resources._atomic_write", wraps=_atomic_write) as written:
            second = self.apply(load_state(self.spec.paths.state_path))
            self.assertEqual(written.call_count, 1)
        after = yaml.safe_load(self.spec.paths.secrets_path.read_text())
        expected = before["tokens"]
        expected["agent"] = {"principal": self.spec.principals["agent"], "token": "replacement-agent-token"}
        self.assertEqual(after["tokens"], expected)
        self.assertEqual(second.tokens["agent"], "replacement-agent-token")
        self.assertEqual(self.fake.created_tokens, 4)
        self.assertEqual(first.channels, second.channels)

    def test_new_token_remains_saved_after_validation_failure_and_is_reused(self):
        self.fake.fail_validation = True
        with patch("runtime_reconciler.resources._atomic_write", wraps=_atomic_write) as written:
            with self.assertRaisesRegex(ApplyError, "network"):
                self.apply()
            self.assertEqual(written.call_count, 1)
        saved = yaml.safe_load(self.spec.paths.secrets_path.read_text())["tokens"]
        self.assertEqual(len(saved), 1)
        name, entry = next(iter(saved.items()))
        self.assertEqual(entry, {"principal": self.spec.principals[name],
                                 "token": self.fake.tokens[self.spec.principals[name]]})
        self.assertEqual(self.fake.created_tokens, 1)
        self.assertEqual(self.fake.created_channels, 0)
        self.fake.fail_validation = False
        with patch("runtime_reconciler.resources._atomic_write", wraps=_atomic_write) as written:
            resolved = self.apply()
            self.assertEqual(written.call_count, 3)
        self.assertEqual(resolved.tokens[name], entry["token"])
        self.assertEqual(self.fake.created_tokens, 4)

    def test_network_error_does_not_create_replacement_token(self):
        self.data["tokens"] = {name: "explicit" for name in self.data["principals"]}
        self.write_spec()
        self.fake.fail_validation = True
        with self.assertRaisesRegex(ApplyError, "network"):
            self.apply()
        self.assertEqual(self.fake.created_tokens, 0)
        self.assertEqual(self.fake.created_channels, 0)

    def test_interrupted_initialization_requires_operator_review_without_a_failure_record(self):
        begin_apply(self.spec)
        self.assert_initialization_requires_operator_review()
        self.assertFalse(load_state(self.spec.paths.state_path).get("initialized"))

    def test_ambiguous_discovery_fails(self):
        first = self.apply()
        original = next(c for c in self.fake.channels if c.protocol == "llm.v1")
        duplicate = SimpleNamespace(**{**vars(original), "channel_id": 999})
        self.fake.channels.append(duplicate)
        self.spec.paths.state_path.unlink()
        with self.assertRaisesRegex(ApplyError, "multiple model"):
            self.apply()

    def test_permanent_binding_cannot_be_removed_or_changed(self):
        self.apply()
        complete_start(self.spec)
        state = load_state(self.spec.paths.state_path)
        state.pop("channels")
        save_state(self.spec, state)
        with self.assertRaisesRegex(ApplyError, "missing"):
            load_state(self.spec.paths.state_path, spec=self.spec)

    def test_missing_state_does_not_reinitialize_existing_deployment(self):
        self.apply()
        self.spec.paths.state_path.unlink()
        with self.assertRaisesRegex(ApplyError, "missing its runtime state"):
            begin_apply(self.spec)

    def test_explicit_cmd_binding_accepts_plain_description(self):
        # A Cmd description has no deployment metadata contract, including at initialization.
        for number, description in enumerate(('{"metadata":"说明"}', "plain command channel")):
            with self.subTest(description=description):
                self.data["runtime"]["root"] = str(self.root / f"runtime-description-{number}")
                self.write_spec()
                self.fake = FakeRuntime()
                self.fake.channels[0].description = description
                first = self.apply()
                self.assertEqual(first.channels.cmd, self.spec.channels["cmd"])
                second = self.apply(load_state(self.spec.paths.state_path))
                self.assertEqual(first.channels, second.channels)
                self.assertEqual(self.fake.created_channels, 2)

    def test_config_changes_track_chat_settings_and_worker_environment(self):
        resolved = self.apply()
        first = write_runtime_files(self.spec, resolved, False)
        self.assertEqual(load_state(self.spec.paths.state_path)["cmd_environment"], {
            "object_target": self.spec.agent["object_target"],
        })
        complete_start(self.spec)
        second = write_runtime_files(self.spec, resolved, False)
        self.assertTrue(first["configs"]["chat_server"]["changed"])
        self.assertTrue(all(not c["changed"] for c in second["configs"].values()))
        changed = replace(self.spec, chat={**self.spec.chat, "port": 8090}, agent={**self.spec.agent, "object_target": "new-objects:9527"})
        third = write_runtime_files(changed, resolved, False)
        self.assertTrue(third["configs"]["chat_server"]["changed"])
        self.assertTrue(third["configs"]["cmd_worker"]["changed"])
        self.assertFalse(third["configs"]["agent"]["changed"])
        self.assertEqual(load_state(self.spec.paths.state_path)["cmd_environment"]["object_target"],
                         changed.agent["object_target"])
        self.assertEqual(json.loads((self.spec.paths.config_dir / "chat-server.json").read_text())["port"], 8090)

    def test_config_restart_survives_interrupted_apply_and_start(self):
        resolved = self.apply()
        write_runtime_files(self.spec, resolved, False)
        complete_start(self.spec)
        self.data["model"]["api_key"] = "replacement-key"
        self.write_spec()
        changed = write_runtime_files(self.spec, resolved, False)
        self.assertTrue(changed["configs"]["model_proxy"]["changed"])
        # A retry sees identical files, but the old process still needs a restart.
        retry = write_runtime_files(self.spec, resolved, False)
        self.assertTrue(retry["configs"]["model_proxy"]["changed"])
        with patch("runtime_reconciler.cli.ensure_program_running"), \
                patch("runtime_reconciler.cli.wait_openevent_ready"), \
                patch("runtime_reconciler.cli.start_configured") as start, redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--spec", str(self.path), "--start"]), 0)
        self.assertTrue(start.call_args.args[1]["configs"]["model_proxy"]["changed"])
        self.assertEqual(load_state(self.spec.paths.state_path)["pending_restarts"], [])
        done = write_runtime_files(self.spec, resolved, False)
        self.assertTrue(all(not item["changed"] for item in done["configs"].values()))

    def test_model_retry_interval_update_requires_apply_and_only_changes_agent(self):
        resolved = self.apply()
        write_runtime_files(self.spec, resolved, False)
        complete_start(self.spec)
        self.data["agent"]["model_retry_interval_ms"] = 2500
        self.write_spec()
        with patch("runtime_reconciler.cli.ensure_program_running") as start:
            with self.assertRaisesRegex(SpecError, "run --apply first"):
                main(["--spec", str(self.path), "--start"])
            start.assert_not_called()
        plan = write_runtime_files(self.spec, resolved, False)
        self.assertEqual({name for name, item in plan["configs"].items() if item["changed"]}, {"agent"})
        config = json.loads((self.spec.paths.config_dir / "chat-model-agent.json").read_text())
        self.assertEqual(config["agent"]["model_retry_interval_ms"], 2500)

    def test_start_requires_apply_for_credentials_and_runtime_options(self):
        resolved = self.apply()
        write_runtime_files(self.spec, resolved, False)
        complete_start(self.spec)
        changes = (
            ("web token", {"chat": {**self.data["chat"], "web_token": "new-web-token"}}),
            ("model key", {"model": {**self.data["model"], "api_key": "new-model-key"}}),
            ("OpenEvent token", {"tokens": {"agent": "new-agent-token"}}),
            ("idle wait", {"agent": {**self.data["agent"], "idle_sleep_ms": 100}}),
        )
        for label, change in changes:
            with self.subTest(label):
                self.path.write_text(yaml.safe_dump({**self.data, **change}), encoding="utf-8")
                with patch("runtime_reconciler.cli.ensure_program_running") as start:
                    with self.assertRaisesRegex(SpecError, "run --apply first"):
                        main(["--spec", str(self.path), "--start"])
                    start.assert_not_called()

    def test_supervisor_binding_rejects_changes_before_any_cli_side_effects(self):
        resolved = self.apply()
        write_runtime_files(self.spec, resolved, False)
        complete_start(self.spec)
        before = {p: p.read_bytes() for p in self.spec.paths.config_dir.iterdir()}
        changes = ({"ctl": "/bin/other-controller"}, {"programs": {"agent": "other-agent"}})
        for change in changes:
            self.data["runtime"]["supervisor"] = change
            self.write_spec()
            for mode in ("--apply", "--start", "--dry-run", "--print-config"):
                with self.subTest(change=change, mode=mode):
                    with patch("runtime_reconciler.process._control") as control, \
                            patch("runtime_reconciler.resources.OpenEventRuntime") as runtime:
                        args = ["--spec", str(self.path), mode]
                        if mode == "--print-config":
                            args.append("agent")
                        with self.assertRaisesRegex(ApplyError, "supervisor"):
                            main(args)
                        control.assert_not_called()
                        runtime.assert_not_called()
                    self.assertEqual({p: p.read_bytes() for p in self.spec.paths.config_dir.iterdir()}, before)

    def test_first_apply_binds_supervisor_before_process_control(self):
        def interrupt_first_stop(spec, logical):
            state = load_state(spec.paths.state_path, spec=spec)
            self.assertEqual(state["supervisor"], {"ctl": spec.supervisor_ctl, "programs": spec.supervisor_programs})
            self.assertFalse(state.get("initialized"))
            self.assertEqual(set(state["pending_restarts"]), set(PROCESS_START_ORDER))
            self.assertNotIn("channels", state)
            raise ApplyError("interrupted before first process operation")

        with patch("runtime_reconciler.cli.stop_program", side_effect=interrupt_first_stop):
            with self.assertRaisesRegex(ApplyError, "interrupted before"):
                main(["--spec", str(self.path), "--apply"])
        self.assert_initialization_requires_operator_review()
        # Read-only inspection remains available after initialization fails.
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--spec", str(self.path), "--dry-run"]), 0)
        self.data["runtime"]["supervisor"] = {"programs": {"agent": "other-agent"}}
        self.write_spec()
        with patch("runtime_reconciler.process._control") as control:
            with self.assertRaisesRegex(ApplyError, "supervisor"):
                main(["--spec", str(self.path), "--apply"])
            control.assert_not_called()

    def test_missing_or_incomplete_supervisor_binding_is_not_reconstructed(self):
        begin_apply(self.spec)
        state = load_state(self.spec.paths.state_path)
        for binding in (None, {"ctl": self.spec.supervisor_ctl}, {"programs": self.spec.supervisor_programs}):
            with self.subTest(binding=binding):
                changed = {**state, "supervisor": binding}
                if binding is None:
                    changed.pop("supervisor")
                self.spec.paths.state_path.write_text(yaml.safe_dump(changed), encoding="utf-8")
                with self.assertRaisesRegex(ApplyError, "supervisor"):
                    main(["--spec", str(self.path), "--dry-run"])

    def test_incomplete_apply_invalidates_start_until_configuration_commits(self):
        resolved = self.apply()
        write_runtime_files(self.spec, resolved, False)
        complete_start(self.spec)
        original = self.spec
        self.data["model"]["api_key"] = "new-model-key"
        self.write_spec()
        begin_apply(self.spec)
        def fail_before_state(path, *args):
            if path == self.spec.paths.state_path:
                raise OSError("interrupted configuration write")
            return _atomic_write(path, *args)

        with patch("runtime_reconciler.state._atomic_write", side_effect=fail_before_state):
            with self.assertRaises(OSError):
                write_runtime_files(self.spec, resolved, False)
        self.assertNotIn("applied_spec_sha256", load_state(self.spec.paths.state_path))
        for key in (original.model["api_key"], self.spec.model["api_key"]):
            self.data["model"]["api_key"] = key
            self.write_spec()
            with self.subTest(key=key), patch("runtime_reconciler.cli.ensure_program_running") as start:
                with self.assertRaisesRegex(SpecError, "run --apply first"):
                    main(["--spec", str(self.path), "--start"])
                start.assert_not_called()

    def test_spec_fingerprint_keeps_normalized_configuration_redacted(self):
        self.data["tokens"] = {"agent": "private-agent-token"}
        self.write_spec()
        plan = write_runtime_files(self.spec, self.apply(), False)
        write_diagnostics(self.spec, plan)
        state = load_state(self.spec.paths.state_path)
        self.assertEqual(state["applied_spec_sha256"], self.spec.fingerprint())
        visible = self.spec.paths.desired_path.read_text()
        for secret in (self.spec.chat["web_token"], self.spec.model["api_key"], self.spec.tokens["agent"]):
            self.assertNotIn(secret, visible)
            self.assertNotIn(secret, self.spec.paths.state_path.read_text())
        self.assertEqual(yaml.safe_load(visible)["tokens"]["agent"], "<redacted>")

    def test_config_file_written_before_state_still_requires_restart(self):
        resolved = self.apply()
        write_runtime_files(self.spec, resolved, False)
        complete_start(self.spec)
        old_state = load_state(self.spec.paths.state_path)
        changed = replace(self.spec, model={**self.spec.model, "api_key": "replacement-key"})
        write_runtime_files(changed, resolved, False)
        # Model the crash window between writing files and saving new state.
        save_state(self.spec, old_state)
        retry = write_runtime_files(changed, resolved, False)
        self.assertTrue(retry["configs"]["model_proxy"]["changed"])

    def test_worker_starts_independently_before_agent_and_survives_agent_restart(self):
        calls, running = [], {"openevent"}
        def control(spec, action, logical):
            calls.append((action, logical))
            if action == "start":
                if logical == "agent":
                    self.assertIn("cmd_worker", running)
                running.add(logical)
            elif action == "stop":
                running.discard(logical)
            return SimpleNamespace(returncode=0 if logical in running or action == "stop" else 3, stdout=f"{logical} RUNNING pid 4321" if logical in running else "STOPPED")
        with patch("runtime_reconciler.process._control", side_effect=control):
            start_configured(self.spec, {"configs": {}})
            calls.clear()
            start_configured(self.spec, {"configs": {}})
            self.assertFalse(any(action in {"start", "stop"} for action, _ in calls))
            for _ in range(2):
                calls.clear()
                start_configured(self.spec, {"configs": {"agent": {"changed": True}}})
                self.assertEqual([call for call in calls if call[0] in {"start", "stop"}],
                                 [("stop", "agent"), ("start", "agent")])

    def test_removed_configuration_fields_are_rejected(self):
        for group, field in (("runtime", "startup_timeout_ms"), ("agent", "discovery_interval_ms"),
                             ("agent", "max_context_messages"), ("agent", "shutdown_timeout_ms")):
            with self.subTest(field=field):
                data = {**self.data, group: {**self.data.get(group, {}), field: 1000}}
                self.path.write_text(yaml.safe_dump(data))
                with self.assertRaisesRegex(SpecError, "unknown .* fields: " + field):
                    parse_spec(self.path)
        config = render_configs(self.spec, self.apply())["agent"]["data"]["agent"]
        self.assertNotIn("discovery_interval_ms", config)
        self.assertNotIn("shutdown_timeout_ms", config)
        wal = next(c for c in self.fake.channels if c.protocol == "agent.wal.v5")
        self.assertEqual(json.loads(wal.description)["version"], "v5")

    def test_openevent_prevalidation_has_no_process_or_state_side_effects(self):
        for change in ({"admin_addr": "127.0.0.1:9527"}, {"max_payload_bytes": 62914561}):
            with self.subTest(change=change):
                self.data["openevent"] = {**spec_data(self.root)["openevent"], **change}
                self.write_spec()
                with patch("runtime_reconciler.process._control") as control, \
                        patch("runtime_reconciler.resources.OpenEventRuntime") as runtime:
                    with self.assertRaisesRegex(SpecError, "OpenEvent"):
                        main(["--spec", str(self.path), "--apply"])
                    control.assert_not_called()
                    runtime.assert_not_called()
                self.assertFalse(self.spec.paths.runtime_root.exists())

    def initialized_processes(self):
        write_runtime_files(self.spec, self.apply(), False)
        complete_start(self.spec)
        calls, running = [], set(PROCESS_START_ORDER)

        def control(spec, action, logical):
            if action in {"start", "stop"}:
                calls.append((action, logical))
            if action == "start":
                running.add(logical)
            elif action == "stop":
                running.discard(logical)
            return SimpleNamespace(returncode=0 if logical in running or action == "stop" else 3,
                                   stdout="RUNNING" if logical in running else "STOPPED")
        return calls, running, control

    def test_port_update_stops_clients_before_server_and_resolves_only_new_address(self):
        calls, running, control = self.initialized_processes()
        self.data["openevent"].update(grpc_addr="127.0.0.1:9627", admin_addr="127.0.0.1:9628")
        self.write_spec()

        def ready(spec):
            calls.append(("ready", "openevent"))
            config = yaml.safe_load((spec.paths.config_dir / "openevent-server.yaml").read_text())
            self.assertEqual(config["grpc"]["listen_addr"], "127.0.0.1:9627")
            self.assertEqual(config["admin"]["listen_addr"], "127.0.0.1:9628")
            self.assertEqual(running, {"openevent"})

        def runtime(grpc_addr, admin_addr, timeout):
            calls.append(("resolve", "resources"))
            self.assertEqual((grpc_addr, admin_addr), ("127.0.0.1:9627", "127.0.0.1:9628"))
            self.assertLess(calls.index(("start", "openevent")), calls.index(("ready", "openevent")))
            return self.fake

        def guarded_control(spec, action, logical):
            if action == "stop" and logical == "openevent":
                state = load_state(spec.paths.state_path)
                self.assertNotIn("applied_spec_sha256", state)
                self.assertEqual(set(state["pending_restarts"]), set(PROCESS_START_ORDER))
                self.assertEqual(running, {"openevent"})
            return control(spec, action, logical)

        with patch("runtime_reconciler.process._control", side_effect=guarded_control), \
                patch("runtime_reconciler.cli.wait_openevent_ready", side_effect=ready), \
                patch("runtime_reconciler.resources.OpenEventRuntime", side_effect=runtime), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--spec", str(self.path), "--apply"]), 0)
        self.assertEqual(calls[0], ("stop", "agent"))
        self.assertEqual(calls[-1], ("start", "agent"))
        self.assertCountEqual([logical for action, logical in calls if action == "stop"], PROCESS_START_ORDER)
        self.assertEqual([logical for action, logical in calls if action == "start"], list(PROCESS_START_ORDER))
        self.assertEqual(load_state(self.spec.paths.state_path)["pending_restarts"], [])

    def test_early_server_write_requires_reapply_and_retry_restarts_from_saved_generation(self):
        calls, running, control = self.initialized_processes()
        old_hash = load_state(self.spec.paths.state_path)["configs"]["openevent"]["sha256"]
        self.data["openevent"]["grpc_addr"] = "127.0.0.1:9627"
        self.write_spec()
        with patch("runtime_reconciler.process._control", side_effect=control), \
                patch("runtime_reconciler.cli.wait_openevent_ready"), \
                patch("runtime_reconciler.cli.apply_resolve", side_effect=ApplyError("resource failure")):
            with self.assertRaisesRegex(ApplyError, "resource failure"):
                main(["--spec", str(self.path), "--apply"])
        state = load_state(self.spec.paths.state_path)
        self.assertNotIn("applied_spec_sha256", state)
        self.assertEqual(state["configs"]["openevent"]["sha256"], old_hash)
        self.assertIn("openevent", state["pending_restarts"])
        self.assertCountEqual([logical for action, logical in calls if action == "stop"], PROCESS_START_ORDER)
        with patch("runtime_reconciler.process._control") as blocked:
            with self.assertRaisesRegex(SpecError, "run --apply first"):
                main(["--spec", str(self.path), "--start"])
            blocked.assert_not_called()
        calls.clear()
        with patch("runtime_reconciler.process._control", side_effect=control), \
                patch("runtime_reconciler.cli.wait_openevent_ready"), \
                patch("runtime_reconciler.resources.OpenEventRuntime", return_value=self.fake), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--spec", str(self.path), "--apply"]), 0)
        # A new CLI invocation must confirm stops again, even when the failed
        # invocation already stopped those processes before restarting OpenEvent.
        self.assertCountEqual([logical for action, logical in calls if action == "stop"], PROCESS_START_ORDER)
        self.assertEqual([logical for action, logical in calls if action == "start"], list(PROCESS_START_ORDER))
        self.assertEqual(running, set(PROCESS_START_ORDER))

    def test_dependency_change_pauses_agent_before_config_write_and_keeps_server_running(self):
        calls, running, control = self.initialized_processes()
        self.data["model"]["api_key"] = "replacement-key"
        self.write_spec()

        def guarded_write(path, *args):
            if path.name == "model-proxy.yaml":
                self.assertNotIn("agent", running)
                state = load_state(self.spec.paths.state_path)
                self.assertNotIn("applied_spec_sha256", state)
                self.assertTrue({"agent", "model_proxy"} <= set(state["pending_restarts"]))
            return _atomic_write(path, *args)

        with patch("runtime_reconciler.process._control", side_effect=control), \
                patch("runtime_reconciler.cli.wait_openevent_ready"), \
                patch("runtime_reconciler.resources.OpenEventRuntime", return_value=self.fake), \
                patch("runtime_reconciler.state._atomic_write", side_effect=guarded_write), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--spec", str(self.path), "--apply"]), 0)
            self.assertNotIn(("stop", "openevent"), calls)
            self.assertEqual([logical for action, logical in calls if action == "stop"], ["agent", "model_proxy"])
            self.assertEqual([logical for action, logical in calls if action == "start"], ["model_proxy", "agent"])
            calls.clear()
            self.assertEqual(main(["--spec", str(self.path), "--apply"]), 0)
            self.assertEqual(calls, [])

    def test_prompt_content_and_budget_updates_require_apply_and_only_restart_agent(self):
        prompt = self.root / "system.txt"
        prompt.write_text("original prompt", encoding="utf-8")
        self.data["agent"]["system_prompt_file"] = str(prompt)
        self.write_spec()
        calls, running, control = self.initialized_processes()
        for change in ("prompt", "budget"):
            with self.subTest(change=change):
                if change == "prompt":
                    prompt.write_text("changed prompt", encoding="utf-8")
                else:
                    self.data["agent"]["context_max_input_bytes"] = 1200000
                    self.path.write_text(yaml.safe_dump(self.data), encoding="utf-8")
                with patch("runtime_reconciler.process._control") as blocked:
                    with self.assertRaisesRegex(SpecError, "run --apply first"):
                        main(["--spec", str(self.path), "--start"])
                    blocked.assert_not_called()
                calls.clear()
                with patch("runtime_reconciler.process._control", side_effect=control), \
                        patch("runtime_reconciler.cli.wait_openevent_ready"), \
                        patch("runtime_reconciler.resources.OpenEventRuntime", return_value=self.fake), \
                        redirect_stdout(io.StringIO()):
                    self.assertEqual(main(["--spec", str(self.path), "--apply"]), 0)
                self.assertEqual(calls, [("stop", "agent"), ("start", "agent")])
                self.assertEqual(running, set(PROCESS_START_ORDER))

    def test_chat_listener_updates_change_config_hash_and_restart_chat(self):
        calls, running, control = self.initialized_processes()
        for field, value in (("host", "0.0.0.0"), ("port", 8090),
                             ("origin", "https://chat.example.test")):
            with self.subTest(field=field):
                calls.clear()
                old_hash = load_state(self.spec.paths.state_path)["configs"]["chat_server"]["sha256"]
                self.data["chat"][field] = value
                self.write_spec()
                with patch("runtime_reconciler.process._control", side_effect=control), \
                        patch("runtime_reconciler.cli.wait_openevent_ready"), \
                        patch("runtime_reconciler.resources.OpenEventRuntime", return_value=self.fake), \
                        redirect_stdout(io.StringIO()):
                    self.assertEqual(main(["--spec", str(self.path), "--apply"]), 0)
                self.assertEqual(calls, [("stop", "agent"), ("stop", "chat_server"),
                                         ("start", "chat_server"), ("start", "agent")])
                state = load_state(self.spec.paths.state_path)
                self.assertNotEqual(state["configs"]["chat_server"]["sha256"], old_hash)
                self.assertNotIn("chat_launch", state)
                config = json.loads((self.spec.paths.config_dir / "chat-server.json").read_text())
                self.assertEqual(config[field], value)
                self.assertEqual(running, set(PROCESS_START_ORDER))

    def test_chat_limit_update_restarts_chat_server_and_agent(self):
        calls, _, control = self.initialized_processes()
        self.data["chat"]["max_user_message_bytes"] = 32768
        self.write_spec()
        with patch("runtime_reconciler.process._control", side_effect=control), \
                patch("runtime_reconciler.cli.wait_openevent_ready"), \
                patch("runtime_reconciler.resources.OpenEventRuntime", return_value=self.fake), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--spec", str(self.path), "--apply"]), 0)
        self.assertEqual([logical for action, logical in calls if action == "stop"], ["agent", "chat_server"])
        self.assertEqual([logical for action, logical in calls if action == "start"], ["chat_server", "agent"])
        config = json.loads((self.spec.paths.config_dir / "chat-server.json").read_text())
        self.assertEqual(config["max_user_message_bytes"], 32768)

    def test_postcommit_failure_can_start_but_still_requires_openevent_ready(self):
        calls, running, control = self.initialized_processes()
        self.data["model"]["api_key"] = "replacement-key"
        self.write_spec()
        errors = io.StringIO()
        with patch("runtime_reconciler.process._control", side_effect=control), \
                patch("runtime_reconciler.cli.wait_openevent_ready"), \
                patch("runtime_reconciler.resources.OpenEventRuntime", return_value=self.fake), \
                patch("runtime_reconciler.cli.start_configured", side_effect=ApplyError("start failure")), \
                redirect_stderr(errors):
            with self.assertRaisesRegex(ApplyError, "start failure"):
                main(["--spec", str(self.path), "--apply"])
        self.assertNotIn("require operator review", errors.getvalue())
        state = load_state(self.spec.paths.state_path)
        self.assertEqual(state["applied_spec_sha256"], self.spec.fingerprint())
        self.assertEqual(set(state["pending_restarts"]), {"agent", "model_proxy"})
        running.remove("openevent")
        calls.clear()

        def ready(spec):
            calls.append(("ready", "openevent"))
            self.assertIn("openevent", running)

        with patch("runtime_reconciler.process._control", side_effect=control), \
                patch("runtime_reconciler.cli.wait_openevent_ready", side_effect=ready), \
                patch("runtime_reconciler.resources.OpenEventRuntime") as resources, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--spec", str(self.path), "--start"]), 0)
            resources.assert_not_called()
        self.assertLess(calls.index(("ready", "openevent")), calls.index(("start", "model_proxy")))
        self.assertEqual([logical for action, logical in calls if action == "stop"], ["agent", "model_proxy"])
        self.assertEqual(calls[-1], ("start", "agent"))
        self.assertEqual(load_state(self.spec.paths.state_path)["pending_restarts"], [])

    def test_start_pending_server_restart_stops_each_component_once(self):
        calls, running, control = self.initialized_processes()
        state = load_state(self.spec.paths.state_path)
        state["pending_restarts"] = ["openevent"]
        save_state(self.spec, state)

        def ready(spec):
            self.assertEqual(running, {"openevent"})
            calls.append(("ready", "openevent"))

        with patch("runtime_reconciler.process._control", side_effect=control), \
                patch("runtime_reconciler.cli.wait_openevent_ready", side_effect=ready), \
                patch("runtime_reconciler.resources.OpenEventRuntime") as resources, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--spec", str(self.path), "--start"]), 0)
            resources.assert_not_called()
        self.assertCountEqual([logical for action, logical in calls if action == "stop"], PROCESS_START_ORDER)
        self.assertEqual([logical for action, logical in calls if action == "start"], list(PROCESS_START_ORDER))
        self.assertLess(calls.index(("ready", "openevent")), calls.index(("start", "model_proxy")))
        self.assertEqual(running, set(PROCESS_START_ORDER))
        self.assertEqual(load_state(self.spec.paths.state_path)["pending_restarts"], [])

    def test_unknown_status_after_stop_aborts_coordination(self):
        with patch("runtime_reconciler.process._control", return_value=SimpleNamespace(returncode=1, stdout="")):
            with self.assertRaisesRegex(ApplyError, "confirm agent stopped"):
                stop_program(self.spec, "agent")

    def test_partial_config_write_keeps_previous_generation_and_reapply_finishes(self):
        calls, running, control = self.initialized_processes()
        old_configs = load_state(self.spec.paths.state_path)["configs"]
        self.data["model"]["api_key"] = "replacement-key"
        self.data["cmd"] = {"max_concurrent_tasks": 5}
        self.write_spec()

        def fail_cmd_write(path, *args):
            if path.name == "cmd-worker.yaml":
                raise OSError("interrupted halfway through configs")
            return _atomic_write(path, *args)

        with patch("runtime_reconciler.process._control", side_effect=control), \
                patch("runtime_reconciler.cli.wait_openevent_ready"), \
                patch("runtime_reconciler.resources.OpenEventRuntime", return_value=self.fake), \
                patch("runtime_reconciler.state._atomic_write", side_effect=fail_cmd_write):
            with self.assertRaisesRegex(OSError, "halfway"):
                main(["--spec", str(self.path), "--apply"])
        state = load_state(self.spec.paths.state_path)
        self.assertEqual(state["configs"], old_configs)
        self.assertNotIn("applied_spec_sha256", state)
        self.assertEqual(set(state["pending_restarts"]), {"model_proxy", "cmd_worker", "agent"})
        model = yaml.safe_load((self.spec.paths.config_dir / "model-proxy.yaml").read_text())
        self.assertEqual(model["providers"]["default"]["api_key"], "replacement-key")
        self.assertFalse(any(action == "start" for action, _ in calls))
        self.assertEqual(calls, [("stop", "agent")])
        calls.clear()
        with patch("runtime_reconciler.process._control", side_effect=control), \
                patch("runtime_reconciler.cli.wait_openevent_ready"), \
                patch("runtime_reconciler.resources.OpenEventRuntime", return_value=self.fake), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--spec", str(self.path), "--apply"]), 0)
        self.assertEqual([logical for action, logical in calls if action == "stop"],
                         ["agent", "model_proxy", "cmd_worker"])
        self.assertEqual([logical for action, logical in calls if action == "start"],
                         ["model_proxy", "cmd_worker", "agent"])
        self.assertEqual(load_state(self.spec.paths.state_path)["pending_restarts"], [])

    def test_failed_agent_stop_preserves_old_files_and_does_not_start_dependencies(self):
        calls, running, control = self.initialized_processes()
        old_configs = load_state(self.spec.paths.state_path)["configs"]
        old_files = {name: Path(item["path"]).read_bytes() for name, item in old_configs.items()}
        self.data["model"]["api_key"] = "replacement-key"
        self.write_spec()

        def fail_stop(spec, action, logical):
            if action == "stop" and logical == "agent":
                calls.append((action, logical))
                return SimpleNamespace(returncode=1, stdout="could not stop")
            return control(spec, action, logical)

        with patch("runtime_reconciler.process._control", side_effect=fail_stop), \
                patch("runtime_reconciler.cli.wait_openevent_ready"), \
                patch("runtime_reconciler.resources.OpenEventRuntime", return_value=self.fake):
            with self.assertRaisesRegex(ApplyError, "confirm agent stopped"):
                main(["--spec", str(self.path), "--apply"])
        self.assertEqual(calls, [("stop", "agent")])
        state = load_state(self.spec.paths.state_path)
        self.assertEqual(state["configs"], old_configs)
        self.assertNotIn("applied_spec_sha256", state)
        self.assertEqual(set(state["pending_restarts"]), {"agent", "model_proxy"})
        self.assertEqual({name: Path(item["path"]).read_bytes() for name, item in old_configs.items()}, old_files)
        self.assertIn("agent", running)
        calls.clear()
        with patch("runtime_reconciler.process._control", side_effect=control), \
                patch("runtime_reconciler.cli.wait_openevent_ready"), \
                patch("runtime_reconciler.resources.OpenEventRuntime", return_value=self.fake), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--spec", str(self.path), "--apply"]), 0)
        self.assertEqual(calls, [("stop", "agent"), ("stop", "model_proxy"),
                                 ("start", "model_proxy"), ("start", "agent")])
        self.assertEqual(running, set(PROCESS_START_ORDER))

    def test_start_without_pending_still_starts_and_waits_for_openevent_first(self):
        calls, running, control = self.initialized_processes()
        running.clear()
        self.assertEqual(load_state(self.spec.paths.state_path)["pending_restarts"], [])

        def ready(spec):
            self.assertEqual(running, {"openevent"})
            calls.append(("ready", "openevent"))

        with patch("runtime_reconciler.process._control", side_effect=control), \
                patch("runtime_reconciler.cli.wait_openevent_ready", side_effect=ready), \
                patch("runtime_reconciler.resources.OpenEventRuntime") as resources, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--spec", str(self.path), "--start"]), 0)
            resources.assert_not_called()
        self.assertEqual(calls[:2], [("start", "openevent"), ("ready", "openevent")])
        self.assertEqual(calls[-1], ("start", "agent"))
        self.assertFalse(any(action == "stop" for action, _ in calls))

if __name__ == "__main__":
    unittest.main()

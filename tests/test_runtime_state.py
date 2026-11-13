"""Runtime file persistence without invoking other components' config parsers."""

import io
import json
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import yaml

from runtime_reconciler.cli import _apply, main
from runtime_reconciler.common import _atomic_write
from runtime_reconciler.model import ApplyError, ChannelIds, ResolvedRuntime
from runtime_reconciler.render import render_configs
from runtime_reconciler.spec import parse_spec
from runtime_reconciler.state import (
    add_pending_restarts, begin_apply, complete_start, config_record, load_state,
    require_completed_initialization, save_state, write_diagnostics, write_runtime_files,
)

TEMP_ROOT = Path(__file__).resolve().parents[1] / "build" / "runtime-state-tests"


class RuntimeStateTests(unittest.TestCase):
    def setUp(self):
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "stack.yaml"
        data = {
            "version": "chat-runtime.v1",
            "runtime": {"name": "state-test", "root": str(self.root / "runtime")},
            "openevent": {"grpc_addr": "127.0.0.1:9527", "admin_addr": "127.0.0.1:9528"},
            "principals": {"user": 1, "agent": 2, "model_proxy": 3, "cmd_worker": 4},
            "tokens": {"agent": "private-agent-token"},
            "chat": {"web_token": "private-web-token"},
            "model": {"base_url": "https://example.test", "api_key": "private-model-key", "model": "test"},
            "agent": {
                "system_prompt_file": str(TEMP_ROOT.parent.parent / "openevent-stack/prompts/system.txt"),
                "context_compression_prompt_file": str(TEMP_ROOT.parent.parent / "openevent-stack/prompts/context_compression.txt"),
                "context_max_input_bytes": 1048576,
            },
        }
        self.path.write_text(yaml.safe_dump(data), encoding="utf-8")
        self.spec = parse_spec(self.path)
        self.resolved = ResolvedRuntime(
            {name: f"resolved-{name}-token" for name in self.spec.principals},
            ChannelIds(model=71, cmd=72, wal=73),
        )
        self.configs = render_configs(self.spec, self.resolved)
        self.plan = {"actions": [], "configs": {
            key: config_record(self.spec, key, item["path"], item["data"], {})
            for key, item in self.configs.items()
        }}

    def write_files(self):
        # Exercise the same persistence entry point used after validation by
        # _apply; external component parser compatibility is tested separately.
        return write_runtime_files(self.spec, self.resolved, False, prepared=(self.configs, self.plan))

    def test_required_files_and_state_commit_without_diagnostic_files(self):
        self.write_files()
        paths = self.spec.paths
        self.assertFalse(paths.desired_path.exists())
        self.assertFalse(paths.plan_path.exists())
        for item in self.configs.values():
            path = paths.config_dir / item["path"]
            self.assertEqual(yaml.safe_load(path.read_text()), item["data"])
        chat = json.loads((paths.config_dir / "chat-server.json").read_text())
        self.assertEqual({key: chat[key] for key in ("host", "port", "origin")},
                         {"host": "127.0.0.1", "port": 8081, "origin": None})
        self.assertFalse((paths.config_dir / "chat-launch.json").exists())
        state = load_state(paths.state_path, spec=self.spec)
        self.assertNotIn("chat_launch", state)
        self.assertEqual(state["channels"], {"model": 71, "cmd": 72, "wal": 73})
        self.assertEqual(state["configs"], {
            key: {"path": item["path"], "sha256": item["sha256"]}
            for key, item in self.plan["configs"].items()
        })
        self.assertTrue(all(item["changed"] for item in self.plan["configs"].values()))
        self.assertEqual(state["applied_spec_sha256"], self.spec.fingerprint())
        self.assertFalse(state.get("initialized"))
        self.assertEqual(set(state["pending_restarts"]), set(self.configs))

    def test_obsolete_chat_launch_files_are_removed_after_state_commit(self):
        self.write_files()
        paths = self.spec.paths
        old_launch = {"host": "127.0.0.1", "port": 8081, "origin": None}
        sidecar = paths.config_dir / "chat-launch.json"
        sidecar.write_text(json.dumps(old_launch))
        state = load_state(paths.state_path)
        state["chat_launch"] = old_launch
        save_state(self.spec, state)
        with patch("runtime_reconciler.state.save_state", side_effect=OSError("state write failed")):
            with self.assertRaisesRegex(OSError, "state write failed"):
                self.write_files()
        self.assertTrue(sidecar.exists())
        self.assertEqual(load_state(paths.state_path)["chat_launch"], old_launch)
        self.write_files()
        self.assertFalse(sidecar.exists())
        self.assertNotIn("chat_launch", load_state(paths.state_path))

    def test_start_uses_pending_restarts(self):
        for pending in ([], ["model_proxy"]):
            with self.subTest(pending=pending):
                self.write_files()
                complete_start(self.spec)
                configs = load_state(self.spec.paths.state_path)["configs"]
                add_pending_restarts(self.spec, pending)
                output = io.StringIO()
                with patch("runtime_reconciler.cli.ensure_program_running"), \
                        patch("runtime_reconciler.cli.wait_openevent_ready"), \
                        patch("runtime_reconciler.cli.start_configured") as start, redirect_stdout(output):
                    self.assertEqual(main(["--spec", str(self.path), "--start"]), 0)
                self.assertEqual(output.getvalue(), "Waiting for OpenEvent to become ready...\n"
                                 "Services started successfully.\n")
                plan = start.call_args.args[1]
                self.assertEqual({key for key, item in plan["configs"].items() if item["changed"]},
                                 {"model_proxy", "agent"} if pending else set())
                current = load_state(self.spec.paths.state_path)
                self.assertEqual(current["configs"], configs)
                self.assertEqual(current["pending_restarts"], [])
                self.assertEqual(current["applied_spec_sha256"], self.spec.fingerprint())

    def test_apply_boundaries_preserve_restart_intent(self):
        self.write_files()
        complete_start(self.spec)
        original = load_state(self.spec.paths.state_path)
        add_pending_restarts(self.spec, {"view"})
        begin_apply(self.spec, {"model_proxy"})
        state = load_state(self.spec.paths.state_path)
        self.assertNotIn("applied_spec_sha256", state)
        self.assertTrue(state["initialized"])
        self.assertEqual(state["configs"], original["configs"])
        self.assertEqual(set(state["pending_restarts"]), {"view", "model_proxy"})
        self.write_files()
        self.assertTrue(load_state(self.spec.paths.state_path)["pending_restarts"])
        complete_start(self.spec)
        state = load_state(self.spec.paths.state_path)
        self.assertTrue(state["initialized"])
        self.assertEqual(state["pending_restarts"], [])
        self.assertEqual(state["applied_spec_sha256"], self.spec.fingerprint())

    def test_dependency_restart_intent_survives_failure_before_config_write(self):
        self.write_files()
        complete_start(self.spec)
        original = load_state(self.spec.paths.state_path)
        files = {key: Path(item["path"]).read_bytes() for key, item in original["configs"].items()}
        changed = {"actions": [], "configs": {
            key: {**item, "changed": key == "model_proxy"} for key, item in original["configs"].items()
        }}

        def stop(spec, logical):
            self.assertEqual(logical, "agent")
            saved = load_state(spec.paths.state_path)
            self.assertEqual(set(saved["pending_restarts"]), {"agent", "model_proxy"})
            self.assertNotIn("applied_spec_sha256", saved)
            raise ApplyError("Agent stop failed")

        with patch("runtime_reconciler.cli.ensure_program_running"), \
                patch("runtime_reconciler.cli.wait_openevent_ready"), \
                patch("runtime_reconciler.cli.apply_resolve", return_value=self.resolved), \
                patch("runtime_reconciler.cli.prepare_runtime_files", return_value=(self.configs, changed)), \
                patch("runtime_reconciler.cli.stop_program", side_effect=stop):
            with self.assertRaisesRegex(ApplyError, "Agent stop failed"):
                _apply(self.spec, original)
        saved = load_state(self.spec.paths.state_path)
        self.assertTrue(saved["initialized"])
        self.assertNotIn("applied_spec_sha256", saved)
        self.assertEqual(set(saved["pending_restarts"]), {"agent", "model_proxy"})
        self.assertEqual({key: Path(item["path"]).read_bytes() for key, item in saved["configs"].items()}, files)

    def test_explicit_diagnostics_are_redacted_private_and_do_not_change_state(self):
        self.write_files()
        paths = self.spec.paths
        saved = paths.state_path.read_bytes()
        write_diagnostics(self.spec, self.plan)
        visible = paths.desired_path.read_text() + paths.plan_path.read_text()
        for secret in (self.spec.chat["web_token"], self.spec.model["api_key"],
                       *self.spec.tokens.values(), *self.resolved.tokens.values()):
            self.assertNotIn(secret, visible)
        desired = yaml.safe_load(paths.desired_path.read_text())
        self.assertEqual(desired["tokens"]["agent"], "<redacted>")
        self.assertEqual(desired["chat"]["web_token"], "<redacted>")
        self.assertEqual(desired["model"]["api_key"], "<redacted>")
        self.assertEqual(yaml.safe_load(paths.plan_path.read_text()), self.plan)
        for path in (paths.desired_path, paths.plan_path):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(paths.state_path.read_bytes(), saved)

    def test_each_diagnostic_write_failure_warns_and_allows_the_other_file(self):
        self.write_files()
        paths = self.spec.paths
        saved = paths.state_path.read_bytes()
        diagnostics = (paths.desired_path, paths.plan_path)
        for failed in diagnostics:
            with self.subTest(path=failed.name):
                for path in diagnostics:
                    path.unlink(missing_ok=True)

                def write(path, *args):
                    if path == failed:
                        raise OSError("diagnostic write unavailable")
                    return _atomic_write(path, *args)

                errors = io.StringIO()
                with patch("runtime_reconciler.state._atomic_write", side_effect=write), redirect_stderr(errors):
                    write_diagnostics(self.spec, self.plan)
                self.assertIn("warning", errors.getvalue().lower())
                self.assertIn(failed.name, errors.getvalue())
                self.assertFalse(failed.exists())
                self.assertTrue(next(path for path in diagnostics if path != failed).exists())
                self.assertEqual(paths.state_path.read_bytes(), saved)

    def test_required_config_and_state_write_failures_still_raise(self):
        paths = self.spec.paths
        for failed in (paths.config_dir / "chat-model-agent.json", paths.state_path):
            with self.subTest(path=failed.name):
                def write(path, *args):
                    if path == failed:
                        raise OSError("required write failed")
                    return _atomic_write(path, *args)

                with patch("runtime_reconciler.state._atomic_write", side_effect=write):
                    with self.assertRaisesRegex(OSError, "required write failed"):
                        self.write_files()
                self.assertFalse(paths.state_path.exists())

    def test_diagnostics_alone_do_not_block_initialization(self):
        write_diagnostics(self.spec, self.plan)
        begin_apply(self.spec)
        state = load_state(self.spec.paths.state_path, spec=self.spec)
        self.assertNotIn("applied_spec_sha256", state)
        self.assertFalse(state.get("initialized"))
        with self.assertRaisesRegex(ApplyError, "initialization did not complete"):
            require_completed_initialization(state)

    def test_saved_secrets_without_state_still_block_initialization(self):
        paths = self.spec.paths
        paths.config_dir.mkdir(parents=True)
        paths.secrets_path.write_text("version: chat-runtime.secrets.v1\n")
        with self.assertRaisesRegex(ApplyError, "missing its runtime state"):
            begin_apply(self.spec)
        self.assertFalse(paths.state_path.exists())

    def test_cli_writes_diagnostics_only_after_explicit_successful_apply(self):
        order = []
        output = io.StringIO()

        def export(spec, plan):
            order.append("diagnostics")
            write_diagnostics(spec, plan)

        with patch("runtime_reconciler.cli._apply", side_effect=lambda *args: order.append("apply") or self.plan), \
                patch("runtime_reconciler.cli.write_diagnostics", side_effect=export) as diagnostics, \
                redirect_stdout(output):
            self.assertEqual(main(["--spec", str(self.path), "--apply"]), 0)
            self.assertEqual(output.getvalue(), "[1/5] Checking configuration...\n"
                             "Configuration applied successfully.\n")
            diagnostics.assert_not_called()
            self.assertFalse(self.spec.paths.plan_path.exists())
            order.clear()
            output.seek(0)
            output.truncate(0)
            self.assertEqual(main(["--spec", str(self.path), "--apply", "--write-diagnostics"]), 0)
            self.assertEqual(output.getvalue(), "[1/5] Checking configuration...\n"
                             "Configuration applied successfully.\n")
            self.assertEqual(order, ["apply", "diagnostics"])
            diagnostics.assert_called_once_with(self.spec, self.plan)
            self.assertEqual(yaml.safe_load(self.spec.paths.plan_path.read_text()), self.plan)

    def test_cli_does_not_write_diagnostics_after_failed_apply(self):
        output = io.StringIO()
        with patch("runtime_reconciler.cli._apply", side_effect=ApplyError("apply failed")), \
                patch("runtime_reconciler.cli.write_diagnostics") as diagnostics, redirect_stdout(output):
            with self.assertRaisesRegex(ApplyError, "apply failed"):
                main(["--spec", str(self.path), "--apply", "--write-diagnostics"])
            diagnostics.assert_not_called()
            self.assertEqual(output.getvalue(), "[1/5] Checking configuration...\n")

    def test_cli_rejects_diagnostics_for_other_modes_before_runtime_writes(self):
        for mode in (["--dry-run"], ["--start"], ["--print-config", "agent"]):
            with self.subTest(mode=mode), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    main(["--spec", str(self.path), "--write-diagnostics", *mode])
                self.assertEqual(error.exception.code, 2)
                self.assertFalse(self.spec.paths.runtime_root.exists())


if __name__ == "__main__":
    unittest.main()

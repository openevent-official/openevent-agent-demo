from __future__ import annotations
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
import yaml

from runtime_reconciler.model import ChannelIds, ResolvedRuntime, SpecError
from runtime_reconciler.spec import parse_spec
from runtime_reconciler.state import write_runtime_files
from test_reconcile_runtime import spec_data

ROOT = Path(__file__).resolve().parents[1]

class StackScriptTests(unittest.TestCase):
    def _install_fake_process_manager(self, stack, mode, *, managed=True, extra=False, program="model-proxy"):
        """Run the real shell dispatch with fake processes, signals, and clock."""
        for filename in ("process.sh", "common.sh", "env.sh"):
            shutil.copy(ROOT / "openevent-stack" / filename, stack / filename)
        (stack / "runtime/config").mkdir(parents=True)
        (stack / "runtime/run").mkdir()
        (stack / "runtime/run" / f"{program}.pid").write_text("424242\n")
        mocks = r'''
fake_alive=1
fake_killed=0
fake_kill_checks=0
pid_alive() {
  if [ "$fake_killed" = 1 ]; then
    fake_kill_checks=$((fake_kill_checks + 1))
    printf 'check-after-kill\n' >> "$STACK_DIR/observations"
    if [ "$FAKE_MODE" = kill_delayed_exit ] && [ "$fake_kill_checks" -ge 3 ]; then
      fake_alive=0
    fi
  fi
  [ "$fake_alive" = 1 ]
}
pid_matches_program() {
  [ "$FAKE_MANAGED" = 1 ] && [ "$2" = 424242 ]
}
matching_pids() {
  if [ "$FAKE_EXTRA" = 1 ]; then printf '424243\n'; fi
}
kill() {
  if [ "$1" = -9 ]; then
    printf 'KILL\n' >> "$STACK_DIR/observations"
    fake_killed=1
    case "$FAKE_MODE" in
      kill_failure) return 1 ;;
      kill_exit_race) fake_alive=0; return 1 ;;
    esac
  else
    printf 'TERM\n' >> "$STACK_DIR/observations"
    case "$FAKE_MODE" in
      term_failure) return 1 ;;
      term_exit_race) fake_alive=0; return 1 ;;
    esac
  fi
  return 0
}
sleep() {
  SECONDS=$((SECONDS + 1))
  if [ "$FAKE_MODE" = term_delayed_exit ] && [ "$SECONDS" -ge 20 ]; then fake_alive=0; fi
}
command_for() { printf 'launch\n' >> "$STACK_DIR/observations"; return 1; }
'''
        process = stack / "process.sh"
        source = process.read_text()
        dispatch = '\naction="${1:-status}"'
        self.assertEqual(source.count(dispatch), 1)
        process.write_text(source.replace(dispatch, mocks + dispatch))
        return str(process), {**os.environ, "PYTHON_BIN": sys.executable,
                              "FAKE_MODE": mode, "FAKE_MANAGED": str(int(managed)),
                              "FAKE_EXTRA": str(int(extra))}

    def test_generated_environment_precedes_defaults_and_preserves_explicit_overrides(self):
        build = ROOT / "build/stack-tests"
        build.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=build) as directory:
            stack = Path(directory) / "openevent-stack"
            stack.mkdir()
            for filename in ("common.sh", "env.sh"):
                shutil.copy(ROOT / "openevent-stack" / filename, stack / filename)
            config = stack / "runtime/config"
            config.mkdir(parents=True)
            custom = Path(directory) / "custom-workdir"
            selected = (str(custom / "venv/bin/python"), str(custom / "sources/openevent/build/openevent_server"))
            (config / "env.sh").write_text(
                'PYTHON_BIN="${PYTHON_BIN:-' + selected[0] + '}"\n'
                'OPENEVENT_SERVER_BIN="${OPENEVENT_SERVER_BIN:-' + selected[1] + '}"\n')
            clean_env = {k: v for k, v in os.environ.items()
                         if k not in {"PYTHON_BIN", "OPENEVENT_SERVER_BIN", "RUNTIME_ROOT"}}
            command = ["bash", "-c", 'source "$1"; printf "%s\\n" "$PYTHON_BIN" "$OPENEVENT_SERVER_BIN"',
                       "test", str(stack / "common.sh")]
            cases = (({}, selected), ({"PYTHON_BIN": "/explicit/python", "OPENEVENT_SERVER_BIN": "/explicit/server"},
                                      ("/explicit/python", "/explicit/server")))
            for overrides, expected in cases:
                with self.subTest(overrides=overrides):
                    result = subprocess.run(command, env={**clean_env, **overrides},
                                            capture_output=True, text=True, check=True)
                    self.assertEqual(result.stdout.splitlines(), list(expected))
            self.assertEqual({path.name for path in stack.iterdir()}, {"common.sh", "env.sh", "runtime"})
            self.assertEqual({path.name for path in (stack / "runtime").iterdir()},
                             {"config", "data", "logs", "run", "workdir"})

    def test_start_result_reports_service_status_and_configured_urls(self):
        build = ROOT / "build/stack-tests"
        build.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=build) as directory:
            stack = Path(directory) / "openevent-stack"
            stack.mkdir()
            for filename in ("common.sh", "env.sh"):
                shutil.copy(ROOT / "openevent-stack" / filename, stack / filename)
            process = stack / "process.sh"
            process.write_text(
                '#!/bin/sh\n[ "$#" -eq 2 ] && [ "$1" = status ] && [ "$2" = all ] || exit 98\n'
                "printf '%s\\n' 'openevent RUNNING pid 101' 'chat-model-agent RUNNING pid 202'\n"
                'exit "${FAKE_STATUS_CODE:-0}"\n')
            process.chmod(0o755)
            config = stack / "runtime/config"
            config.mkdir(parents=True)
            command = ["bash", "-c", 'source "$1"; print_start_result',
                       "test", str(stack / "common.sh")]
            environment = {**os.environ, "PYTHON_BIN": sys.executable, "FAKE_STATUS_CODE": "0"}
            status_lines = ["Services:", "openevent RUNNING pid 101", "chat-model-agent RUNNING pid 202"]
            cases = (
                ({"host": "0.0.0.0", "port": 8081}, {"host": "0.0.0.0", "port": 9999},
                 "http://127.0.0.1:8081/api/chat/", "http://127.0.0.1:9999/"),
                ({"host": "chat.example.test", "port": 18081}, {"host": "192.0.2.10", "port": 19999},
                 "http://chat.example.test:18081/api/chat/", "http://192.0.2.10:19999/"),
                ({"host": "0.0.0.0", "port": 8081, "origin": "https://chat.example.test/"},
                 {"host": "0.0.0.0", "port": 9999},
                 "https://chat.example.test/api/chat/", "http://127.0.0.1:9999/"),
                ({"host": "::", "port": 8081}, {"host": "2001:db8::10", "port": 9999},
                 "http://[::1]:8081/api/chat/", "http://[2001:db8::10]:9999/"),
                ({"host": "2001:db8::20", "port": 18081}, {"host": "::", "port": 19999},
                 "http://[2001:db8::20]:18081/api/chat/", "http://[::1]:19999/"),
            )
            for chat, view, chat_url, view_url in cases:
                with self.subTest(chat=chat, view=view):
                    (config / "chat-server.json").write_text(json.dumps({
                        **chat, "web_token": "private-web-token", "user_openevent_token": "private-user-token",
                    }))
                    (config / "openevent-view.yaml").write_text(yaml.safe_dump({"server": view}))
                    result = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=5)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    lines = [line for line in result.stdout.splitlines() if line]
                    self.assertEqual(lines[:3], status_lines)
                    self.assertEqual(len(lines), 5, result.stdout)
                    self.assertTrue(lines[3].endswith(chat_url), result.stdout)
                    self.assertTrue(lines[4].endswith(view_url), result.stdout)
                    self.assertNotIn("private-", result.stdout + result.stderr)
            # A failed status query must return before reading either address configuration.
            (config / "chat-server.json").unlink()
            (config / "openevent-view.yaml").unlink()
            result = subprocess.run(command, env={**environment, "FAKE_STATUS_CODE": "4"},
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
            self.assertEqual([line for line in result.stdout.splitlines() if line], status_lines)
            self.assertEqual(result.stderr, "")

    def test_chat_start_command_uses_only_the_unified_config(self):
        build = ROOT / "build/stack-tests"
        build.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=build) as directory:
            stack = Path(directory) / "stack with spaces"
            stack.mkdir()
            for filename in ("process.sh", "common.sh", "env.sh"):
                shutil.copy(ROOT / "openevent-stack" / filename, stack / filename)
            process = stack / "process.sh"
            source = process.read_text()
            dispatch = '\naction="${1:-status}"'
            self.assertEqual(source.count(dispatch), 1)
            process.write_text(source.replace(dispatch, '\ncommand_for chat-server\nexit\n' + dispatch))
            config = stack / "runtime/config/chat-server.json"
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps({"host": "0.0.0.0", "port": 8090,
                                          "origin": "https://chat.example.test"}))
            result = subprocess.run([str(process)], env={**os.environ, "PYTHON_BIN": sys.executable},
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stdout.split("\0"), [
                sys.executable, "-B", "-m", "openevent.chat_app", "--config", str(config), "",
            ])
            self.assertEqual(result.stderr, "")

    def test_stack_entrypoints_pass_the_same_resolved_runtime_directory(self):
        build = ROOT / "build/stack-tests"
        build.mkdir(parents=True, exist_ok=True)
        for linked_runtime in (False, True):
            with self.subTest(linked_runtime=linked_runtime), tempfile.TemporaryDirectory(dir=build) as directory:
                stack = Path(directory) / "openevent-stack"
                stack.mkdir()
                for filename in ("common.sh", "env.sh", "bootstrap.sh", "start.sh"):
                    shutil.copy(ROOT / "openevent-stack" / filename, stack / filename)
                runtime = stack / "runtime"
                if linked_runtime:
                    runtime = Path(directory) / "runtime-target"
                    runtime.mkdir()
                    (stack / "runtime").symlink_to(runtime, target_is_directory=True)
                recorded = Path(directory) / "arguments.json"
                python = Path(directory) / "record-python"
                python.write_text(
                    f"#!{sys.executable} -B\nimport json,sys\nfrom pathlib import Path\n"
                    "Path(" + repr(str(recorded)) + ").write_text(json.dumps(sys.argv[1:]))\n"
                    "raise SystemExit(77)\n")
                python.chmod(0o755)
                for filename, options, mode in (("bootstrap.sh", ["--dry-run"], "--dry-run"),
                                                ("start.sh", [], "--start")):
                    with self.subTest(entrypoint=filename):
                        result = subprocess.run([str(stack / filename), *options],
                                                env={**os.environ, "PYTHON_BIN": str(python)},
                                                capture_output=True, text=True, timeout=5)
                        self.assertEqual(result.returncode, 77, result.stdout + result.stderr)
                        args = json.loads(recorded.read_text())
                        self.assertEqual(args[args.index("-m") + 1], "runtime_reconciler.cli")
                        self.assertEqual(args[args.index("--spec") + 1], str(stack / "stack.yaml"))
                        self.assertEqual(args[args.index("--runtime-root") + 1], str(runtime))
                        self.assertIn(mode, args)
                self.assertEqual({path.name for path in stack.iterdir() if path.is_dir()}, {"runtime"})

    def test_stack_spec_is_chat_only(self):
        template = ROOT / "openevent-stack/stack.yaml"
        runtime = template.parent / "runtime"
        spec = parse_spec(template, runtime)
        self.assertEqual(Path(spec.openevent["storage_path"]), runtime / "data/openevent")
        self.assertEqual(Path(spec.chat["channels_dir"]), runtime / "chat-sessions")
        self.assertEqual(Path(spec.cmd["output_dir"]), runtime / "cmd-output")
        self.assertEqual(Path(spec.agent["work_root"]), runtime / "workdir")
        build = ROOT / "build/stack-tests"
        build.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=build) as directory:
            stack = Path(directory)
            source = stack / "stack.yaml"
            for channels in (None, {"cmd": 90}):
                for cmd in (None, {"max_concurrent_tasks": 2, "default_timeout_ms": 1000}):
                    with self.subTest(channels=channels, cmd=cmd):
                        data = spec_data(stack / "runtime")
                        data.pop("channels")
                        if channels is not None:
                            data["channels"] = channels
                        if cmd is not None:
                            data["cmd"] = cmd
                        source.write_text(yaml.safe_dump(data), encoding="utf-8")
                        spec = parse_spec(source)
                        self.assertEqual(spec.channels, channels or {})
                        expected = cmd or {"max_concurrent_tasks": 4, "default_timeout_ms": 300000}
                        for name, value in expected.items():
                            self.assertEqual(spec.cmd[name], value)
            for field in ("im", "sessions"):
                with self.subTest(legacy_field=field):
                    data = spec_data(stack / "runtime")
                    data[field] = {}
                    source.write_text(yaml.safe_dump(data), encoding="utf-8")
                    with self.assertRaisesRegex(SpecError, "unknown spec fields: " + field):
                        parse_spec(source)

    def test_process_manager_rejects_unordered_start_all(self):
        build = ROOT / "build/stack-tests"
        build.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=build) as directory:
            stack = Path(directory) / "openevent-stack"
            stack.mkdir()
            for filename in ("process.sh", "common.sh", "env.sh"):
                shutil.copy(ROOT / "openevent-stack" / filename, stack / filename)
            result = subprocess.run([str(stack / "process.sh"), "start", "all"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("start.sh or stop.sh", result.stderr)

    def test_normal_stop_waits_for_agent_before_dependencies(self):
        build = ROOT / "build/stack-tests"
        build.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=build) as directory:
            stack = Path(directory)
            shutil.copy(ROOT / "openevent-stack/stop.sh", stack / "stop.sh")
            log = stack / "calls"
            process = stack / "process.sh"
            process.write_text("#!/bin/sh\nprintf '%s %s\\n' \"$1\" \"$2\" >> " + str(log) + "\n")
            process.chmod(0o755)
            subprocess.run([str(stack / "stop.sh")], check=True)
            lines = log.read_text().splitlines()
        self.assertEqual(lines[0], "stop chat-model-agent")
        self.assertEqual(lines[-1], "stop openevent")
        self.assertIn("stop cmd-worker", lines)

    def test_failed_agent_stop_keeps_dependencies_running(self):
        build = ROOT / "build/stack-tests"
        build.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=build) as directory:
            stack = Path(directory)
            shutil.copy(ROOT / "openevent-stack/stop.sh", stack / "stop.sh")
            log = stack / "calls"
            process = stack / "process.sh"
            process.write_text("#!/bin/sh\nprintf '%s %s\\n' \"$1\" \"$2\" >> "
                               + shlex.quote(str(log)) + "\nexit 1\n")
            process.chmod(0o755)
            result = subprocess.run([str(stack / "stop.sh")], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(log.read_text().splitlines(), ["stop chat-model-agent"])

    def test_failed_signals_and_unconfirmed_exit_preserve_process_records(self):
        build = ROOT / "build/stack-tests"
        build.mkdir(parents=True, exist_ok=True)
        cases = (("term_failure", "failed to send SIGTERM"),
                 ("kill_failure", "failed to send SIGKILL"),
                 ("kill_stuck", "still running 2 seconds after SIGKILL"))
        for action in ("stop", "restart"):
            for mode, message in cases:
                with self.subTest(action=action, mode=mode), tempfile.TemporaryDirectory(dir=build) as directory:
                    stack = Path(directory)
                    process, environment = self._install_fake_process_manager(stack, mode)
                    result = subprocess.run([process, action, "model-proxy"], env=environment,
                                            capture_output=True, text=True, timeout=5)
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn(message, result.stderr)
                    self.assertNotIn("STOPPED", result.stdout)
                    self.assertTrue((stack / "runtime/run/model-proxy.pid").exists())
                    observations = (stack / "observations").read_text().splitlines()
                    self.assertNotIn("launch", observations)
                    if mode == "term_failure":
                        self.assertEqual(observations, ["TERM"])
                    if mode == "kill_stuck":
                        self.assertEqual(observations.count("check-after-kill"), 3)

    def test_stop_confirms_delayed_exit_and_accepts_signal_exit_races(self):
        build = ROOT / "build/stack-tests"
        build.mkdir(parents=True, exist_ok=True)
        for mode in ("kill_delayed_exit", "term_exit_race", "kill_exit_race"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory(dir=build) as directory:
                stack = Path(directory)
                process, environment = self._install_fake_process_manager(stack, mode)
                result = subprocess.run([process, "stop", "model-proxy"], env=environment,
                                        capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("model-proxy STOPPED", result.stdout)
                self.assertFalse((stack / "runtime/run/model-proxy.pid").exists())
                observations = (stack / "observations").read_text().splitlines()
                if mode == "kill_delayed_exit":
                    self.assertEqual(observations.count("check-after-kill"), 3)
                if mode == "term_exit_race":
                    self.assertEqual(observations, ["TERM"])

    def test_failed_stale_process_cleanup_stops_start_and_stop(self):
        build = ROOT / "build/stack-tests"
        build.mkdir(parents=True, exist_ok=True)
        for action, managed in (("stop", False), ("start", False), ("start", True)):
            with self.subTest(action=action, managed=managed), tempfile.TemporaryDirectory(dir=build) as directory:
                stack = Path(directory)
                process, environment = self._install_fake_process_manager(
                    stack, "term_failure", managed=managed, extra=True)
                result = subprocess.run([process, action, "model-proxy"], env=environment,
                                        capture_output=True, text=True, timeout=5)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("failed to send SIGTERM to pid 424243", result.stderr)
                self.assertNotIn("cleaned stale pid", result.stderr)
                self.assertNotIn("STOPPED", result.stdout)
                self.assertNotIn("RUNNING", result.stdout)
                self.assertTrue((stack / "runtime/run/model-proxy.pid").exists())
                self.assertEqual((stack / "observations").read_text().splitlines(), ["TERM"])

    def test_directory_symlink_entries_share_process_status_and_stop(self):
        build = ROOT / "build/stack-tests"
        build.mkdir(parents=True, exist_ok=True)
        for start_from_alias in (False, True):
            with self.subTest(start_from_alias=start_from_alias), tempfile.TemporaryDirectory(dir=build) as directory:
                project = Path(directory) / "real-project"
                stack = project / "openevent-stack"
                stack.mkdir(parents=True)
                for filename in ("process.sh", "common.sh", "env.sh", "status.sh", "stop.sh"):
                    shutil.copy(ROOT / "openevent-stack" / filename, stack / filename)
                alias = Path(directory) / "linked-project"
                alias.symlink_to(project, target_is_directory=True)
                linked_stack = alias / "openevent-stack"
                launch_stack, control_stack = (linked_stack, stack) if start_from_alias else (stack, linked_stack)
                config = stack / "runtime/config/chat-model-agent.json"
                config.parent.mkdir(parents=True)
                config.write_text(json.dumps({"agent": {}}))
                modules = project / "fake-modules"
                package = modules / "chat_model_agent"
                package.mkdir(parents=True)
                (package / "__init__.py").write_text("")
                stopped = stack / "fake-agent-stopped"
                (package / "cli.py").write_text(
                    "import signal\nfrom pathlib import Path\n"
                    "def stop(*args):\n"
                    "    Path(" + repr(str(stopped)) + ").write_text('stopped')\n"
                    "    raise SystemExit()\n"
                    "signal.signal(signal.SIGTERM, stop)\n"
                    "while True: signal.pause()\n")
                environment = {**os.environ, "PYTHON_BIN": sys.executable,
                               "EXTRA_PYTHONPATH": str(modules)}
                pid_path = stack / "runtime/run/chat-model-agent.pid"
                pid = None

                def alive():
                    if pid is None:
                        return False
                    try:
                        state = (Path("/proc") / str(pid) / "status").read_text()
                    except FileNotFoundError:
                        return False
                    return not any(line.startswith("State:") and "Z" in line.split()[1:2]
                                   for line in state.splitlines())

                try:
                    started = subprocess.run([str(launch_stack / "process.sh"), "start", "chat-model-agent"],
                                             env=environment, capture_output=True, text=True, timeout=10)
                    if pid_path.exists():
                        pid = int(pid_path.read_text())
                    self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
                    self.assertIsNotNone(pid)
                    self.assertEqual((Path("/proc") / str(pid) / "cwd").resolve(),
                                     stack / "runtime/workdir")
                    self.assertTrue((stack / "runtime/logs/chat-model-agent.log").exists())
                    self.assertEqual({path.name for path in stack.iterdir() if path.is_dir()}, {"runtime"})
                    for entry in (launch_stack, control_stack):
                        status = subprocess.run([str(entry / "status.sh")], env=environment,
                                                capture_output=True, text=True, timeout=10)
                        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
                        self.assertIn(f"chat-model-agent RUNNING pid {pid}", status.stdout)
                    result = subprocess.run([str(control_stack / "stop.sh")], env=environment,
                                            capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertTrue(stopped.exists(), "the alternate entry did not signal the running Agent")
                    self.assertFalse(alive(), "stop succeeded while the Agent was still running")
                    self.assertFalse(pid_path.exists())
                finally:
                    subprocess.run([str(launch_stack / "process.sh"), "stop", "chat-model-agent"],
                                   env=environment, capture_output=True, timeout=10)
                    if alive():
                        os.kill(pid, signal.SIGKILL)

    def test_process_scan_resolves_its_config_path_once(self):
        build = ROOT / "build/stack-tests"
        build.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=build) as directory:
            stack = Path(directory)
            for filename in ("process.sh", "common.sh", "env.sh"):
                shutil.copy(ROOT / "openevent-stack" / filename, stack / filename)
            process = stack / "process.sh"
            source = process.read_text()
            self.assertEqual(source.count("config_path_for() {"), 1)
            source = source.replace("config_path_for() {", "original_config_path_for() {")
            dispatch = '\naction="${1:-status}"'
            self.assertEqual(source.count(dispatch), 1)
            instrumentation = r'''
config_path_for() {
  printf '%s\n' "$1" >> "$STACK_DIR/config-lookups"
  original_config_path_for "$@"
}
matching_pids chat-server || true
exit
'''
            process.write_text(source.replace(dispatch, instrumentation + dispatch))
            result = subprocess.run([str(process)], env={**os.environ, "PYTHON_BIN": sys.executable},
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual((stack / "config-lookups").read_text().splitlines(), ["chat-server"])
            self.assertEqual(result.stdout, "")

    def test_process_scan_finds_duplicate_and_orphaned_instances(self):
        build = ROOT / "build/stack-tests"
        build.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=build) as directory:
            stack = Path(directory)
            for filename in ("process.sh", "common.sh", "env.sh"):
                shutil.copy(ROOT / "openevent-stack" / filename, stack / filename)
            (stack / "runtime/run").mkdir(parents=True)
            config = stack / "runtime/config/chat-model-agent.json"
            config.parent.mkdir()
            config.write_text("{}")
            children = []
            try:
                for _ in range(2):
                    children.append(subprocess.Popen([
                        sys.executable, "-B", "-c", "import signal; signal.pause()",
                        "-m", "chat_model_agent.cli", "--config", str(config),
                    ]))
                pid_path = stack / "runtime/run/chat-model-agent.pid"
                pid_path.write_text(str(children[0].pid))
                command = [str(stack / "process.sh"), "status", "chat-model-agent"]
                result = subprocess.run(command, capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
                self.assertEqual(result.stdout.strip(),
                                 f"chat-model-agent RUNNING pid {children[0].pid} DUPLICATE pids {children[1].pid}")
                pid_path.unlink()
                result = subprocess.run(command, capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
                prefix = "chat-model-agent ORPHANED pids "
                self.assertTrue(result.stdout.startswith(prefix), result.stdout)
                self.assertEqual(set(result.stdout.removeprefix(prefix).strip().split(",")),
                                 {str(child.pid) for child in children})
                self.assertTrue(all(child.poll() is None for child in children))
            finally:
                for child in children:
                    child.terminate()
                    child.wait(timeout=5)

    def test_stale_pid_never_reports_or_stops_an_unrelated_process(self):
        build = ROOT / "build/stack-tests"
        build.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=build) as directory:
            stack = Path(directory)
            for filename in ("process.sh", "common.sh", "env.sh"):
                shutil.copy(ROOT / "openevent-stack" / filename, stack / filename)
            (stack / "runtime/run").mkdir(parents=True)
            process = str(stack / "process.sh")
            unrelated = subprocess.Popen([sys.executable, "-B", "-c", "import time; time.sleep(30)"])
            try:
                pid_path = stack / "runtime/run/chat-model-agent.pid"
                pid_path.write_text(str(unrelated.pid))
                status = subprocess.run([process, "status", "chat-model-agent"], capture_output=True,
                                        text=True, timeout=5)
                self.assertEqual(status.returncode, 3, status.stdout + status.stderr)
                self.assertIn("STOPPED", status.stdout)
                subprocess.run([process, "stop", "chat-model-agent"], capture_output=True,
                               text=True, check=True, timeout=5)
                self.assertIsNone(unrelated.poll(), "stale PID caused an unrelated process to be stopped")
                self.assertFalse(pid_path.exists())
            finally:
                unrelated.terminate()
                unrelated.wait(timeout=5)

    def test_process_is_not_declared_dead_when_signals_are_denied(self):
        build = ROOT / "build/stack-tests"
        build.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=build) as directory:
            stack = Path(directory)
            for filename in ("process.sh", "common.sh", "env.sh"):
                shutil.copy(ROOT / "openevent-stack" / filename, stack / filename)
            (stack / "runtime/config").mkdir(parents=True)
            (stack / "runtime/run").mkdir()
            config = stack / "runtime/config/chat-model-agent.json"
            config.write_text(json.dumps({"agent": {}}))
            denied_signals = stack / "denied-signals.sh"
            denied_signals.write_text("kill() { return 1; }\n")
            child = subprocess.Popen([sys.executable, "-B", "-c", "import time; time.sleep(30)",
                                      "-m", "chat_model_agent.cli", "--config", str(config)])
            try:
                pid_path = stack / "runtime/run/chat-model-agent.pid"
                pid_path.write_text(str(child.pid))
                result = subprocess.run([str(stack / "process.sh"), "stop", "chat-model-agent"],
                                        env={**os.environ, "PYTHON_BIN": sys.executable,
                                             "BASH_ENV": str(denied_signals)},
                                        capture_output=True, text=True, timeout=5)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("failed to send SIGTERM", result.stderr)
                self.assertNotIn("STOPPED", result.stdout)
                self.assertIsNone(child.poll())
                self.assertTrue(pid_path.exists())
            finally:
                child.terminate()
                child.wait(timeout=5)

    def test_cmd_start_uses_generated_object_environment_and_rejects_missing_values(self):
        build = ROOT / "build/stack-tests"
        build.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=build) as directory:
            stack = Path(directory)
            for filename in ("process.sh", "common.sh", "env.sh"):
                shutil.copy(ROOT / "openevent-stack" / filename, stack / filename)
            runtime = stack / "runtime"
            source = stack / "spec.yaml"
            source.write_text(yaml.safe_dump({
                "version": "chat-runtime.v1", "runtime": {"name": "test", "root": str(runtime)},
                "openevent": {"grpc_addr": "127.0.0.1:9527", "admin_addr": "127.0.0.1:9528"},
                "principals": {"user": 1, "agent": 2, "model_proxy": 3, "cmd_worker": 4},
                "chat": {"web_token": "test-web-token"},
                "model": {"base_url": "https://example.test", "api_key": "test-key", "model": "test"},
                "agent": {**spec_data(runtime)["agent"], "object_target": "objects.example.test:9527"},
            }))
            spec = parse_spec(source)
            resolved = ResolvedRuntime(
                {name: f"test-{name}-token" for name in spec.principals},
                ChannelIds(model=41, cmd=42, wal=43),
            )
            write_runtime_files(spec, resolved, False)
            agent_config = json.loads((runtime / "config/chat-model-agent.json").read_text())
            self.assertNotIn("object_target", agent_config["agent"])
            self.assertEqual(Path(spec.agent["work_root"]), runtime / "workdir")
            self.assertEqual(Path(spec.chat["channels_dir"]), runtime / "chat-sessions")
            cmd_config = yaml.safe_load((runtime / "config/cmd-worker.yaml").read_text())
            self.assertEqual(Path(cmd_config["output_dir"]), runtime / "cmd-output")
            marker = stack / "observed-environment.json"
            worker = stack / "fake_worker.py"
            worker.write_text(
                "import json,os,time\nfrom pathlib import Path\n"
                "Path(" + repr(str(marker)) + ").write_text(json.dumps({key: os.environ.get(key) for key in "
                "('OPENEVENT_OBJECT_TARGET',)}))\n"
                "while True: time.sleep(.05)\n")
            python = stack / "python"
            python.write_text(
                '#!/bin/sh\nif [ "$1" = -m ] && [ "$2" = openevent.cmd_worker.cli ]; then\n'
                + "exec " + shlex.quote(sys.executable) + " -B " + shlex.quote(str(worker)) + ' "$@"\nfi\n'
                + "exec " + shlex.quote(sys.executable) + ' "$@"\n')
            python.chmod(0o755)
            environment = {**os.environ, "PYTHON_BIN": str(python)}
            process = str(stack / "process.sh")
            try:
                result = subprocess.run([process, "start", "cmd-worker"], env=environment,
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(json.loads(marker.read_text()), {
                    "OPENEVENT_OBJECT_TARGET": spec.agent["object_target"],
                })
                self.assertNotIn("Traceback", (runtime / "logs/cmd-worker.log").read_text())
                self.assertEqual({path.name for path in stack.iterdir() if path.is_dir()}, {"runtime"})
            finally:
                subprocess.run([process, "stop", "cmd-worker"], env=environment,
                               capture_output=True, text=True, check=True, timeout=10)
            marker.unlink(missing_ok=True)
            state = spec.paths.state_path
            invalid_states = (None, "invalid: [yaml", "{}", yaml.safe_dump({"cmd_environment": {
                "object_target": ""}}))
            for contents in invalid_states:
                with self.subTest(contents=contents):
                    if contents is None:
                        state.unlink()
                    else:
                        state.write_text(contents)
                    result = subprocess.run([process, "start", "cmd-worker"], env=environment,
                                            capture_output=True, text=True, timeout=10)
                    try:
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn("could not load Cmd object reader environment", result.stderr)
                        self.assertNotIn("RUNNING", result.stdout)
                        self.assertFalse(marker.exists())
                        self.assertFalse((runtime / "run/cmd-worker.pid").exists())
                    finally:
                        if (runtime / "run/cmd-worker.pid").exists():
                            subprocess.run([process, "stop", "cmd-worker"], env=environment,
                                           capture_output=True, timeout=10)

    def test_agent_stop_waits_for_exit_without_forcing_a_deadline(self):
        build = ROOT / "build/stack-tests"
        build.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=build) as directory:
            stack = Path(directory)
            process, environment = self._install_fake_process_manager(
                stack, "term_delayed_exit", program="chat-model-agent")
            result = subprocess.run([process, "stop", "chat-model-agent"], env=environment,
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("chat-model-agent STOPPED", result.stdout)
            self.assertFalse((stack / "runtime/run/chat-model-agent.pid").exists())
            self.assertEqual((stack / "observations").read_text().splitlines(), ["TERM"])

if __name__ == "__main__":
    unittest.main()

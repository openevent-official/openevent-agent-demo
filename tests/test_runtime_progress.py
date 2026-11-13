"""Progress stays visible while reconciliation or process control is waiting."""

import io
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import yaml

from runtime_reconciler.cli import main
from runtime_reconciler.model import ApplyError
from runtime_reconciler.process import ensure_program_running
from test_reconcile_runtime import FakeRuntime, spec_data


ROOT = Path(__file__).resolve().parents[1]
STAGES = [
    "[1/5] Checking configuration...",
    "[2/5] Preparing OpenEvent...",
    "[3/5] Preparing access tokens and channels...",
    "[4/5] Preparing service configuration...",
    "[5/5] Updating services...",
]


class RuntimeProgressTests(unittest.TestCase):
    def test_apply_reports_stages_in_order_without_secrets_and_stops_at_failure(self):
        build = ROOT / "build/runtime-progress-tests"
        build.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=build) as directory:
            root = Path(directory)
            source = root / "stack.yaml"
            data = spec_data(root / "runtime")
            source.write_text(yaml.safe_dump(data))
            runtime = FakeRuntime()
            running = set()

            def control(spec, action, logical):
                if action == "start":
                    running.add(logical)
                elif action == "stop":
                    running.discard(logical)
                return SimpleNamespace(returncode=0 if logical in running or action == "stop" else 3,
                                       stdout="RUNNING" if logical in running else "STOPPED")

            with patch("runtime_reconciler.process._control", side_effect=control), \
                    patch("runtime_reconciler.cli.wait_openevent_ready"), \
                    patch("runtime_reconciler.resources.OpenEventRuntime", return_value=runtime):
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(main(["--spec", str(source), "--apply"]), 0)
                lines = output.getvalue().splitlines()
                self.assertEqual([line for line in lines if line.startswith("[")], STAGES)
                self.assertEqual(lines[-1], "Configuration applied successfully.")
                self.assertIn("Waiting for OpenEvent to become ready...", lines)
                self.assertIn("  Checking service status...", lines)
                for secret in (data["chat"]["web_token"], data["model"]["api_key"], *runtime.tokens.values()):
                    self.assertNotIn(secret, output.getvalue())

                output = io.StringIO()
                with patch("runtime_reconciler.cli.apply_resolve", side_effect=ApplyError("resource lookup failed")), \
                        redirect_stdout(output), redirect_stderr(io.StringIO()):
                    with self.assertRaisesRegex(ApplyError, "resource lookup failed"):
                        main(["--spec", str(source), "--apply"])
                lines = output.getvalue().splitlines()
                self.assertEqual([line for line in lines if line.startswith("[")], STAGES[:3])
                self.assertNotIn("Configuration applied successfully.", lines)
                self.assertNotIn("  Checking service status...", lines)

    def test_process_progress_uses_configured_name_and_does_not_claim_failed_start(self):
        spec = SimpleNamespace(supervisor_programs={"agent": "custom-agent"})
        cases = (
            ([True], 0, ["  custom-agent: already running."], False),
            ([False, True], 0, ["  Starting custom-agent...", "  custom-agent: running."], False),
            ([False], 1, ["  Starting custom-agent..."], True),
        )
        for statuses, returncode, expected, failed in cases:
            with self.subTest(statuses=statuses, returncode=returncode):
                output = io.StringIO()
                with patch("runtime_reconciler.process.program_running", side_effect=statuses), \
                        patch("runtime_reconciler.process._control", return_value=SimpleNamespace(returncode=returncode)), \
                        redirect_stdout(output):
                    if failed:
                        with self.assertRaises(ApplyError):
                            ensure_program_running(spec, "agent")
                    else:
                        ensure_program_running(spec, "agent")
                self.assertEqual(output.getvalue().splitlines(), expected)

    def test_progress_is_flushed_to_pipe_before_blocking_operations(self):
        script = r'''
import sys
from types import SimpleNamespace
from unittest.mock import patch
from runtime_reconciler.cli import main
from runtime_reconciler.model import ApplyError
from runtime_reconciler.process import stop_program

def blocked(*args):
    sys.stderr.write("operation entered\n")
    sys.stderr.flush()
    sys.stdin.readline()
    raise ApplyError("test operation released")

try:
    if sys.argv[1] == "apply":
        with patch("runtime_reconciler.cli.parse_spec", side_effect=blocked):
            main(["--spec", "unused-test-spec.yaml", "--apply"])
    else:
        with patch("runtime_reconciler.process._control", side_effect=blocked):
            stop_program(SimpleNamespace(supervisor_programs={"agent": "custom-agent"}), "agent")
except ApplyError:
    pass
'''
        # Python must use normal pipe buffering: -u or PYTHONUNBUFFERED would
        # hide a missing flush in the production progress helper.
        environment = {key: value for key, value in os.environ.items() if key != "PYTHONUNBUFFERED"}
        for action, expected in (("apply", STAGES[0]),
                                 ("stop", "  Stopping custom-agent; waiting for exit...")):
            with self.subTest(action=action):
                child = subprocess.Popen([sys.executable, "-B", "-c", script, action], cwd=ROOT,
                                         env=environment, stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    self.assertTrue(select.select([child.stderr], [], [], 5)[0], "operation was not entered")
                    self.assertEqual(child.stderr.readline(), b"operation entered\n")
                    self.assertIsNone(child.poll())
                    self.assertTrue(select.select([child.stdout], [], [], 5)[0],
                                    "progress was buffered while the operation waited")
                    self.assertEqual(os.read(child.stdout.fileno(), 65536).decode(), expected + "\n")
                    self.assertIsNone(child.poll())
                    output, errors = child.communicate(input=b"continue\n", timeout=5)
                    self.assertEqual(child.returncode, 0, errors.decode())
                    self.assertEqual(output, b"")
                    self.assertEqual(errors, b"")
                finally:
                    if child.poll() is None:
                        child.kill()
                        child.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()

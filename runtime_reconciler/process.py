from __future__ import annotations
import subprocess
import time
from .common import progress
from .model import ApplyError, PROCESS_START_ORDER

def _control(spec, action, logical):
    return subprocess.run([spec.supervisor_ctl, action, spec.supervisor_programs[logical]], check=False, capture_output=True, text=True)

def program_running(spec, logical):
    result = _control(spec, "status", logical)
    return result.returncode == 0 and "RUNNING" in result.stdout

def ensure_program_running(spec, logical):
    name = spec.supervisor_programs[logical]
    if program_running(spec, logical):
        progress(f"  {name}: already running.")
        return
    progress(f"  Starting {name}...")
    if _control(spec, "start", logical).returncode:
        raise ApplyError(f"could not start {logical}; inspect its log")
    if not program_running(spec, logical):
        raise ApplyError(f"{logical} did not reach RUNNING; inspect its log")
    progress(f"  {name}: running.")

def stop_program(spec, logical):
    progress(f"  Stopping {spec.supervisor_programs[logical]}; waiting for exit...")
    _control(spec, "stop", logical)
    status = _control(spec, "status", logical)
    stopped = any(state in status.stdout.split() for state in ("STOPPED", "EXITED", "FATAL"))
    if not stopped:
        raise ApplyError(f"could not confirm {logical} stopped")

def start_configured(spec, plan, *, stopped=()):
    """Reuse stops confirmed earlier in this invocation; always verify startup."""
    changed = {key for key, item in plan.get("configs", {}).items() if item.get("changed")}
    # Dependency changes pause Agent before restarting those dependencies.
    if changed and "agent" not in stopped:
        stop_program(spec, "agent")
    for logical in ("model_proxy", "chat_server", "view", "cmd_worker"):
        if logical in changed and logical not in stopped:
            stop_program(spec, logical)
        ensure_program_running(spec, logical)
    ensure_program_running(spec, "agent")
    wait_programs_running(spec)

def wait_programs_running(spec, timeout_s=5.0):
    progress("  Checking service status...")
    deadline = time.monotonic() + timeout_s
    while True:
        pending = [p for p in PROCESS_START_ORDER if not program_running(spec, p)]
        if not pending:
            return
        if time.monotonic() >= deadline:
            raise ApplyError("processes did not reach RUNNING: " + ", ".join(pending))
        time.sleep(.1)

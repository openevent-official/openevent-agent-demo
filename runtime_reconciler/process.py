from __future__ import annotations

import subprocess
import time
from typing import Any

from .model import ApplyError, DesiredSpec, PROCESS_START_ORDER


def ensure_program_running(spec: DesiredSpec, logical: str) -> None:
    program = spec.supervisor_programs[logical]
    status = subprocess.run([spec.supervisor_ctl, "status", program], check=False, capture_output=True, text=True)
    output = f"{status.stdout}\n{status.stderr}"
    if status.returncode == 0 and "RUNNING" in output:
        return
    start = subprocess.run([spec.supervisor_ctl, "start", program], check=False, capture_output=True, text=True)
    if start.returncode != 0:
        raise ApplyError(f"supervisor start failed for {program}: {start.stderr or start.stdout}")


def restart_changed(
    spec: DesiredSpec,
    plan: dict[str, Any],
    skip: set[str] | None = None,
    force: set[str] | None = None,
) -> None:
    skip = skip or set()
    force = force or set()
    configs = plan.get("configs", {})
    for logical in PROCESS_START_ORDER:
        if logical in skip:
            continue
        config = configs.get(logical, {})
        if logical not in force and not config.get("changed"):
            continue
        program = spec.supervisor_programs[logical]
        result = subprocess.run([spec.supervisor_ctl, "restart", program], check=False)
        if result.returncode != 0:
            raise ApplyError(f"supervisor restart failed for {program}")


def wait_programs_running(spec: DesiredSpec, timeout_s: float = 5.0) -> None:
    pending = set(PROCESS_START_ORDER)
    deadline = time.time() + timeout_s
    last_outputs: dict[str, str] = {}
    while pending and time.time() < deadline:
        for logical in tuple(pending):
            program = spec.supervisor_programs[logical]
            status = subprocess.run(
                [spec.supervisor_ctl, "status", program],
                check=False,
                capture_output=True,
                text=True,
            )
            output = f"{status.stdout}\n{status.stderr}"
            last_outputs[logical] = output.strip()
            if status.returncode == 0 and "RUNNING" in output:
                pending.remove(logical)
        if pending:
            time.sleep(0.1)
    if pending:
        details = "; ".join(
            f"{spec.supervisor_programs[logical]}: {last_outputs.get(logical, 'no status')}"
            for logical in PROCESS_START_ORDER
            if logical in pending
        )
        raise ApplyError(f"programs did not reach RUNNING state: {details}")

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from .common import _dump_yaml
from .model import DesiredSpec, PROCESS_START_ORDER, SpecError
from .openevent import wait_openevent_ready
from .process import ensure_program_running, restart_changed, wait_programs_running
from .render import render_configs
from .resources import apply_resolve, dry_resolve
from .spec import parse_spec
from .state import begin_apply, load_state, record_apply_phase, write_openevent_config, write_runtime_files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--runtime-root")
    parser.add_argument("--print-config", choices=["openevent", "im_syncer", "model_proxy", "cmd_worker", "agent"])
    args = parser.parse_args(argv)
    mode_count = int(args.dry_run) + int(args.apply) + int(args.print_config is not None)
    if mode_count != 1:
        raise SpecError("choose exactly one of --dry-run, --apply, or --print-config")
    repo_root = Path(__file__).resolve().parents[1]
    spec = parse_spec(Path(args.spec), repo_root=repo_root, runtime_root_override=args.runtime_root)
    previous = load_state(spec.paths.state_path)
    if args.apply:
        plan = _apply(spec, previous)
        print(_dump_yaml(plan), end="")
        return 0
    resolved = dry_resolve(spec, previous)
    if args.print_config:
        print(_dump_yaml(render_configs(spec, resolved)[args.print_config]["data"]), end="")
        return 0
    plan = write_runtime_files(spec, resolved, dry_run=True)
    print(_dump_yaml(plan), end="")
    return 0


def _apply(spec: DesiredSpec, previous: dict[str, Any] | None) -> dict[str, Any]:
    begin_apply(spec)
    completed_phase = "parsed"
    attempted_phase = "openevent_ready"
    try:
        openevent_config = write_openevent_config(spec)
        restart_changed(spec, {"configs": {"openevent": openevent_config}})
        ensure_program_running(spec, "openevent")
        wait_openevent_ready(spec)
        record_apply_phase(spec, "openevent_ready")
        completed_phase = "openevent_ready"

        attempted_phase = "resources_resolved"
        resolved = apply_resolve(spec, previous)
        record_apply_phase(spec, "resources_resolved")
        completed_phase = "resources_resolved"

        attempted_phase = "config_committed"
        plan = write_runtime_files(spec, resolved, dry_run=False, openevent_config=openevent_config)
        completed_phase = "config_committed"

        attempted_phase = "processes_running"
        force_restart = (
            {"model_proxy", "cmd_worker", "im_syncer", "agent"}
            if openevent_config.get("changed")
            else set()
        )
        restart_changed(spec, plan, skip={"openevent"}, force=force_restart)
        for logical in PROCESS_START_ORDER[1:]:
            ensure_program_running(spec, logical)
        wait_programs_running(spec)
        record_apply_phase(spec, "processes_running", status="complete")
        return plan
    except Exception:
        try:
            record_apply_phase(
                spec,
                completed_phase,
                status="failed",
                failed_phase=attempted_phase,
            )
        except Exception:
            pass
        raise

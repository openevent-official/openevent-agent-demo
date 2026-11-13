from __future__ import annotations
import argparse
import sys
from pathlib import Path
from .common import _dump_yaml, progress
from .model import SpecError, PROCESS_START_ORDER
from .openevent import wait_openevent_ready
from .process import ensure_program_running, stop_program, start_configured
from .render import render_configs, render_openevent_config, validate_openevent_config
from .resources import apply_resolve, dry_resolve
from .spec import parse_spec
from .state import (INITIALIZATION_FAILURE_MESSAGE, add_pending_restarts, begin_apply, complete_start, config_record, load_state,
                    prepare_runtime_files, require_completed_initialization, save_state,
                    write_diagnostics, write_openevent_config, write_runtime_files)

OPENEVENT_CLIENTS = ("agent", "cmd_worker", "model_proxy", "chat_server", "view")

def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", required=True)
    parser.add_argument("--runtime-root")
    parser.add_argument("--write-diagnostics", action="store_true",
                        help="export optional redacted configuration and plan after a successful --apply")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--dry-run", action="store_true")
    modes.add_argument("--apply", action="store_true")
    modes.add_argument("--start", action="store_true")
    modes.add_argument("--print-config", choices=["openevent", "chat_server", "model_proxy", "cmd_worker", "agent", "view"])
    args = parser.parse_args(argv)
    if args.write_diagnostics and not args.apply:
        parser.error("--write-diagnostics requires --apply")
    if args.apply:
        progress("[1/5] Checking configuration...")
    spec_path = Path(args.spec).resolve()
    spec = parse_spec(spec_path, args.runtime_root)
    if args.dry_run or args.print_config == "agent":
        from chat_model_agent.budget import ContextBudget
        budget = ContextBudget.from_settings(
            model=spec.model["model"], system_prompt=spec.agent["system_prompt"],
            context_compression_prompt=spec.agent["context_compression_prompt"],
            context_max_input_bytes=spec.agent["context_max_input_bytes"])
        print(f"Configured context budget (before publication limits): B={budget.limit}, F={budget.fixed}, T={budget.target}; "
              f"Chat body limit={spec.chat['max_user_message_bytes']}; "
              f"tool output limit per stream={spec.agent['max_tool_output_bytes']}", file=sys.stderr)
    previous = load_state(spec.paths.state_path, spec=spec)
    if args.apply:
        plan = _apply(spec, previous)
        if args.write_diagnostics:
            write_diagnostics(spec, plan)
    elif args.start:
        require_completed_initialization(previous)
        if not previous or not previous.get("channels") or not previous.get("configs"):
            raise SpecError("run --apply to initialize this Chat deployment first")
        if previous.get("applied_spec_sha256") != spec.fingerprint():
            raise SpecError("spec differs from the applied configuration; run --apply first")
        pending = set(previous.get("pending_restarts", ()))
        stopped = set()
        if pending:
            pending.add("agent")
        if "openevent" in pending:
            pending.update(PROCESS_START_ORDER)
            previous["pending_restarts"] = sorted(pending)
            save_state(spec, previous)
            for logical in (*OPENEVENT_CLIENTS, "openevent"):
                stop_program(spec, logical)
                stopped.add(logical)
        ensure_program_running(spec, "openevent")
        progress("Waiting for OpenEvent to become ready...")
        wait_openevent_ready(spec)
        stopped.discard("openevent")
        pending.discard("openevent")
        previous["pending_restarts"] = sorted(pending)
        save_state(spec, previous)
        plan = {"configs": {k: {**v, "changed": k in pending} for k, v in previous["configs"].items()}}
        start_configured(spec, plan, stopped=stopped)
        complete_start(spec)
    else:
        resolved = dry_resolve(spec, previous)
        if args.print_config:
            print(_dump_yaml(render_configs(spec, resolved)[args.print_config]["data"]), end="")
            return 0
        plan = write_runtime_files(spec, resolved, dry_run=True)
    if args.apply:
        progress("Configuration applied successfully.")
    elif args.start:
        progress("Services started successfully.")
    else:
        print(_dump_yaml(plan), end="")
    return 0

def _apply(spec, previous):
    previous = load_state(spec.paths.state_path, spec=spec) or previous
    require_completed_initialization(previous)
    dry_resolve(spec, previous)
    server_config = render_openevent_config(spec)
    validate_openevent_config(server_config)
    server_changed = config_record(spec, "openevent", "openevent-server.yaml", server_config,
                                   previous or {})["changed"]
    pending = set((previous or {}).get("pending_restarts", ()))
    if server_changed:
        pending.update(PROCESS_START_ORDER)
    elif pending:
        pending.add("agent")
    progress("[2/5] Preparing OpenEvent...")
    begin_apply(spec, pending)
    stopped = set()
    try:
        if server_changed:
            for logical in (*OPENEVENT_CLIENTS, "openevent"):
                stop_program(spec, logical)
                stopped.add(logical)
        elif pending:
            stop_program(spec, "agent")
            stopped.add("agent")
        write_openevent_config(spec)
        ensure_program_running(spec, "openevent")
        progress("Waiting for OpenEvent to become ready...")
        wait_openevent_ready(spec)
        stopped.discard("openevent")
        progress("[3/5] Preparing access tokens and channels...")
        resolved = apply_resolve(spec, previous)
        progress("[4/5] Preparing service configuration...")
        prepared = prepare_runtime_files(spec, resolved)
        changed = {key for key, item in prepared[1]["configs"].items() if item["changed"]} - {"openevent"}
        if changed:
            # Resource resolution can reveal a token/config change. Persist its
            # restart intent and pause Agent before writing any dependency file.
            add_pending_restarts(spec, changed | {"agent"})
            if "agent" not in stopped:
                stop_program(spec, "agent")
                stopped.add("agent")
        plan = write_runtime_files(spec, resolved, dry_run=False, prepared=prepared, completed={"openevent"})
        progress("[5/5] Updating services...")
        start_configured(spec, plan, stopped=stopped)
        complete_start(spec)
        return plan
    except Exception:
        if previous is None:
            print(f"error: {INITIALIZATION_FAILURE_MESSAGE}", file=sys.stderr)
        raise


if __name__ == "__main__":
    from .model import ReconcileError
    try:
        raise SystemExit(main())
    except ReconcileError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(exc.exit_code)

"""Run real signals in bounded children so regressions cannot hang the suite."""

import faulthandler
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from chat_model_agent import cli, config, worker
from openevent import sdk


def run_child(case, signum=None):
    cfg = config.parse_config({
        "version": "chat-agent.v1", "deployment_id": "signal-test",
        "openevent": {"target": "unused:1"},
        "agent": {"principal": 10, "user_principal": 11, "token": "test",
                  "system_prompt": "", "context_compression_prompt": "compress",
                  "context_max_input_bytes": 1048576, "model": "test",
                  "work_root": str(Path(__file__).resolve().parents[1] / "build/tests/work")},
        "model_proxy": {"principal": 12}, "cmd_worker": {"principal": 13},
        "channels": {"model": 20, "cmd": 21, "wal": 22},
    })
    closed = threading.Event()
    events = SimpleNamespace(close=closed.set)
    agent = worker.ChatModelAgent(cfg, events)
    waiting = threading.Event()
    sent = False

    async def coordinate():
        if case == "locked":
            agent.request_stop()
        else:
            waiting.set()
            await agent.wait_async(agent.stop.is_set)

    agent._coordinate = coordinate

    def trace(frame, what, arg):
        nonlocal sent
        if (not sent and what == "line"
                and frame.f_code is threading.Event.set.__code__
                and frame.f_locals.get("self") is agent.stop
                and agent.stop._cond._lock.locked()):
            sent = True
            # Both signals arrive while the real ordinary Event lock is held.
            os.kill(os.getpid(), signal.SIGTERM)
            os.kill(os.getpid(), signal.SIGINT)
        return trace

    def send_when_idle():
        nonlocal sent
        if waiting.wait(2):
            sent = True
            os.kill(os.getpid(), signum)

    # A caller's non-default handler must survive the CLI's event-loop lifetime.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    sender = None
    if case == "locked":
        sys.settrace(trace)
    else:
        sender = threading.Thread(target=send_when_idle, daemon=True)
        sender.start()
    try:
        with patch.object(config, "load_config", return_value=cfg), \
                patch.object(sdk, "OpenEventClient", return_value=events), \
                patch.object(worker, "ChatModelAgent", return_value=agent):
            result = cli.main(["--config", "unused-test-config"])
    finally:
        sys.settrace(None)
        if sender is not None:
            sender.join(2)
    assert result == 0, "Agent failed during signal shutdown"
    assert sent, "Test did not deliver its signal"
    assert agent._closed and closed.is_set(), "Shutdown did not close the Agent"
    assert not agent.stop._cond._lock.locked(), "Shutdown retained its Event lock"
    assert all(signal.getsignal(sig) is handler for sig, handler in handlers.items()), \
        "CLI did not restore the caller's signal handlers"
    if sender is not None:
        assert not sender.is_alive()


class AgentCLISignalTests(unittest.TestCase):
    def child(self, case, signum=None):
        command = [sys.executable, "-B", str(Path(__file__).resolve()), case]
        if signum is not None:
            command.append(str(signum))
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, timeout=6)
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_signals_during_shutdown_do_not_reenter_event_lock(self):
        self.child("locked")

    def test_signals_wake_idle_agent(self):
        for signum in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=signum):
                self.child("idle", signum)


if __name__ == "__main__":
    faulthandler.dump_traceback_later(3, exit=True)
    try:
        run_child(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else None)
    finally:
        faulthandler.cancel_dump_traceback_later()

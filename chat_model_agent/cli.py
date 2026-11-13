from __future__ import annotations

import argparse
import asyncio
import logging
import signal


async def _run(agent):
    loop = asyncio.get_running_loop()
    previous_handlers = {}
    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous = signal.getsignal(sig)
            # asyncio queues the callback and wakes an idle loop. Never acquire
            # Agent or Event locks from a synchronous Python signal handler.
            loop.add_signal_handler(sig, agent.request_stop)
            previous_handlers[sig] = previous
        await agent._run()
    finally:
        for sig, handler in previous_handlers.items():
            loop.remove_signal_handler(sig)
            signal.signal(sig, handler)


def main(argv=None):
    from openevent.sdk import OpenEventClient
    from .config import load_config
    from .dependencies import require_dependencies
    from .worker import ChatModelAgent
    parser = argparse.ArgumentParser(prog="chat-model-agent")
    parser.add_argument("--config", required=True)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO))
    require_dependencies()
    config = load_config(args.config)
    agent = ChatModelAgent(config, OpenEventClient(config.openevent.target, timeout_ms=config.rpc.timeout_ms))
    try:
        asyncio.run(_run(agent))
    except Exception:
        logging.getLogger(__name__).error("Chat Agent exited after an infrastructure or history failure")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

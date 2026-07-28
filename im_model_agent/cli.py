from __future__ import annotations

import argparse
import logging


def main(argv: list[str] | None = None) -> int:
    from openevent.sdk import OpenEventClient

    from .config import load_config
    from .dependencies import validate_runtime_dependencies
    from .worker import ImModelAgent

    parser = argparse.ArgumentParser(prog="im-model-agent")
    parser.add_argument("--config", required=True)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO))
    validate_runtime_dependencies()
    config = load_config(args.config)
    agent = ImModelAgent(config, OpenEventClient(config.openevent.target))
    agent.start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

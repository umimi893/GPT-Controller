from __future__ import annotations

import argparse
import logging
from pathlib import Path

from .config import Config
from .runtime import Runtime
from .lock import RuntimeLock
from .util import configure_file_logging


def main() -> None:
    parser = argparse.ArgumentParser(prog="q-agent-v4")
    parser.add_argument("--config", required=True, help="Path to agent.config.json")
    parser.add_argument("--once", action="store_true", help="Poll and execute at most one action")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    config_path = Path(args.config).expanduser().resolve()
    config = Config.load(config_path)
    level = getattr(logging, args.log_level.upper(), logging.INFO)
    configure_file_logging(config.repo_path.parent / "logs" / "q-agent-v4.log", level)

    runtime = Runtime(config, config_path)
    lock_path = config.repo_path.parent / f".{config.agent_id}.q-agent-v4.lock"
    with RuntimeLock(lock_path):
        if args.once:
            runtime.run_once()
        else:
            runtime.run_forever()


if __name__ == "__main__":
    main()

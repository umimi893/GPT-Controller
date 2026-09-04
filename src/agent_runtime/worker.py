from __future__ import annotations

import argparse
import os
import time
import traceback
from pathlib import Path
from typing import Any

from .config import Config
from .supervised_executor import SupervisedExecutor
from .util import atomic_write_json, load_json, utc_now


def _wait_for_gate(path: Path, timeout_seconds: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    raise RuntimeError("Action worker start gate was not released by supervisor")


def main() -> None:
    parser = argparse.ArgumentParser(prog="q-agent-v4-worker")
    parser.add_argument("--config", required=True)
    parser.add_argument("--action", required=True)
    parser.add_argument("--result", required=True)
    parser.add_argument("--progress", required=True)
    parser.add_argument("--gate", required=True)
    args = parser.parse_args()

    config = Config.load(Path(args.config))
    action_path = Path(args.action)
    result_path = Path(args.result)
    progress_path = Path(args.progress)
    gate_path = Path(args.gate)
    action = load_json(action_path)
    action_id = str(action.get("id", "unknown"))
    started_at = utc_now()

    def progress(update: dict[str, Any]) -> None:
        payload = {
            "protocol": "q-agent-v4-worker-progress",
            "action_id": action_id,
            "agent_id": config.agent_id,
            "worker_pid": os.getpid(),
            "updated_at": utc_now(),
            **update,
        }
        if update.get("event") == "started":
            payload["phase_started_at"] = payload["updated_at"]
        atomic_write_json(progress_path, payload)

    try:
        _wait_for_gate(gate_path)
        progress({"phase": "worker", "event": "started"})
        executor = SupervisedExecutor(config, progress)
        result = executor.execute(action)
    except BaseException as exc:
        result = {
            "protocol": "q-agent-v4-result",
            "action_id": action_id,
            "agent_id": config.agent_id,
            "started_at": started_at,
            "finished_at": utc_now(),
            "status": "failed",
            "error": f"isolated action worker failed: {exc}",
            "steps": [],
            "workspace_guards": {},
            "worker_traceback": traceback.format_exc(limit=12),
        }
    atomic_write_json(result_path, result)
    progress({"phase": "worker", "event": "finished", "status": result.get("status")})


if __name__ == "__main__":
    main()

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .executor import Executor


ProgressCallback = Callable[[dict[str, Any]], None]


class SupervisedExecutor(Executor):
    """Executor variant that reports durable phase boundaries to its supervisor.

    The ordinary Executor keeps all execution semantics. This subclass only reports
    which step is currently active and the timeout that should bound it, allowing the
    parent runtime to detect a live-but-stuck worker without relying on process liveness.
    """

    def __init__(self, config, progress: ProgressCallback):
        super().__init__(config)
        self._progress = progress

    def _step(self, action: dict[str, Any], step: dict[str, Any]) -> Any:
        index = next(
            (candidate_index for candidate_index, candidate in enumerate(action.get("steps", [])) if candidate is step),
            None,
        )
        timeout = int(step.get("timeout_seconds", action.get("timeout_seconds", self.config.default_timeout_seconds)))
        self._progress({
            "phase": "step",
            "event": "started",
            "step_index": index,
            "step_type": step.get("type"),
            "timeout_seconds": timeout,
        })
        try:
            return super()._step(action, step)
        finally:
            self._progress({
                "phase": "step",
                "event": "finished",
                "step_index": index,
                "step_type": step.get("type"),
                "timeout_seconds": timeout,
            })

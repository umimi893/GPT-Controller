from __future__ import annotations

import logging
import os
import threading
import time
import traceback
from pathlib import Path
from typing import Any

from .config import Config
from .process_supervisor import ActionSupervisor
from .resilient_bus import ResilientGitBus
from .util import atomic_write_json, utc_now

log = logging.getLogger("q-agent-v4")


class Runtime:
    HEARTBEAT_INTERVAL_SECONDS = 15

    def __init__(self, config: Config, config_path: Path | None = None):
        self.config = config
        self.config_path = (config_path or Path("agent.config.json")).resolve()
        self.bus = ResilientGitBus(config)
        self.supervisor = ActionSupervisor(config, self.config_path)
        self._recovered = False
        self._remote_head: str | None = None
        self._heartbeat_path = config.repo_path.parent / "state" / f"{config.agent_id}-runtime-heartbeat.json"
        self._heartbeat_lock = threading.RLock()
        self._heartbeat_state = "starting"
        self._heartbeat_action_id: str | None = None
        self._heartbeat_details: dict[str, Any] = {}
        self._heartbeat_state_started_at = utc_now()
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None

    def _write_heartbeat(self) -> None:
        with self._heartbeat_lock:
            payload = {
                "protocol": "q-agent-v4-heartbeat",
                "agent_id": self.config.agent_id,
                "pid": os.getpid(),
                "branch": self.config.branch,
                "state": self._heartbeat_state,
                "state_started_at": self._heartbeat_state_started_at,
                "action_id": self._heartbeat_action_id,
                "updated_at": utc_now(),
                **self._heartbeat_details,
            }
            for attempt in range(3):
                try:
                    atomic_write_json(self._heartbeat_path, payload)
                    return
                except PermissionError:
                    if attempt == 2:
                        raise
                    time.sleep(0.05)

    def _set_heartbeat_state(self, state: str, action_id: str | None = None, **details: Any) -> None:
        with self._heartbeat_lock:
            if state != self._heartbeat_state or action_id != self._heartbeat_action_id:
                self._heartbeat_state_started_at = utc_now()
            self._heartbeat_state = state
            self._heartbeat_action_id = action_id
            self._heartbeat_details = dict(details)
            self._write_heartbeat()

    def _on_worker_progress(self, progress: dict[str, Any]) -> None:
        action_id = self._heartbeat_action_id
        details: dict[str, Any] = {
            "worker_pid": progress.get("worker_pid"),
            "worker_progress_at": progress.get("updated_at"),
            "worker_phase": progress.get("phase"),
            "worker_event": progress.get("event"),
        }
        if progress.get("phase") == "step" and progress.get("event") == "started":
            details.update({
                "step_index": progress.get("step_index"),
                "step_type": progress.get("step_type"),
                "step_started_at": progress.get("phase_started_at"),
                "step_timeout_seconds": progress.get("timeout_seconds"),
            })
        self._set_heartbeat_state("executing", action_id, **details)

    def _heartbeat_loop(self) -> None:
        while not self._heartbeat_stop.wait(self.HEARTBEAT_INTERVAL_SECONDS):
            try:
                self._write_heartbeat()
            except Exception:
                log.exception("runtime heartbeat write failed")

    def _start_heartbeat(self) -> None:
        self._heartbeat_stop.clear()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            name="gpt-controller-heartbeat",
            daemon=True,
        )
        self._heartbeat_thread.start()

    def _stop_heartbeat(self) -> None:
        self._heartbeat_stop.set()
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=2)
            self._heartbeat_thread = None

    def run_forever(self) -> None:
        log.info("GPT Controller supervisor starting: agent_id=%s branch=%s", self.config.agent_id, self.config.branch)
        self._set_heartbeat_state("starting")
        self._start_heartbeat()
        try:
            while True:
                try:
                    self._set_heartbeat_state("polling")
                    did_work = self.run_once()
                    self._set_heartbeat_state("idle" if not did_work else "completed_action")
                    if not did_work:
                        time.sleep(self.config.poll_seconds)
                except KeyboardInterrupt:
                    self._set_heartbeat_state("stopping")
                    raise
                except Exception:
                    self._set_heartbeat_state("loop_error")
                    log.exception("runtime supervisor loop error")
                    time.sleep(max(self.config.poll_seconds, 5))
        finally:
            self._stop_heartbeat()

    def _supervisor_failure_result(self, action: dict[str, Any], exc: Exception) -> dict[str, Any]:
        return {
            "protocol": "q-agent-v4-result",
            "action_id": action["id"],
            "agent_id": self.config.agent_id,
            "started_at": None,
            "finished_at": utc_now(),
            "status": "failed",
            "error": f"Action supervisor failed before worker Result: {exc}",
            "steps": [],
            "workspace_guards": {},
            "supervisor": {
                "isolated_worker": True,
                "infrastructure_failure": True,
                "traceback": traceback.format_exc(limit=12),
            },
        }

    def run_once(self) -> bool:
        # Idle polling should be one cheap remote-head lookup, not a full fetch/status/
        # rev-list sequence every few seconds. A real sync runs only when the remote
        # branch changes (or during startup recovery).
        remote_head = self.bus.remote_head()
        if self._remote_head != remote_head:
            self.bus.sync()
            self._remote_head = remote_head

        if not self._recovered:
            count = self.bus.recover_ambiguous()
            if count:
                log.warning("reconciled %d previously claimed action(s) after restart; none were replayed", count)
                self.bus.sync()
                self._remote_head = self.bus.remote_head()
            self._recovered = True

        for path in self.bus.pending():
            try:
                claimed = self.bus.claim(path)
            except Exception as exc:
                log.error("rejecting malformed/unexecutable action file %s: %s", path.name, exc)
                self.bus.reject(path, str(exc))
                return True
            if claimed is None:
                continue

            action, running = claimed
            action_id = str(action["id"])
            log.info("claimed action %s", action_id)
            self._set_heartbeat_state("executing", action_id, action_started_at=utc_now())
            try:
                result = self.supervisor.execute(action, on_progress=self._on_worker_progress)
            except Exception as exc:
                log.exception("Action supervisor infrastructure failure for %s", action_id)
                result = self._supervisor_failure_result(action, exc)

            self._set_heartbeat_state("publishing_result", action_id, result_status=result.get("status"))
            try:
                self.bus.finish(action, running, result)
            except Exception:
                # finish() creates the local Result commit before its retry loop. Force a
                # full sync next iteration so an eventually-restored network publishes
                # that commit instead of leaving a clean-looking but local-only Result.
                self._remote_head = None
                raise
            log.info("finished action %s: %s", action_id, result["status"])
            return True
        return False

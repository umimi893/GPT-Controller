import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent_runtime.runtime import Runtime


class RuntimeHeartbeatTests(unittest.TestCase):
    def make_runtime(self, root: Path) -> Runtime:
        runtime = Runtime.__new__(Runtime)
        runtime.config = SimpleNamespace(agent_id="test-agent", branch="gpt-controller-control")
        runtime._heartbeat_path = root / "heartbeat.json"
        runtime._heartbeat_lock = threading.RLock()
        runtime._heartbeat_state = "starting"
        runtime._heartbeat_action_id = None
        runtime._heartbeat_details = {}
        runtime._heartbeat_state_started_at = "2026-01-01T00:00:00Z"
        runtime._heartbeat_stop = threading.Event()
        runtime._heartbeat_thread = None
        runtime.HEARTBEAT_INTERVAL_SECONDS = 0.05
        return runtime

    def test_heartbeat_records_action_state(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = self.make_runtime(Path(temp))
            runtime._set_heartbeat_state("executing", "action-123")
            payload = json.loads(runtime._heartbeat_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["state"], "executing")
            self.assertEqual(payload["action_id"], "action-123")
            self.assertEqual(payload["agent_id"], "test-agent")
            self.assertIn("state_started_at", payload)

    def test_worker_progress_exposes_step_deadline_to_external_watchdog(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = self.make_runtime(Path(temp))
            runtime._set_heartbeat_state("executing", "action-456")
            runtime._on_worker_progress({
                "worker_pid": 1234,
                "updated_at": "2026-01-01T00:00:10Z",
                "phase": "step",
                "event": "started",
                "phase_started_at": "2026-01-01T00:00:10Z",
                "step_index": 2,
                "step_type": "browser.playwright",
                "timeout_seconds": 180,
            })
            payload = json.loads(runtime._heartbeat_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["worker_pid"], 1234)
            self.assertEqual(payload["step_index"], 2)
            self.assertEqual(payload["step_type"], "browser.playwright")
            self.assertEqual(payload["step_timeout_seconds"], 180)
            self.assertEqual(payload["step_started_at"], "2026-01-01T00:00:10Z")

    def test_background_heartbeat_advances_while_state_is_unchanged(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = self.make_runtime(Path(temp))
            runtime._set_heartbeat_state("executing", "long-action", step_timeout_seconds=600)
            first = json.loads(runtime._heartbeat_path.read_text(encoding="utf-8"))["updated_at"]
            runtime._start_heartbeat()
            try:
                time.sleep(0.12)
                second = json.loads(runtime._heartbeat_path.read_text(encoding="utf-8"))["updated_at"]
            finally:
                runtime._stop_heartbeat()
            self.assertNotEqual(first, second)
            payload = json.loads(runtime._heartbeat_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["step_timeout_seconds"], 600)


if __name__ == "__main__":
    unittest.main()

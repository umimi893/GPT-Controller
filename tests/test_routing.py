import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_runtime.config import Config
from agent_runtime.git_bus import GitBus


class RoutingTests(unittest.TestCase):
    def _config(self, agent_id: str = "desktop-pc") -> Config:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg_path = root / "config.json"
            cfg_path.write_text(json.dumps({
                "agent_id": agent_id,
                "capabilities": ["git", "browser"],
                "control_repo": str(root / "control"),
                "interaction_policy": {
                    "non_interference": True,
                    "allow_physical_input": False,
                    "allow_foreground_activation": False
                }
            }))
            return Config.load(cfg_path)

    def test_capability_filter(self):
        bus = GitBus(self._config())
        self.assertTrue(bus._eligible({"requires": ["git"]}))
        self.assertFalse(bus._eligible({"requires": ["windows-ui"]}))

    def test_ordered_preference(self):
        created = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        action = {
            "created_at": created,
            "target": {"mode": "ordered", "order": ["desktop-pc", "worker-pc"], "fallback_after_seconds": 60}
        }
        self.assertTrue(GitBus(self._config("desktop-pc"))._eligible(action))
        self.assertFalse(GitBus(self._config("worker-pc"))._eligible(action))

    def test_ordered_fallback(self):
        created = (datetime.now(timezone.utc) - timedelta(seconds=70)).isoformat().replace("+00:00", "Z")
        action = {
            "created_at": created,
            "target": {"mode": "ordered", "order": ["desktop-pc", "worker-pc"], "fallback_after_seconds": 60}
        }
        self.assertTrue(GitBus(self._config("worker-pc"))._eligible(action))

    def test_non_interference_rejects_input_injection(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg_path = root / "config.json"
            cfg_path.write_text(json.dumps({
                "agent_id": "desktop-pc",
                "control_repo": str(root / "control"),
                "interaction_policy": {
                    "non_interference": True,
                    "allow_physical_input": True
                }
            }))
            with self.assertRaises(ValueError):
                Config.load(cfg_path)


if __name__ == "__main__":
    unittest.main()
